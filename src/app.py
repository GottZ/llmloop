import sys
import os
import json
import time
import uuid
import tempfile
import zipfile
import shutil
import subprocess
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, flash, session, Response, stream_with_context, jsonify, send_file, after_this_request
from config import Config
from db import init_db, wait_for_db, get_db_connection, get_active_backend
from orchestrator import Orchestrator, PLANNER_HISTORY_LIMIT
from llm_client import list_models, LLMError
from token_stream import pop_tokens, clear_tokens

# Fix import path for docker
sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")

app = Flask(__name__)
app.config.from_object(Config)

# Global state for HITL (simple in-memory storage for this demo)
# Key: thread_id, Value: {tool_history: [], runner_resp: "", commands: []}
PENDING_APPROVALS = {}
# Track thread-level activity for SSE status updates
THREAD_STATUSES = {}
# Track prepared upload workspaces before thread creation
UPLOAD_WORKSPACES = {}


def set_thread_status(thread_id, state):
    THREAD_STATUSES[thread_id] = {"state": state, "token": uuid.uuid4().hex}


def get_thread_status(thread_id):
    status = THREAD_STATUSES.get(thread_id)
    if status:
        return status
    # Deterministic token so SSE consumers still get an initial state
    return {"state": "idle", "token": f"idle-{thread_id}"}


def clear_thread_status(thread_id):
    THREAD_STATUSES.pop(thread_id, None)


def estimate_tokens(text):
    if not text:
        return 0
    stripped = text.strip()
    if not stripped:
        return 0
    # Rough heuristic: word count approximates token count for display purposes
    parts = stripped.split()
    return max(1, len(parts))


def _prepare_local_cwd(path):
    return path if path else "/tmp"


def _safe_relative_filename(filename):
    if not filename:
        return None
    normalized = filename.replace("\\", "/").strip("/")
    if not normalized:
        return None
    candidate = Path(normalized)
    parts = []
    for part in candidate.parts:
        if part in ("", ".", ".."):
            continue
        parts.append(part)
    if not parts:
        return None
    return "/".join(parts)


def _prepare_upload_workspace(upload_files):
    files = [f for f in upload_files if f and f.filename]
    if not files:
        raise ValueError("No files selected for upload.")
    archive_dir = tempfile.mkdtemp(prefix="thread_upload_", dir="/tmp")
    archive_path = os.path.join(archive_dir, "payload.zip")
    wrote_any = False
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for storage in files:
            rel_path = _safe_relative_filename(storage.filename)
            if not rel_path:
                continue
            storage.stream.seek(0)
            archive.writestr(rel_path, storage.read())
            wrote_any = True
    if not wrote_any:
        shutil.rmtree(archive_dir, ignore_errors=True)
        raise ValueError("Upload did not contain any files.")
    workspace = tempfile.mkdtemp(prefix="thread_workspace_", dir="/tmp")
    with zipfile.ZipFile(archive_path, "r") as archive:
        archive.extractall(workspace)
    shutil.rmtree(archive_dir, ignore_errors=True)
    return _finalize_workspace(workspace)


def _prepare_git_workspace(git_url):
    if not git_url:
        raise ValueError("Git URL is required.")
    workspace = tempfile.mkdtemp(prefix="thread_git_", dir="/tmp")
    try:
        result = subprocess.run(
            ["git", "clone", "--depth", "1", git_url, workspace],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            raise ValueError(f"Git clone failed: {stderr[:200]}")
        return _finalize_workspace(workspace)
    except Exception:
        shutil.rmtree(workspace, ignore_errors=True)
        raise


def _finalize_workspace(path):
    if not path or not os.path.isdir(path):
        return path
    for artifact in ("__MACOSX", ".DS_Store"):
        target = os.path.join(path, artifact)
        if os.path.isdir(target):
            shutil.rmtree(target, ignore_errors=True)
        elif os.path.isfile(target):
            os.remove(target)
    entries = [entry for entry in os.listdir(path) if entry not in ("__MACOSX", ".DS_Store")]
    if len(entries) == 1:
        child = os.path.join(path, entries[0])
        if os.path.isdir(child):
            for item in os.listdir(child):
                shutil.move(os.path.join(child, item), os.path.join(path, item))
            os.rmdir(child)
    return path

@app.route('/')
def index():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=import_extras().RealDictCursor)
    cur.execute("SELECT * FROM threads ORDER BY created_at DESC")
    threads = cur.fetchall()
    cur.close()
    conn.close()
    
    backend = get_active_backend()
    return render_template('index.html', threads=threads, backend=backend)

