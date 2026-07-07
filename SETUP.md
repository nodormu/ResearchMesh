# Setup

Environment setup for MCP Chat. Run all commands from the **project root** (the
folder containing `main.py`), not from `core/`.

## Prerequisites

- Python 3.10+
- A running **n8n** instance exposing an MCP Server Trigger over Streamable HTTP,
  reachable on your network (default `http://192.168.2.12:5678/mcp-server/http`).
- `ANTHROPIC_API_KEY` and `N8N_MCP_TOKEN` available in your shell.

## 1. Python dependencies

```bash
pip install -r requirements.txt
```

Use a current `anthropic` SDK — the 2026 `web_search`/`web_fetch` tool schemas
need it to parse the server-tool result blocks (`pip install -U anthropic`).

## 2. Browser tool (Playwright)

The custom browser tool (`core/browser.py`) drives a **headless Chromium** via
Playwright. `pip` installs the Python package but **not** the browser binary or
its OS-level libraries.

```bash
playwright install chromium              # download the headless browser binary
sudo playwright install-deps chromium    # Linux: install the OS libraries it needs
```

Notes:

- `playwright install` with **no browser name** fetches all three engines
  (Chromium, Firefox, WebKit). This app only launches **Chromium**, so the
  `chromium` argument is sufficient.
- The `install-deps` step is what pulls in system libraries such as
  `libmanette` (a WebKit dependency). Equivalent manual routes you may see:
  - `npx playwright install-deps` (Node)
  - `sudo apt install libmanette-0.2-0 libmanette-0.2-dev` (only needed if you
    installed WebKit)

## 3. Environment variables

The app reads these from the shell — it does **not** load a `.env` file:

```bash
export ANTHROPIC_API_KEY=...   # commonly kept in ~/.bashrc
export N8N_MCP_TOKEN=...         # n8n Bearer token
export N8N_MCP_URL=...           # optional: override the n8n endpoint
```

These must be exported in the environment of the **user account you launch the
app as** — `main.py` calls `os.getenv(...)` directly and does not read a `.env`
file. To persist them across sessions, add the `export` lines above to that
user's shell startup file:

- `~/.bashrc` — interactive non-login shells (the usual case for a local terminal)
- `~/.bash_profile` or `~/.profile` — login shells (e.g. SSH sessions)

After editing, open a fresh shell (or run `source ~/.bashrc`) so the variables
are exported *before* you launch. Verify without revealing the values:

```bash
echo "key: ${ANTHROPIC_API_KEY:+set}  token: ${N8N_MCP_TOKEN:+set}"
```

## 4. Run

```bash
python main.py            # the interactive chat REPL

python mcp_client.py      # smoke-test the n8n connection (lists tools, then exits)
```

Connect additional stdio MCP servers by passing their scripts as arguments:
`python main.py path/to/other_server.py`.

Exit the REPL with **Ctrl-C** — this closes the headless browser and the n8n
connection cleanly.

## Notes

- The Claude model is hardcoded in `main.py` (`claude_model = "claude-sonnet-5"`).
- There are no tests, linters, or type checks. Sanity-check edits with
  `python -m py_compile` and an import smoke test
  (`PYTHONPATH=.. python -c "import core.chat"`).

## Built and tested on

Reference environment this project was developed and run on:

| Component  | Version |
|------------|---------|
| OS         | Ubuntu 26.04 LTS (kernel 7.0.0-27-generic) |
| Python     | 3.14.4 |
| Node.js    | v24.16.0 — **compiled from source** (installed under `~/nodejs`, not via apt) |
| npm        | 11.17.0 |
| Playwright | 1.61.0 (Chromium) |

Notes:

- `pyproject.toml` only requires Python 3.10+; 3.14 is simply what it was run on.
- Node.js was built from source rather than installed via `apt`, so `node`/`npm`
  live under `~/nodejs/bin` (on `PATH`) instead of `/usr/bin`. Node is needed
  **only** for the optional MCP Inspector — see the README.
- The `sudo playwright install-deps` step above assumes a Debian/Ubuntu `apt`
  system.

