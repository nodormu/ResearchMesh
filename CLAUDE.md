# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

ResearchMesh, a Linux CLI Research Client for Claude is a command-line chat client for the Anthropic API, built on the Model Context Protocol (MCP). The CLI talks to Claude and to one or more MCP servers, and additionally gives Claude **16 local tools** — Anthropic's built-in "learned" schemas (bash, text editor, web search/fetch) plus custom ones for DOM browsing, document conversion, stateful Python, interactive commands, config editing, SQL, and recoverable deletes. It began as a learning/tutorial project (a Skilljar submodule) but has since been rewired to connect to any number of external MCP servers over Streamable HTTP, declared as a list under `[mcp]` in `config.toml`, instead of the original bundled stdio document server.

## Commands

Run the app (**from the root project folder**, not from `core/`):

```bash
python main.py
```

Requires two environment variables (both read from the shell — the app does not load `.env`):

```bash
export ANTHROPIC_API_KEY=...   # in practice lives in ~/.bashrc
export N8N_MCP_TOKEN=...        # one per server, named by its token_env in config.toml
```

Check every configured MCP server standalone (connects to each, lists tools, reports failures, exits):

```bash
python mcp_client.py
```

Connect additional stdio MCP servers by passing their scripts as argv: `python main.py path/to/other_server.py`.

One-time setup for the browser tool (headless Chromium via Playwright — `pip` installs the package but not the browser binary or its OS libraries), plus the two system binaries `document_convert` shells out to:

```bash
pip install -r requirements.txt
playwright install chromium
sudo playwright install-deps chromium   # Linux: OS libraries the browser needs (e.g. libmanette)
sudo apt install libreoffice pandoc     # document_convert: soffice + the markdown path
```

Optional tool dependencies are imported **lazily, inside the tool that needs them**, so a missing package only breaks that one tool — it still gets declared to Claude and returns an install hint if used. To drop a tool entirely, remove its module from `MODULES` in `core/local_tools.py`.

See **`README.md`** for the full environment setup — the quick start is at the top, and the collapsed "Full setup detail" section covers browser system libraries, the optional per-tool packages, and environment variables. (`SETUP.md` was merged into it; the two duplicated ~60% of their content and drifted apart.)

There are **no tests, linters, or type checks** configured. Sanity-check edits with `python -m py_compile` and an import smoke test (`PYTHONPATH=.. python -c "import core.chat"`).

## Runtime configuration

- `ANTHROPIC_API_KEY` — read from the environment. `main.py` keeps an explicit `os.getenv("ANTHROPIC_API_KEY")` reference on purpose (the user runs a global key from `~/.bashrc`); **do not remove it** even though `core/claude.py` also constructs its own `Anthropic()` that reads the same env var.
- **MCP bearer tokens** — each `[mcp].servers` entry may set `token_env` naming the environment variable that holds its token; it is sent as `Authorization: Bearer <token>`. A server with no `token_env` connects unauthenticated. Tokens are never stored in `config.toml`.
- `CLAUDE_SHOW_USAGE` — set to `1` to print per-request token and prompt-cache counters (see `core/chat.py`). Prompt caching fails silently, so this is how you confirm the `cache_control` breakpoint is landing.
- The Claude model comes from `config.toml` (`[claude] model`), overridable by the `CLAUDE_MODEL` env var (default `claude-sonnet-5`). The app does **not** load a `.env` file — `ANTHROPIC_API_KEY` and any MCP tokens come from the shell environment (e.g. `~/.bashrc`).
- The 2026 web-tool schemas (`web_search_20260209` / `web_fetch_20260209`) need a current `anthropic` SDK to parse the server-tool result blocks (`pip install -U anthropic`).
- Python 3.11+ (`pyproject.toml`) — the floor is `tomllib`, used by `main.py`.

## Architecture

Request flow: **CLI input → Chat.run() agentic loop → Claude API + (local tools | MCP server tools)**.

- **`main.py`** — entrypoint (in the repo root). Reads the API key, builds a `Claude` service, connects every enabled `[mcp].servers` entry over Streamable HTTP via `build_client()` / `_connect_mcp_servers()` — a server that fails is reported and skipped rather than aborting startup — plus any stdio servers passed as argv, registers `local_tools.shutdown` on the `AsyncExitStack`, wires everything into a `Chat`, and runs the `CliApp` loop.