@app.route('/thread/new', methods=['POST'])
def new_thread():
    source_type = request.form.get('source_type', 'local')
    try:
        if source_type == 'upload':
            files = request.files.getlist('upload[]')
            cwd = _prepare_upload_workspace(files)
        elif source_type == 'upload_prepared':
            workspace_id = request.form.get('workspace_id')
            cwd = UPLOAD_WORKSPACES.pop(workspace_id, None)
            if not cwd or not os.path.isdir(cwd):
                raise ValueError("Uploaded workspace expired or missing. Please upload again.")
            cwd = _finalize_workspace(cwd)
        elif source_type == 'git':
            git_url = request.form.get('git_url', '').strip()
            cwd = _prepare_git_workspace(git_url)
        else:
            cwd = _prepare_local_cwd(request.form.get('cwd', '/tmp').strip())
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for('index'))
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO threads (cwd, context_summary) VALUES (%s, '') RETURNING id", (cwd,))
    thread_id = cur.fetchone()[0]
    conn.close()
    set_thread_status(thread_id, "idle")
    return redirect(url_for('view_thread', thread_id=thread_id))


@app.route('/thread/upload/init', methods=['POST'])
def init_upload_workspace():
    workspace = tempfile.mkdtemp(prefix="thread_upload_stream_", dir="/tmp")
    workspace_id = uuid.uuid4().hex
    UPLOAD_WORKSPACES[workspace_id] = workspace
    return jsonify({"workspace_id": workspace_id})


@app.route('/thread/upload/file', methods=['POST'])
def upload_workspace_file():
    workspace_id = request.form.get('workspace_id')
    rel_path = _safe_relative_filename(request.form.get('path'))
    file_obj = request.files.get('file')
    workspace = UPLOAD_WORKSPACES.get(workspace_id)
    if not workspace or not rel_path or not file_obj:
        return jsonify({"error": "Invalid upload parameters."}), 400
    dest_path = os.path.join(workspace, rel_path)
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    file_obj.save(dest_path)
    return jsonify({"status": "ok"})


@app.route('/thread/upload/cancel', methods=['POST'])
def cancel_upload_workspace():
    workspace_id = request.form.get('workspace_id')
    workspace = UPLOAD_WORKSPACES.pop(workspace_id, None)
    if workspace:
        shutil.rmtree(workspace, ignore_errors=True)
    return jsonify({"status": "cancelled"})

@app.route('/thread/<int:thread_id>')
def view_thread(thread_id):
    conn = get_db_connection()
    extras = import_extras()
    cur = conn.cursor(cursor_factory=extras.RealDictCursor)
    
    # Get Thread
    cur.execute("SELECT * FROM threads WHERE id = %s", (thread_id,))
    thread = cur.fetchone()
    
    # Get Messages
    cur.execute("SELECT * FROM messages WHERE thread_id = %s ORDER BY created_at ASC", (thread_id,))
    messages = cur.fetchall()
    
    cur.close()
    conn.close()
    
    backend = get_active_backend()
    pending_approval = PENDING_APPROVALS.get(thread_id)

    total_tokens = sum(estimate_tokens(msg['content']) for msg in messages)
    context_limit = PLANNER_HISTORY_LIMIT or len(messages) or 0
    if context_limit > 0:
        context_subset = messages[-context_limit:]
    else:
        context_subset = messages
    context_queue = [estimate_tokens(msg['content']) for msg in context_subset]
    context_summary_tokens = estimate_tokens(thread['context_summary']) if thread and thread.get('context_summary') else 0
    context_tokens = context_summary_tokens + sum(context_queue)
    stats = {
        "total_tokens": total_tokens,
        "context_tokens": context_tokens,
        "context_limit": context_limit,
        "context_queue": context_queue,
        "summary_tokens": context_summary_tokens
    }
    
    return render_template('thread.html', 
                           thread=thread, 
                           messages=messages, 
                           backend=backend,
                           pending_approval=pending_approval,
                           stats=stats)

