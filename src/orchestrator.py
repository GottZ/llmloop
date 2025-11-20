import re
import subprocess
import shlex
import os
import logging
import uuid
from db import get_db_connection
from llm_client import call_llm, LLMError
from prompts import PLANNER_SYSTEM_PROMPT, TOOL_RUNNER_SYSTEM_PROMPT
from psycopg2.extras import RealDictCursor
from token_stream import push_token

PLANNER_HISTORY_LIMIT = 6

logger = logging.getLogger(__name__)

class Orchestrator:
    def __init__(self, thread_id):
        self.thread_id = thread_id
        self.conn = get_db_connection()
        self._load_thread_context()
        self.stream_tokens = False

    def _load_thread_context(self):
        with self.conn.cursor() as cur:
            cur.execute("SELECT cwd, context_summary FROM threads WHERE id = %s", (self.thread_id,))
            row = cur.fetchone()
            self.cwd = row[0] if row and row[0] else "/tmp"
            self.context_summary = row[1] if row else ""

    def _update_summary(self, new_summary):
        with self.conn.cursor() as cur:
            cur.execute("UPDATE threads SET context_summary = %s WHERE id = %s", (new_summary, self.thread_id))
        self.context_summary = new_summary

    def _save_message(self, role, content):
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO messages (thread_id, role, content) VALUES (%s, %s, %s)", 
                        (self.thread_id, role, content))

    def _fetch_messages(self, limit=None):
        query = "SELECT role, content FROM messages WHERE thread_id = %s ORDER BY created_at DESC"
        params = [self.thread_id]
        if limit:
            query += " LIMIT %s"
            params.append(limit)

        with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
        return list(reversed(rows))

    def _get_history(self, limit=None):
        """Return chat history mapped to roles the LLM API understands."""
        formatted = []
        rows = self._fetch_messages(limit)
        for row in rows:
            role = row['role']
            content = row['content']
            if role == 'user':
                mapped_role = 'user'
            elif role == 'planner':
                mapped_role = 'assistant'
            elif role in ('tool_runner', 'tool_context'):
                mapped_role = 'system'
            else:
                mapped_role = 'system'
            formatted.append({"role": mapped_role, "content": content})
        return formatted

    def _save_tool_run(self, command, stdout, stderr, exit_code):
        with self.conn.cursor() as cur:
            cur.execute("""
                INSERT INTO tool_runs (thread_id, command, output_stdout, output_stderr, exit_code)
                VALUES (%s, %s, %s, %s, %s)
            """, (self.thread_id, command, stdout, stderr, exit_code))

    def process_user_message(self, user_text, use_tools=True, hitl=False, continuous_intent=True, stream_tokens=False):
        self._save_message("user", user_text)
        self.stream_tokens = stream_tokens
        return self.run_planner_loop(use_tools, hitl, continuous_intent)

    def run_planner_loop(self, use_tools, hitl, continuous_intent=True):
        try:
            # 1. Call Planner
            messages = [{"role": "system", "content": PLANNER_SYSTEM_PROMPT}]
            if self.context_summary:
                messages.append({"role": "system", "content": f"Current Context: {self.context_summary}"})
            messages.extend(self._get_history(limit=PLANNER_HISTORY_LIMIT))
            
            planner_response = self._call_llm("planner", messages)
            self._save_message("planner", planner_response)

            # 2. Parse Planner Tags
            intent = self._extract_tag(planner_response, "TOOL_INTENT")
            summary = self._extract_tag(planner_response, "CONTEXT_SUMMARY")
            
            if summary:
                self._update_summary(summary)

            if not use_tools or self._intent_means_none(intent):
                return {"status": "complete"}

            # 3. Tool Runner Loop
            return self.run_tool_loop(intent, hitl, continuous_intent)

        except LLMError as e:
            logger.error(f"LLM Error: {e}")
            self._save_message("system", f"Error calling LLM: {e.message}")
            return {"status": "error", "error": str(e)}

    def run_tool_loop(self, intent, hitl, continuous_intent=True):
        tool_history = [
            {"role": "system", "content": TOOL_RUNNER_SYSTEM_PROMPT},
            {"role": "user", "content": f"Planner Intent: {intent}\nWorking Directory: {self.cwd}"}
        ]

        while True:
            # Call Tool Runner
            try:
                runner_resp = self._call_llm("tool_runner", tool_history)
            except LLMError as e:
                 self._save_message("system", f"Tool Runner LLM Error: {e.message}")
                 return {"status": "error", "error": str(e)}
            
            # Check for RESULT
            if "RESULT:" in runner_resp:
                result_text = runner_resp.split("RESULT:", 1)[1].strip()
                self._save_message("tool_runner", f"TOOL_RESULT:\n{result_text}")
                # Final Planner pass to synthesize
                return self.run_final_planner_pass(hitl, continuous_intent)

            # Parse RUN commands
            commands = [line.replace("RUN:", "").strip() for line in runner_resp.splitlines() if line.strip().startswith("RUN:")]
            
            if not commands:
                # Fallback if model is confused
                self._save_message("system", "Tool Runner failed to produce valid commands or result.")
                return {"status": "error", "error": "Invalid tool runner output"}

            # HITL Check
            if hitl:
                # We need to pause here.
                # Store the tool_history state implies complexity. 
                # Simplification: We save the runner_resp to messages (so we know what happened)
                # but we return a status that asks the UI to confirm.
                # Actually, let's execute the commands but in "dry run" -> wait for approval logic.
                return {
                    "status": "approval_required", 
                    "commands": commands, 
                    "tool_history": tool_history,
                    "runner_resp": runner_resp,
                    "continuous_intent": continuous_intent
                }

            # Execute Commands
            tool_outputs = self._execute_commands(commands)
            self._record_tool_outputs(tool_outputs)
            
            # Feed back to Tool Runner
            tool_history.append({"role": "assistant", "content": runner_resp})
            tool_history.append({"role": "user", "content": f"Tool Outputs:\n{tool_outputs}"})

    def resume_tool_loop_after_approval(self, tool_history, runner_resp, commands, continuous_intent=True):
        # Execute the commands that were pending
        tool_outputs = self._execute_commands(commands)
        self._record_tool_outputs(tool_outputs)
        
        # Update history
        tool_history.append({"role": "assistant", "content": runner_resp})
        tool_history.append({"role": "user", "content": f"Tool Outputs:\n{tool_outputs}"})
        
        # Resume loop (recursion or iterative - doing recursive call to run_tool_loop logic part)
        # Better to refactor, but for this script, we duplicate the loop logic slightly or jump back in.
        # We will call a helper that takes the history.
        return self._continue_tool_loop(tool_history, continuous_intent)

    def _continue_tool_loop(self, tool_history, continuous_intent=True):
         while True:
            try:
                runner_resp = self._call_llm("tool_runner", tool_history)
            except LLMError as e:
                 self._save_message("system", f"Tool Runner LLM Error: {e.message}")
                 return {"status": "error", "error": str(e)}
            
            if "RESULT:" in runner_resp:
                result_text = runner_resp.split("RESULT:", 1)[1].strip()
                self._save_message("tool_runner", f"TOOL_RESULT:\n{result_text}")
                return self.run_final_planner_pass(hitl=False, continuous_intent=continuous_intent)

            commands = [line.replace("RUN:", "").strip() for line in runner_resp.splitlines() if line.strip().startswith("RUN:")]
            if not commands:
                 return {"status": "error", "error": "Invalid tool runner output"}
            
            # Note: resuming DOES NOT support HITL again in this simple version to avoid infinite recursion depth issues easily
            # or simply pass hitl=False.
            tool_outputs = self._execute_commands(commands)
            self._record_tool_outputs(tool_outputs)
            tool_history.append({"role": "assistant", "content": runner_resp})
            tool_history.append({"role": "user", "content": f"Tool Outputs:\n{tool_outputs}"})

    def run_final_planner_pass(self, hitl=False, continuous_intent=True):
        # Let planner see the result
        messages = [{"role": "system", "content": PLANNER_SYSTEM_PROMPT}]
        if self.context_summary:
             messages.append({"role": "system", "content": f"Context: {self.context_summary}"})
        messages.extend(self._get_history(limit=PLANNER_HISTORY_LIMIT))
        # The tool result was already saved to history
        
        try:
            final_resp = self._call_llm("planner", messages)
            self._save_message("planner", final_resp)
            
            summary = self._extract_tag(final_resp, "CONTEXT_SUMMARY")
            if summary:
                self._update_summary(summary)

            followup_intent = self._extract_tag(final_resp, "TOOL_INTENT")
            if followup_intent and not self._intent_means_none(followup_intent) and continuous_intent:
                return self.run_tool_loop(followup_intent, hitl, continuous_intent)
            
            return {"status": "complete"}
        except LLMError as e:
            self._save_message("system", f"Error in final planner pass: {e.message}")
            return {"status": "error", "error": str(e)}

    def _execute_commands(self, commands):
        output_buffer = []
        if not os.path.exists(self.cwd):
            os.makedirs(self.cwd, exist_ok=True)
            
        for cmd in commands:
            stripped = cmd.strip()
            if stripped.lower().startswith("history "):
                output = self._handle_history_command(stripped)
                stdout = output or ""
                stderr = ""
                annotation = self._relative_path_annotation(cmd)
                output_buffer.append(self._format_command_output(cmd, 0, stdout, stderr, annotation=annotation))
                continue
            try:
                # Run command
                proc = subprocess.run(
                    cmd, shell=True, cwd=self.cwd, 
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30
                )
                self._save_tool_run(cmd, proc.stdout, proc.stderr, proc.returncode)
                annotation = self._relative_path_annotation(cmd)
                output_buffer.append(self._format_command_output(cmd, proc.returncode, proc.stdout, proc.stderr, annotation=annotation))
            except Exception as e:
                self._save_tool_run(cmd, "", str(e), -1)
                annotation = self._relative_path_annotation(cmd)
                output_buffer.append(self._format_command_output(cmd, -1, "", str(e), error_message=str(e), annotation=annotation))
        return "\n".join(output_buffer)

    def _extract_tag(self, text, tag):
        pattern = f"<{tag}>(.*?)</{tag}>"
        match = re.search(pattern, text, re.DOTALL)
        return match.group(1).strip() if match else None

    def _record_tool_outputs(self, tool_outputs):
        if not tool_outputs:
            return
        self._save_message("tool_context", f"TOOL_CONTEXT:\n{tool_outputs}")

    def _intent_means_none(self, intent):
        if not intent:
            return True
        cleaned = intent.strip().lower()
        cleaned = cleaned.rstrip(".! ")
        cleaned = cleaned.replace("-", " ").strip()
        synonyms = {
            "none",
            "no tools",
            "no tool",
            "no tool needed",
            "no tooling",
            "nothing",
            "no further action",
            "no further actions",
        }
        return cleaned in synonyms

    def _call_llm(self, role, messages):
        stream = getattr(self, "stream_tokens", False)
        callback = self._token_callback(role) if stream else None
        return call_llm(role, messages, stream=stream, token_callback=callback)

    def _token_callback(self, role):
        def _inner(chunk):
            if chunk:
                push_token(self.thread_id, role, chunk)
        return _inner

    def _format_command_output(self, cmd, exit_code, stdout, stderr, error_message=None, annotation=None):
        stdout_box = self._wrap_stream("STDOUT", stdout or "")
        stderr_box = self._wrap_stream("STDERR", stderr or "")
        parts = [
            f"CMD: {cmd}",
            f"EXIT: {exit_code}",
            "STDOUT:",
            stdout_box,
            "STDERR:",
            stderr_box,
        ]
        if error_message:
            parts.append(f"ERROR: {error_message}")
        if annotation:
            parts.append(annotation)
        return "\n".join(parts)

    def _wrap_stream(self, label, content):
        token = uuid.uuid4().hex
        boundary = f"@@{label}_{token}@@"
        if not content.endswith("\n"):
            content = content + "\n" if content else ""
        return f"{boundary}\n{content}{boundary}"

    def _relative_path_annotation(self, cmd):
        try:
            tokens = shlex.split(cmd)
        except ValueError:
            return None
        if not tokens:
            return None
        if tokens[0] != "ls":
            return None
        targets = [tok for tok in tokens[1:] if not tok.startswith("-")]
        if not targets:
            return None
        lines = []
        for rel_target in targets:
            if rel_target.startswith("/"):
                # absolute path already explicit
                continue
            abs_target = os.path.normpath(os.path.join(self.cwd, rel_target))
            if not os.path.exists(abs_target):
                continue
            if os.path.isdir(abs_target):
                try:
                    entries = sorted(os.listdir(abs_target))
                except OSError:
                    continue
                lines.append(f"{rel_target.rstrip('/')}/:")
                max_entries = 200
                for entry in entries[:max_entries]:
                    lines.append(f"  {rel_target.rstrip('/')}/{entry}")
                if len(entries) > max_entries:
                    lines.append("  ...")
            else:
                lines.append(rel_target)
        if lines:
            return "RELATIVE_PATHS:\n" + "\n".join(lines)
        return None

    def _handle_history_command(self, command):
        tokens = shlex.split(command)
        if len(tokens) < 2:
            return "Invalid history command. Use 'history summarize' or 'history filter'."
        if tokens[0].lower() != "history":
            return "History commands must start with the 'history' keyword."

        action = tokens[1].lower()
        args = {"chunk": 10, "role": None, "limit": None, "contains": None}
        for token in tokens[2:]:
            if "=" in token:
                key, value = token.split("=", 1)
                args[key] = value

        if action == "summarize":
            chunk = self._safe_int(args.get("chunk"), default=10, minimum=1)
            role = args.get("role")
            return self._summarize_history(chunk, role)
        if action == "filter":
            role = args.get("role")
            limit = self._safe_int(args.get("limit"), minimum=1)
            contains = args.get("contains")
            return self._filter_history(role, limit, contains)
        return "Unknown history action."

    def _summarize_history(self, chunk_size, role_filter=None):
        rows = self._fetch_messages()
        if role_filter:
            rows = [row for row in rows if row['role'] == role_filter]
        if not rows:
            return "No history available for summarization."

        chunks = [rows[i:i + max(1, chunk_size)] for i in range(0, len(rows), max(1, chunk_size))]
        summaries = []
        for idx, chunk in enumerate(chunks, start=1):
            lines = [f"Chunk {idx} ({len(chunk)} messages):"]
            for msg in chunk:
                snippet = (msg['content'] or "").strip().replace("\n", " ")
                if len(snippet) > 160:
                    snippet = snippet[:157] + "..."
                lines.append(f"- {msg['role']}: {snippet}")
            summaries.append("\n".join(lines))
        return "\n\n".join(summaries)

    def _filter_history(self, role_filter=None, limit=None, contains=None):
        rows = self._fetch_messages()
        if role_filter:
            rows = [row for row in rows if row['role'] == role_filter]
        if contains:
            rows = [row for row in rows if contains in (row['content'] or "")]
        if limit:
            rows = rows[-limit:]
        if not rows:
            return "No history matched the provided filters."

        lines = []
        for idx, msg in enumerate(rows, start=1):
            lines.append(f"{idx}. {msg['role']}: {msg['content']}")
        return "\n".join(lines)

    def _safe_int(self, value, default=None, minimum=None):
        if value is None:
            return default
        try:
            num = int(value)
        except (TypeError, ValueError):
            return default
        if minimum is not None:
            num = max(minimum, num)
        return num
