# Linux CLI Research Client for Claude

> *Unofficial, community-built client — not affiliated with or endorsed by Anthropic. "Claude" is a trademark of Anthropic.*

A command-line chat client for the Anthropic API, built on the Model Context
Protocol (MCP). It runs an interactive terminal REPL against Claude and gives
Claude a broad tool set:

- **n8n workflow tools** — via an external n8n MCP server over Streamable HTTP
- **`bash`** and a **file editor** — Anthropic's built-in ("learned") tools, executed locally
- **`web_search`** and **`web_fetch`** — Anthropic's server-side web tools
- **browser automation** — a custom headless [Playwright](https://playwright.dev/) tool (`navigate` / `extract` / `click` / `fill`)

Claude decides which tool to use per request; the app routes each call to the
right place (local executor vs. the n8n MCP server) and loops until Claude
produces a final answer.

> This started as a tutorial project. The original bundled stdio document server
> and its `@mention` / `/command` features have been removed — it now connects
> to an external n8n instance instead.

## Requirements

- Python 3.10+
- A running **n8n** instance exposing an MCP Server Trigger over Streamable HTTP
- `ANTHROPIC_API_KEY` and `N8N_MCP_TOKEN` in your shell

## Setup

See **[SETUP.md](SETUP.md)** for the full walkthrough (including the browser's
OS-level libraries on Linux). Quick version, run from the project root:

```bash
pip install -r requirements.txt
playwright install chromium
sudo playwright install-deps chromium    # Linux only

export ANTHROPIC_API_KEY=...              # commonly kept in ~/.bashrc
export N8N_MCP_TOKEN=...                    # n8n Bearer token
```

The app reads these variables from the shell — it does **not** load a `.env`
file.

## Usage

Run from the project root (not `core/`):

```bash
python main.py
```

Then just type. Some prompts to exercise each tool:

- `Run the shell command uname -a and show the output.` → bash
- `Create /tmp/notes.txt containing "hello", then read it back.` → file editor
- `Search the web for the latest stable Python version and cite the source.` → web search
- `Open https://example.com and tell me the heading.` → browser
- *(anything your n8n tools expose, e.g.)* `List my n8n workflows.` → n8n

Exit with **Ctrl-C** (closes the headless browser and n8n connection cleanly).

Smoke-test just the n8n connection without launching the chat:

```bash
python mcp_client.py     # connects, lists tools, exits
```

## Configuration

Non-secret settings live in **`config.toml`** at the project root — the Claude
model and the n8n endpoint/toggle. The matching environment variable overrides
each value. Secrets are never stored in the file.

```toml
# config.toml
[claude]
model = "claude-sonnet-5"   # CLAUDE_MODEL env var overrides this

[n8n]
enabled = true   # false = skip n8n, run with local tools only
url = "http://192.168.2.12:5678/mcp-server/http"
```

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key (required) |
| `N8N_MCP_TOKEN` | n8n Bearer token (required, unless n8n is disabled) |
| `N8N_MCP_URL` | Override the `config.toml` n8n endpoint (optional) |
| `CLAUDE_MODEL` | Override the `config.toml` Claude model (optional) |

## Project layout

```
main.py                          entrypoint — wires the n8n client + Chat + REPL
mcp_client.py                    MCP client (stdio / SSE / Streamable HTTP transports)
requirements.txt                 Python dependencies
SETUP.md                         full environment setup
CLAUDE.md                        guidance for AI coding agents
core/
  chat.py                        agentic loop + tool routing
  claude.py                      thin Anthropic SDK wrapper
  tools.py                       MCP <-> Anthropic tool bridge (n8n)
  claude_learned_schemas.py      bash, file editor, web_search, web_fetch
  browser.py                     custom Playwright browser tool
  cli.py                         prompt_toolkit REPL
```

## Extending

- **Add another MCP server:** pass a stdio server script as an argument
  (`python main.py path/to/server.py`), or add another `MCPClient(...)` in
  `main.py` (e.g. `transport="http"` for another HTTP server). Its tools appear
  to Claude automatically.
- **Add a local tool:** put Anthropic-defined tools in
  `core/claude_learned_schemas.py`; put custom tools (like the browser) in their
  own module and register them in `core/chat.py`.

## Optional: MCP Inspector

The app is **pure Python — Node.js is not part of this project.** For interactively
debugging MCP servers (e.g. hand-calling the tools your n8n endpoint exposes), the
[MCP Inspector](https://github.com/modelcontextprotocol/inspector) can be launched
on demand with `npx` — no install step, and nothing in the repo depends on it:

```bash
npx @modelcontextprotocol/inspector@latest
```

(Requires Node.js/npm on your machine; this is an external debugging aid, not a
project dependency.)

## Notes

- **No approval gating** — Claude runs whatever bash commands, file edits,
  browser actions, and n8n tools it chooses. Intended for local development only.
- `bash` is stateless between calls (fresh subprocess each time); the browser
  page is stateful within a session.
- No tests, linters, or type checks are configured.