@app.route('/thread/<int:thread_id>/events')
def thread_events(thread_id):
    after = request.args.get('after', default=0, type=int)
    
    def event_stream():
        conn = get_db_connection()
        extras = import_extras()
        last_id = after
        last_pending_token = None
        status_info = get_thread_status(thread_id)
        last_status_token = status_info['token']
        last_heartbeat = time.time()
        yield f"data: {json.dumps({'type': 'status', 'state': status_info['state']})}\n\n"
        try:
            while True:
                with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
                    cur.execute(
                        "SELECT id, role, content, created_at FROM messages WHERE thread_id = %s AND id > %s ORDER BY id ASC",
                        (thread_id, last_id)
                    )
                    rows = cur.fetchall()
                if rows:
                    last_id = rows[-1]['id']
                    for row in rows:
                        payload = {
                            "type": "message",
                            "id": row['id'],
                            "role": row['role'],
                            "content": row['content'],
                            "created_at": row['created_at'].isoformat() if row['created_at'] else ""
                        }
                        yield f"data: {json.dumps(payload)}\n\n"
                pending = PENDING_APPROVALS.get(thread_id)
                pending_token = pending.get('token') if pending else None
                if pending_token != last_pending_token:
                    last_pending_token = pending_token
                    payload = {
                        "type": "pending",
                        "pending": bool(pending),
                        "commands": pending['commands'] if pending else []
                    }
                    yield f"data: {json.dumps(payload)}\n\n"
                token_chunks = pop_tokens(thread_id)
                for chunk in token_chunks:
                    yield f"data: {json.dumps({'type': 'token', **chunk})}\n\n"
                status_state = get_thread_status(thread_id)
                if status_state['token'] != last_status_token:
                    last_status_token = status_state['token']
                    yield f"data: {json.dumps({'type': 'status', 'state': status_state['state']})}\n\n"
                now = time.time()
                if now - last_heartbeat >= 15:
                    yield ": keep-alive\n\n"
                    last_heartbeat = now
                time.sleep(1)
        finally:
            conn.close()
    
    return Response(stream_with_context(event_stream()), mimetype='text/event-stream')

@app.route('/thread/<int:thread_id>/send', methods=['POST'])
def send_message(thread_id):
    user_input = request.form.get('content')
    use_tools = 'use_tools' in request.form
    hitl = 'hitl' in request.form
    continuous_intent = 'continuous_intent' in request.form
    stream_tokens = 'stream_tokens' in request.form
    wants_json = request.accept_mimetypes.best_match(['application/json', 'text/html']) == 'application/json'
    
    orch = Orchestrator(thread_id)
    orch.stream_tokens = stream_tokens
    set_thread_status(thread_id, "running")
    try:
        result = orch.process_user_message(user_input, use_tools, hitl, continuous_intent, stream_tokens)
    except Exception:
        set_thread_status(thread_id, "idle")
        raise
    
    response_body = {"status": result.get('status')}
    
    if result['status'] == 'approval_required':
        pending = dict(result)
        pending['continuous_intent'] = continuous_intent
        pending['token'] = uuid.uuid4().hex
        pending['stream_tokens'] = stream_tokens
        PENDING_APPROVALS[thread_id] = pending
        set_thread_status(thread_id, "waiting_approval")
        if not wants_json:
            flash("Approval required for tool execution.", "warning")
    elif result['status'] == 'error':
        response_body['error'] = result.get('error')
        set_thread_status(thread_id, "idle")
        if not wants_json:
            flash(f"Error: {result.get('error')}", "danger")
    else:
        response_body['status'] = 'complete'
        set_thread_status(thread_id, "idle")
    
    if wants_json:
        return jsonify(response_body)
        
    return redirect(url_for('view_thread', thread_id=thread_id))