- **`core/chat.py`** (`Chat`) — the agentic loop, plus `SYSTEM_PROMPT`, sent as `system` on every request. That prompt exists because with tool schemas alone Claude describes capabilities it doesn't have (it invented a sandboxed `code_execution` container in testing); it states the two execution locations, that nothing is sandboxed, which tools are stateful, and how to choose between the overlapping ones. Keep it factual — if you add or remove a tool, update it. Each turn it calls Claude with the merged tool set (`local_tools.TOOLS + ToolManager.get_all_tools(...)`, the MCP half fetched once per turn). While `stop_reason == "tool_use"` it routes each `tool_use` block — `local_tools.execute()` first, then `ToolManager.execute_blocks()` (MCP) if no local module owns the name — feeds the results back, and loops. `stop_reason == "pause_turn"` (server-side web tools mid-run) is handled by resending. Capped at `MAX_TOOL_ITERATIONS`.

- **`core/local_tools.py`** — the registry of client-executed tools. Every module in `MODULES` exposes the same three names (`TOOLS`, `handles(name)`, `await execute(name, input)`), so a new tool is one new module plus one line here rather than edits to the chat loop's declaration list *and* its routing chain. Raises at import on duplicate tool names, and `shutdown()` releases everything the tools may have started (browser, kernel, DuckDB).

- **`core/claude_learned_schemas.py`** — Anthropic's built-in ("learned") tools. `bash` (`bash_20250124`) and the text editor (`text_editor_20250728` / `str_replace_based_edit_tool`) are **client-executed** here; `web_search` (`web_search_20260209`) and `web_fetch` (`web_fetch_20260209`) are **server-executed** by Anthropic (declaration only, no local handler). `handles()` reports the two client-side names; `execute()` runs them off the event loop via `asyncio.to_thread`.

- **`core/browser.py`** — a custom **Playwright** browser tool (`browser_navigate` / `_extract` / `_click` / `_fill` / `_links` / `_back`). Fully custom schemas (Claude learns them from descriptions). A single headless page is kept alive across calls (lazy-launched — `playwright` is imported only on first use, so the module imports fine without it), and each tool trims its output to avoid context bloat. `shutdown()` closes the browser on exit. The point of this tool is **DOM-based surfing**, so `browser_navigate` is described as the primary way to read the web and every page-changing call reports the current URL — there is deliberately no separate "current URL" tool. `_trim()` flattens newlines and is for prose only; element lists use `clip()` so their line structure survives.

- **`core/documents.py`** — `document_convert`: headless LibreOffice (`soffice --convert-to`) with a **throwaway `-env:UserInstallation` profile per call**, because LibreOffice locks its user profile and a second concurrent call otherwise fails silently. Markdown sources route through **pandoc** (soffice has no dependable markdown import); `md → pdf` goes md → odt (pandoc) → pdf (soffice), since pandoc's own PDF writer needs a LaTeX engine.

- **`core/kernel.py`** — `python`: a persistent IPython kernel over `jupyter_client`. The one thing `bash` structurally cannot do, since **state survives between calls**; it also covers plotting/data/symbolic work as plain imports instead of more tool slots. ANSI codes are stripped from tracebacks, inline images are reported but not returned (save to disk instead), and `restart: true` gives a clean namespace.

- **`core/processes.py`** — `interactive_run`: `pexpect` on a pty for commands that prompt (passwords, `[y/N]`, ssh host keys, installers, REPLs), which the `bash` tool cannot answer and simply hangs on. One tool taking a scripted list of expect/send steps rather than a stateful spawn/send/expect trio; `secret: true` redacts a response from the returned transcript.

- **`core/config_edit.py`** — `config_edit`: round-trip YAML (`ruamel.yaml`), TOML (`tomlkit`), and JSON edits that **preserve comments**, key order, and quoting, which `sed` and stdlib YAML silently destroy. Dotted key paths with `[index]` support, `$…` JSONPath for read-only queries (`jsonpath-ng`), and writes go through a temp file + `os.replace`.

- **`core/data.py`** — `sql_query`: DuckDB against CSV/Parquet/JSON files in place, no import step. One in-memory connection is reused for the session, so views and temp tables persist across calls.

- **`core/files.py`** — `trash`: `send2trash`, the only recoverable delete available here given there is no approval gate. Paths are made absolute (send2trash fails opaquely on relative ones) and `TrashPermissionError` is translated, since GIO refuses to trash from tmpfs mounts like `/tmp` and raises it with an empty message.

- **`core/output.py`** — `clip(text, limit)`, the one truncation helper the local tool modules share (bash/editor/kernel/pexpect budget 12000 chars, browser 6000).

- **`core/claude.py`** (`Claude`) — thin Anthropic SDK wrapper. Two things to know before editing `chat()`: **no sampling parameters** — current models reject a non-default `temperature`/`top_p`/`top_k` with a 400 and only accept the default, so sending one can only fail; and **no `budget_tokens`** — adaptive thinking replaced it (`{"type": "enabled", "budget_tokens": N}` is a 400 now), with `output_config={"effort": ...}` as the depth knob if ever needed. Also open: `stop_sequences=[]` is a mutable default argument and is sent empty on every request, and `max_tokens=8000` is shared by thinking *and* the reply — a `/think` turn on a hard problem can end in `stop_reason: "max_tokens"`; raise it (streaming is advisable much above ~16K). `chat()` builds request params (max_tokens 8000, optional thinking/tools/system); helpers append user/assistant messages and extract text blocks.

