# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

MCP Chat is a command-line chat client for the Anthropic API, built on the Model Context Protocol (MCP). The CLI talks to Claude and to one or more MCP servers, and additionally gives Claude a set of **local tools** (Anthropic's built-in "learned" tool schemas plus a custom Playwright browser tool). It began as a learning/tutorial project (a Skilljar submodule) but has since been rewired to connect to an external **n8n** MCP server over HTTP instead of the original bundled stdio document server.

## Commands

Run the app (**from the root project folder**, not from `core/`):

```bash
python main.py
```

Requires two environment variables (both read from the shell — the app does not load `.env`):

```bash
export ANTHROPIC_API_KEY=...   # in practice lives in ~/.bashrc
export N8N_MCP_TOKEN=...        # n8n Bearer token
```

Smoke-test the MCP client against the n8n server standalone (connects, lists tools, exits):

```bash
python mcp_client.py
```

Connect additional stdio MCP servers by passing their scripts as argv: `python main.py path/to/other_server.py`.

One-time setup for the browser tool (headless Chromium via Playwright — `pip` installs the package but not the browser binary or its OS libraries):

```bash
pip install -r requirements.txt
playwright install chromium
sudo playwright install-deps chromium   # Linux: OS libraries the browser needs (e.g. libmanette)
```

See **`SETUP.md`** for the full environment setup (Python deps, browser system libraries, environment variables, running).

There are **no tests, linters, or type checks** configured. Sanity-check edits with `python -m py_compile` and an import smoke test (`PYTHONPATH=.. python -c "import core.chat"`).

## Runtime configuration

- `ANTHROPIC_API_KEY` — read from the environment. `main.py` keeps an explicit `os.getenv("ANTHROPIC_API_KEY")` reference on purpose (the user runs a global key from `~/.bashrc`); **do not remove it** even though `core/claude.py` also constructs its own `Anthropic()` that reads the same env var.
- `N8N_MCP_TOKEN` — Bearer token for the n8n MCP server. Sent as `Authorization: Bearer <token>`.
- `N8N_MCP_URL` — optional override of the n8n endpoint (default `http://192.168.2.12:5678/mcp-server/http`, Streamable HTTP).
- The Claude model comes from `config.toml` (`[claude] model`), overridable by the `CLAUDE_MODEL` env var (default `claude-sonnet-5`). **`.env` is not loaded by the app** — the old `.env` `CLAUDE_MODEL` / `USE_UV` variables were never wired up and have been removed.
- The 2026 web-tool schemas (`web_search_20260209` / `web_fetch_20260209`) need a current `anthropic` SDK to parse the server-tool result blocks (`pip install -U anthropic`).
- Python 3.10+ (`pyproject.toml`).

## Architecture

Request flow: **CLI input → Chat.run() agentic loop → Claude API + (local tools | n8n MCP tools)**.

- **`main.py`** — entrypoint (in the repo root). Reads the API key, builds a `Claude` service, opens the n8n MCP client over Streamable HTTP via `build_primary_client()` (plus any stdio servers passed as argv), registers `browser.shutdown` on the `AsyncExitStack`, wires everything into a `Chat`, and runs the `CliApp` loop.

- **`core/chat.py`** (`Chat`) — the agentic loop. Each turn it calls Claude with the merged tool set (`learned.TOOLS + browser.TOOLS + ToolManager.get_all_tools(...)`). While `stop_reason == "tool_use"` it routes each `tool_use` block — local executor if `claude_learned_schemas.handles()` or `browser.handles()`, otherwise `ToolManager.execute_blocks()` (n8n/MCP) — feeds the results back, and loops. `stop_reason == "pause_turn"` (server-side web tools mid-run) is handled by resending. Capped at `MAX_TOOL_ITERATIONS`.

- **`core/claude_learned_schemas.py`** — Anthropic's built-in ("learned") tools. `bash` (`bash_20250124`) and the text editor (`text_editor_20250728` / `str_replace_based_edit_tool`) are **client-executed** here; `web_search` (`web_search_20260209`) and `web_fetch` (`web_fetch_20260209`) are **server-executed** by Anthropic (declaration only, no local handler). `handles()` reports the two client-side names; `execute()` runs them off the event loop via `asyncio.to_thread`.

- **`core/browser.py`** — a custom **Playwright** browser tool (`browser_navigate` / `_extract` / `_click` / `_fill`). Fully custom schemas (Claude learns them from descriptions). A single headless page is kept alive across calls (lazy-launched — `playwright` is imported only on first use, so the module imports fine without it), and each tool trims its output to avoid context bloat. `shutdown()` closes the browser on exit.

- **`core/claude.py`** (`Claude`) — thin Anthropic SDK wrapper. `chat()` builds request params (max_tokens 8000, optional thinking/tools/system); helpers append user/assistant messages and extract text blocks.

- **`core/tools.py`** (`ToolManager`) — the MCP↔Anthropic bridge (n8n tools only). `get_all_tools` aggregates tool schemas across all MCP clients; `execute_blocks` executes a list of `tool_use` blocks against the owning client; `execute_tool_requests` is a thin wrapper over it.

- **`core/cli.py`** (`CliApp`) — a minimal `prompt_toolkit` REPL (history + styling). Delegates all real work to `Chat`.

- **`mcp_client.py`** (`MCPClient`) — async context-manager over an MCP `ClientSession`, supporting three transports: `stdio` (spawn `command`+`args`), `sse` (`url`), and `http` (Streamable HTTP `url`) — the last two accept `headers` for auth (e.g. n8n's Bearer token). Exposes `list_tools`, `call_tool`, `list_prompts`, `get_prompt`, `read_resource`.

### Key conventions

- **Two parallel tool systems.** MCP tools live on remote servers (n8n) and are discovered/executed via `ToolManager`. Local tools (learned schemas + browser) are declared and executed in-process. `Chat` merges both into one `tools=` list and routes execution by owner.
- **"Learned" vs custom.** `claude_learned_schemas.py` holds Anthropic-defined tools Claude already knows (no descriptions needed). `browser.py` holds a fully custom tool Claude learns at runtime from its descriptions. Keep these separate from `tools.py`, which is strictly the MCP bridge.
- **`web_search` (discovery) and the browser tool (navigate/interact) are complementary**, not redundant — don't reimplement search inside Playwright.
- Add an MCP server by passing its script as argv (stdio) or adding another `MCPClient(...)` in `main.py` (e.g. `transport="http"` for another HTTP server). Its tools then appear to Claude automatically.
- `bash` is **stateless between calls** (fresh subprocess each time — `cd`/env don't persist). The browser page **is** stateful within a session.
- **No approval gating** — Claude executes whatever bash commands, file edits, browser actions, and n8n tools it chooses. This is intended for local dev only.
- The app must run from the **repo root** (`main.py` and `mcp_client.py` live there; `core/` is the importable subpackage).

### Removed from the original tutorial

`mcp_server.py` (the bundled stdio document server) and `core/cli_chat.py` (the `@mention` / `/command` document-resource layer) have been **deleted** — that whole `docs://documents` resource/prompt system was tutorial scaffolding and is gone. Don't reintroduce a `doc_client`.