@app.route('/thread/<int:thread_id>/approve', methods=['POST'])
def approve_tools(thread_id):
    pending = PENDING_APPROVALS.pop(thread_id, None)
    if not pending:
        flash("No pending approval found.", "danger")
        return redirect(url_for('view_thread', thread_id=thread_id))
    
    action = request.form.get('action')
    if action == 'reject':
        # Just log rejection and stop
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("INSERT INTO messages (thread_id, role, content) VALUES (%s, 'system', 'Tool execution rejected by user.')", (thread_id,))
        conn.close()
        return redirect(url_for('view_thread', thread_id=thread_id))

    # Resume
    orch = Orchestrator(thread_id)
    orch.stream_tokens = pending.get('stream_tokens', False)
    set_thread_status(thread_id, "running")
    continuous_intent = pending.get('continuous_intent', True)
    try:
        result = orch.resume_tool_loop_after_approval(
            pending['tool_history'], pending['runner_resp'], pending['commands'], continuous_intent
        )
    except Exception:
        set_thread_status(thread_id, "idle")
        raise
    
    if result['status'] == 'error':
        flash(f"Error: {result.get('error')}", "danger")
        set_thread_status(thread_id, "idle")
    elif result['status'] == 'approval_required':
        # Nested approval currently unsupported but keep status consistent
        set_thread_status(thread_id, "waiting_approval")
    else:
        set_thread_status(thread_id, "idle")
        
    return redirect(url_for('view_thread', thread_id=thread_id))

@app.route('/thread/<int:thread_id>/retry', methods=['POST'])
def retry_last(thread_id):
    # Simple retry: delete last system error if exists, re-run Orchestrator loop based on last user message?
    # Strategy: Get last user message. Instantiate Orchestrator with a flag to "retry" which effectively 
    # just means re-running the logic. But we need to remove any partial failures.
    # For this assignment, we will just re-trigger the planner loop from the current state.
    
    # Check if last message was user
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=import_extras().RealDictCursor)
    cur.execute("SELECT role, content FROM messages WHERE thread_id = %s ORDER BY created_at DESC LIMIT 1", (thread_id,))
    last_msg = cur.fetchone()
    conn.close()
    
    if not last_msg:
         return redirect(url_for('view_thread', thread_id=thread_id))

    orch = Orchestrator(thread_id)
    orch.stream_tokens = False
    set_thread_status(thread_id, "running")

    # If the last response was from the planner, reuse its TOOL_INTENT so the
    # tool runner can evaluate the pending commands without waiting for another
    # user input.
    if last_msg['role'] == 'planner':
        intent = orch._extract_tag(last_msg['content'], "TOOL_INTENT") if last_msg['content'] else None
        if not intent or intent.lower().strip() == "none":
            flash("Last planner message did not include a runnable tool intent.", "info")
            set_thread_status(thread_id, "idle")
            return redirect(url_for('view_thread', thread_id=thread_id))
        try:
            result = orch.run_tool_loop(intent, hitl=False, continuous_intent=True)
        except Exception:
            set_thread_status(thread_id, "idle")
            raise
    else:
        # Otherwise, fall back to rerunning the planner loop using the current
        # conversation state.
        try:
            result = orch.run_planner_loop(use_tools=True, hitl=False, continuous_intent=True)
        except Exception:
            set_thread_status(thread_id, "idle")
            raise
    
    if result['status'] == 'error':
        flash(f"Retry Error: {result.get('error')}", "danger")
        set_thread_status(thread_id, "idle")
    elif result['status'] == 'approval_required':
        set_thread_status(thread_id, "waiting_approval")
    else:
        set_thread_status(thread_id, "idle")

    return redirect(url_for('view_thread', thread_id=thread_id))

@app.route('/thread/<int:thread_id>/fork', methods=['POST'])
def fork_thread(thread_id):
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Get original
    cur.execute("SELECT cwd, context_summary FROM threads WHERE id = %s", (thread_id,))
    orig = cur.fetchone()
    
    # Create new
    cur.execute("INSERT INTO threads (cwd, context_summary) VALUES (%s, %s) RETURNING id", (orig[0], orig[1]))
    new_id = cur.fetchone()[0]
    
    # Copy messages
    cur.execute("SELECT role, content FROM messages WHERE thread_id = %s ORDER BY created_at ASC", (thread_id,))
    msgs = cur.fetchall()
    for role, content in msgs:
        cur.execute("INSERT INTO messages (thread_id, role, content) VALUES (%s, %s, %s)", (new_id, role, content))
        
    conn.close()
    return redirect(url_for('view_thread', thread_id=new_id))