- **`core/tools.py`** (`ToolManager`) — the MCP↔Anthropic bridge (remote server tools only). `get_all_tools` aggregates tool schemas across all MCP clients (called once per user turn by `Chat`, not per tool-use iteration); `execute_blocks` executes a list of `tool_use` blocks against the owning client, resolving owners via one `_tool_owners` map per call.

- **`core/cli.py`** (`CliApp`) — a minimal `prompt_toolkit` REPL (history + styling). Delegates all real work to `Chat`.

- **`mcp_client.py`** (`MCPClient`) — async context-manager over an MCP `ClientSession`, supporting three transports: `stdio` (spawn `command`+`args`), `sse` (`url`), and `http` (Streamable HTTP `url`) — the last two accept `headers` for auth (e.g. a Bearer token). Exposes `list_tools`, `call_tool`, `list_prompts`, `get_prompt`, `read_resource`.

### Key conventions

- **Two parallel tool systems.** MCP tools live on remote servers (any number, listed under `[mcp]`) and are discovered/executed via `ToolManager`. Local tools are declared and executed in-process, aggregated by `local_tools`. `Chat` merges both into one `tools=` list and routes execution by owner.
- **"Learned" vs custom.** `claude_learned_schemas.py` holds Anthropic-defined tools Claude already knows (no descriptions needed). Every other local module holds fully custom tools Claude learns at runtime from its descriptions. Keep these separate from `tools.py`, which is strictly the MCP bridge.
- **A new local tool must beat `bash` at something structural** — statefulness (`python`), interactivity (`interactive_run`), a correctness guarantee (`config_edit`), recoverability (`trash`), or context economy — since Claude can already shell out to any CLI. Wrapping a command bash could run unaided just spends a tool slot.
- **Tool-selection accuracy degrades past roughly 30–50 loaded tools.** 16 local + whatever the connected MCP servers advertise leaves headroom; prefer one tool with a mode parameter (as `document_convert` and `config_edit` do) over one tool per variation.
- **`web_search` (discovery) and the browser tool (navigate/interact) are complementary**, not redundant — don't reimplement search inside Playwright.
- Add an MCP server by passing its script as argv (stdio) or adding another `MCPClient(...)` in `main.py` (e.g. `transport="http"` for another HTTP server). Its tools then appear to Claude automatically.
- `bash` is **stateless between calls** (fresh subprocess each time — `cd`/env don't persist); the `python` kernel, the browser page, and the DuckDB connection **are** stateful within a session.
- **No approval gating** — Claude executes whatever bash commands, file edits, browser actions, conversions, kernel code, interactive commands, and MCP server tools it chooses. This is intended for local dev only, and is why `trash` exists.
- The app must run from the **repo root** (`main.py` and `mcp_client.py` live there; `core/` is the importable subpackage).

### Adding or removing a tool

A tool's name and behaviour are described in ~10 places, and nothing enforces agreement
between them. Removing `notify` needed every one of these; missing any leaves a doc that
lies or a prompt that names a tool Claude doesn't have. Touch them all:

1. The module in `core/` — exposing `TOOLS`, `handles(name)`, and `await execute(name, input)`.
2. `MODULES` in `core/local_tools.py` (the import *and* the list entry).
3. `requirements.txt` and `pyproject.toml`, if it has a third-party dependency.
4. `SYSTEM_PROMPT` in `core/chat.py` — both the explicit roster **and** any tool-choice
   guidance, which is the half no automation can generate.
5. `README.md` — the tool table, the optional-package table, the project layout, and the
   tool count (stated more than once).
6. `CLAUDE.md` — the module bullet in Architecture, the count in Overview, and the count in
   Key conventions.
`main.py` no longer needs touching — its MCP messages don't enumerate tools.

Then verify instead of trusting the list: `grep -rni <toolname>` across `*.py`/`*.md`/
`*.toml`/`*.txt` should come back empty on a removal, and the roster inside `SYSTEM_PROMPT`
should still match `local_tools.TOOLS` exactly. Counting `len(local_tools.TOOLS)` beats
counting by hand.

### Removed from the original tutorial

`mcp_server.py` (the bundled stdio document server) and `core/cli_chat.py` (the `@mention` / `/command` document-resource layer) have been **deleted** — that whole `docs://documents` resource/prompt system was tutorial scaffolding and is gone. Don't reintroduce a `doc_client`.
