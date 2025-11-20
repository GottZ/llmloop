# LLM Loop Orchestrator

An experimental web UI (Flask) plus orchestration layer that lets you spin up “threads” where a Planner LLM collaborates with a Tool Runner LLM to operate on a filesystem via shell commands, persist context, and support human‑in‑the‑loop approvals.

## Highlights

- **Two-stage agent loop** – Planner decides tool intents; Tool Runner executes commands, streams outputs back, and can chain work automatically when “Continuous Intent” is enabled (`src/orchestrator.py`). The container ships with git, ripgrep/rg, tree, fd, sed, curl, apply_patch, python, node, npm, etc., so the agent has a rich POSIX toolbox.
- **Context compression** – Planner only sees a rolling context summary and the most recent messages; deeper history is fetched on-demand via `history summarize` / `history filter` commands.
- **Live UI & HITL** – The thread view streams messages/approvals via Server-Sent Events (SSE) while you type, and still supports HITL approvals, retries, and forks (`src/app.py`, `src/templates/thread.html`).
- **LLM backend management** – Admin screen for selecting/modifying backends and per-role model params (`src/templates/llm_admin.html`).
- **Postgres persistence** – Threads, messages, tool runs, and backend configs are stored relationally (`src/db.py`).
- **Optional token streaming** – Enable real-time token streaming per message to watch planner/tool-runner outputs appear before the final message is stored.

## Architecture Overview

| Component | Role |
| --- | --- |
| `Flask` app (`src/app.py`) | HTTP routes, SSE endpoints, HITL state, template rendering |
| `Orchestrator` (`src/orchestrator.py`) | Planner/Tool Runner loop, context summaries, history utilities, command execution, output sandboxing |
| `DB Layer` (`src/db.py`) | Connection helpers, schema migrations, default seeding |
| `LLM Client` (`src/llm_client.py`) | Calls `/chat/completions`, lists models, surfaces `LLMError`s |
| `Prompts` (`src/prompts.py`) | System prompts describing planner/tool-runner protocol, POSIX emphasis, boundary markers |
| `Templates` (`src/templates/*`) | Threads view (with SSE streaming), admin UI |

## Running Locally

```bash
# Start Postgres + Flask app
docker compose up --build
# The built-in Flask server runs with threading enabled so SSE endpoints
# don’t block other requests. For production, use a threaded/asynchronous WSGI server.

# App runs on http://localhost:5000
```

Environment variables (see `docker-compose.yml`):

- `DATABASE_URL` (auto-set inside compose)
- Optional for seeding: `OPENAI_API_KEY`, `OPENAI_BASE_URL`
- `FLASK_DEBUG`, `SECRET_KEY`

## Usage Flow

1. Visit `/` to create a thread (set working directory path on the host/container).
2. Open the thread to send messages. The UI streams new planner/tool outputs in real time via SSE, so you can keep the page open while the agent works.
3. Use the toggles to adjust behavior:
   - `Enable Tools` – allow the Planner to dispatch intents.
   - `Continuous Intent` – automatically continue tool execution if the Planner responds with another `<TOOL_INTENT>`.
   - `Human-in-the-loop` – require approval before shell commands run.
   - `Stream Tokens` – stream planner/tool-runner tokens live over SSE for incremental feedback (falls back to batched messages when unchecked).
4. When the Planner asks for tools, the Tool Runner executes `RUN:` commands and returns `TOOL_RESULT` or `TOOL_CONTEXT` entries. Approvals can be granted/denied via the UI banner, which updates live from the SSE feed.
5. Use `Retry Last Operation` to re-drive the last Planner intent or re-run the planner loop if needed.
6. Fork threads to branch from any point without losing history.

## Planner / Tool Runner Protocol

- Planner output must include `<TOOL_INTENT>` and `<CONTEXT_SUMMARY>`. Only the rolling summary + most recent messages are visible, so summaries must capture long-term state. The prompt specifically reminds the Planner to request POSIX commands instead of emitting “example code”.
- Tool Runner must alternate between:
  - `RUN: <command>` lines for shell execution.
  - `RESULT: <summary>` when work is complete.
- Special commands:
  - `history summarize chunk=10 role=user` – chunked summaries of past messages.
  - `history filter role=tool_context limit=5 contains=error` – targeted history slices.
  These are intercepted server-side; no actual shell command is run.
- Available CLI tools inside the container include git, curl, rg (ripgrep), tree, fd, sed, python, node, npm, gcc, apply_patch, and more—prefer these over fabricating code.
- Every shell command’s STDOUT/STDERR is wrapped in randomized boundary lines (e.g., `@@STDOUT_<token>@@ … @@STDOUT_<token>@@`) so tool output can contain arbitrary text—including the strings `RUN:` or `RESULT:`—without breaking the protocol. Both prompts explain how to strip/ignore the markers.
- Directory listings may include a `RELATIVE_PATHS` block that already prefixes each entry with the correct subdirectory; copy these relative paths into follow-up commands so context compression never drops directory information.

## Administering LLM Backends

Navigate to `/admin/llm` to:

1. Add/select an LLM backend (base URL + API key).
2. Configure Planner and Tool Runner models separately (model name, temperature, max tokens, top_p).
3. View the list of models reported by the backend’s `/models` endpoint.

Configurations persist in Postgres (`model_configs` table) and are consumed by runtime calls (`src/llm_client.py`).

## Database

Schemas (`init_db()`):

- `threads`, `messages`, `tool_runs`
- `llm_backends`, `model_configs`
- `schema_version` for migrations

On a fresh database with seeding env vars set, a default backend and configs (temperature 0.7, top_p 0.9) are inserted.

## Development Notes

- Requirements: see `src/requirements.txt`.
- Entry point: `python src/app.py`.
- The Dockerfile installs system deps for `psycopg2` (libpq, gcc).
- History commands and continuous intent logic live exclusively in the orchestrator; adjust there when changing agent behavior.

## Roadmap Ideas

- Stream tool output to the UI in real time instead of posting after completion.
- Add per-thread settings for history window size.
- Support parallel tool executions or richer tool catalogs beyond shell commands.
