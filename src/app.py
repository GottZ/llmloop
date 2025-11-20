import sys
import os
import json
from flask import Flask, render_template, request, redirect, url_for, flash, session
from config import Config
from db import init_db, wait_for_db, get_db_connection, get_active_backend
from orchestrator import Orchestrator
from llm_client import list_models, LLMError

# Fix import path for docker
sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")

app = Flask(__name__)
app.config.from_object(Config)

# Global state for HITL (simple in-memory storage for this demo)
# Key: thread_id, Value: {tool_history: [], runner_resp: "", commands: []}
PENDING_APPROVALS = {}

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
    cwd = request.form.get('cwd', '/tmp')
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO threads (cwd, context_summary) VALUES (%s, '') RETURNING id", (cwd,))
    thread_id = cur.fetchone()[0]
    conn.close()
    return redirect(url_for('view_thread', thread_id=thread_id))

@app.route('/thread/<int:thread_id>')
def view_thread(thread_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=import_extras().RealDictCursor)
    
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
    
    return render_template('thread.html', 
                           thread=thread, 
                           messages=messages, 
                           backend=backend,
                           pending_approval=pending_approval)

@app.route('/thread/<int:thread_id>/send', methods=['POST'])
def send_message(thread_id):
    user_input = request.form.get('content')
    use_tools = 'use_tools' in request.form
    hitl = 'hitl' in request.form
    continuous_intent = 'continuous_intent' in request.form
    
    orch = Orchestrator(thread_id)
    result = orch.process_user_message(user_input, use_tools, hitl, continuous_intent)
    
    if result['status'] == 'approval_required':
        pending = dict(result)
        pending['continuous_intent'] = continuous_intent
        PENDING_APPROVALS[thread_id] = pending
        flash("Approval required for tool execution.", "warning")
    elif result['status'] == 'error':
        flash(f"Error: {result.get('error')}", "danger")
        
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
    continuous_intent = pending.get('continuous_intent', True)
    result = orch.resume_tool_loop_after_approval(
        pending['tool_history'], pending['runner_resp'], pending['commands'], continuous_intent
    )
    
    if result['status'] == 'error':
        flash(f"Error: {result.get('error')}", "danger")
        
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

    # If the last response was from the planner, reuse its TOOL_INTENT so the
    # tool runner can evaluate the pending commands without waiting for another
    # user input.
    if last_msg['role'] == 'planner':
        intent = orch._extract_tag(last_msg['content'], "TOOL_INTENT") if last_msg['content'] else None
        if not intent or intent.lower().strip() == "none":
            flash("Last planner message did not include a runnable tool intent.", "info")
            return redirect(url_for('view_thread', thread_id=thread_id))
        result = orch.run_tool_loop(intent, hitl=False, continuous_intent=True)
    else:
        # Otherwise, fall back to rerunning the planner loop using the current
        # conversation state.
        result = orch.run_planner_loop(use_tools=True, hitl=False, continuous_intent=True)
    
    if result['status'] == 'error':
        flash(f"Retry Error: {result.get('error')}", "danger")

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
    app.run(host='0.0.0.0', port=5000)