@app.route('/thread/<int:thread_id>/delete', methods=['POST'])
def delete_thread(thread_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT cwd FROM threads WHERE id = %s", (thread_id,))
    row = cur.fetchone()
    workspace = row[0] if row else None
    cur.execute("DELETE FROM threads WHERE id = %s", (thread_id,))
    cur.close()
    conn.close()
    PENDING_APPROVALS.pop(thread_id, None)
    clear_tokens(thread_id)
    clear_thread_status(thread_id)
    if workspace and workspace.startswith("/tmp/thread_"):
        shutil.rmtree(workspace, ignore_errors=True)
    flash(f"Thread #{thread_id} deleted.", "info")
    return redirect(url_for('index'))


@app.route('/thread/<int:thread_id>/download')
def download_thread(thread_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=import_extras().RealDictCursor)
    cur.execute("SELECT cwd FROM threads WHERE id = %s", (thread_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        flash("Thread not found.", "danger")
        return redirect(url_for('index'))
    cwd = row['cwd']
    if not cwd or not os.path.isdir(cwd):
        flash("Working directory is unavailable.", "danger")
        return redirect(url_for('view_thread', thread_id=thread_id))
    export_dir = tempfile.mkdtemp(prefix="thread_export_", dir="/tmp")
    zip_base = os.path.join(export_dir, f"thread_{thread_id}")
    archive_path = shutil.make_archive(zip_base, "zip", root_dir=cwd)

    @after_this_request
    def cleanup(response):
        shutil.rmtree(export_dir, ignore_errors=True)
        return response

    return send_file(
        archive_path,
        as_attachment=True,
        download_name=f"thread_{thread_id}.zip",
    )

# --- Admin Routes ---

@app.route('/admin/llm')
def admin_llm():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=import_extras().RealDictCursor)
    
    cur.execute("SELECT * FROM llm_backends ORDER BY id ASC")
    backends = cur.fetchall()
    
    active_backend = get_active_backend()
    
    planner_config = None
    runner_config = None
    models = []
    error = None
    
    if active_backend:
        # Get configs
        cur.execute("SELECT * FROM model_configs WHERE backend_id = %s", (active_backend['id'],))
        configs = {row['role']: row for row in cur.fetchall()}
        planner_config = configs.get('planner')
        runner_config = configs.get('tool_runner')
        
        # Try listing models
        try:
            models = list_models(active_backend)
        except LLMError as e:
            error = str(e.message)

    cur.close()
    conn.close()
    
    return render_template('llm_admin.html', 
                           backends=backends, 
                           active_backend=active_backend,
                           planner_config=planner_config,
                           runner_config=runner_config,
                           models=models,
                           error=error)

@app.route('/admin/llm/backend', methods=['POST'])
def save_backend():
    name = request.form['name']
    base_url = request.form['base_url']
    api_key = request.form['api_key']
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO llm_backends (name, base_url, api_key) VALUES (%s, %s, %s)", (name, base_url, api_key))
    conn.close()
    return redirect(url_for('admin_llm'))

@app.route('/admin/llm/select-backend', methods=['POST'])
def select_backend():
    backend_id = request.form['backend_id']
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE llm_backends SET is_active = FALSE")
    cur.execute("UPDATE llm_backends SET is_active = TRUE WHERE id = %s", (backend_id,))
    conn.close()
    return redirect(url_for('admin_llm'))

@app.route('/admin/llm/model-config', methods=['POST'])
def save_model_config():
    backend_id = request.form['backend_id']
    role = request.form['role']
    model_name = request.form['model_name']
    
    # Build params json
    params = {
        "temperature": float(request.form.get('temperature', 0.7)),
        "max_tokens": int(request.form.get('max_tokens', 2000)),
        "top_p": float(request.form.get('top_p', 0.9))
    }
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO model_configs (backend_id, role, model_name, parameters)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (backend_id, role) 
        DO UPDATE SET model_name = EXCLUDED.model_name, parameters = EXCLUDED.parameters
    """, (backend_id, role, model_name, json.dumps(params)))
    conn.close()
    return redirect(url_for('admin_llm'))

def import_extras():
    import psycopg2.extras
    return psycopg2.extras

if __name__ == '__main__':
    print("Waiting for DB...")
    wait_for_db()
    print("Initializing DB...")
    init_db()
    print("Starting app...")
    app.run(host='0.0.0.0', port=5000, threaded=True)
