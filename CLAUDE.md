# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

ResearchMesh, a Linux CLI Research Client for Claude is a command-line chat client for the Anthropic API, built on the Model Context Protocol (MCP). The CLI talks to Claude and to one or more MCP servers, and additionally gives Claude **18 local tools** — Anthropic's built-in "learned" schemas (bash, text editor, web search/fetch, cross-session memory, computer use) plus custom ones for DOM browsing, document conversion, stateful Python, interactive commands, config editing, SQL, and recoverable deletes. It began as a learning/tutorial project (a Skilljar submodule) but has since been rewired to connect to any number of external MCP servers over Streamable HTTP, declared as a list under `[mcp]` in `config.toml`, instead of the original bundled stdio document server.

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
sudo apt install xvfb                   # computer: only if you're on Wayland or headless
```

The `computer` tool needs an **X11** display — Wayland ignores the XTEST input it synthesises, so it refuses there rather than failing silently. On a Wayland box, run the client under a nested X server instead: `xvfb-run -s '-screen 0 1280x800x24' python main.py`.

Optional tool dependencies are imported **lazily, inside the tool that needs them**, so a missing package only breaks that one tool — it still gets declared to Claude and returns an install hint if used. To drop a tool entirely, remove its module from `MODULES` in `core/local_tools.py`.

If a tool's install hint names a package that `requirements.txt` already lists (e.g.
`sql_query`'s `duckdb`, or `config_edit`'s `ruamel.yaml`/`jsonpath-ng`), the docs aren't
incomplete — the active venv predates that line. There is no lockfile here: every entry in
`requirements.txt` is a `>=` floor rather than a pin, so a venv can still satisfy the file as
it stood when it was built and lack a package added to it since. Re-running
`pip install -r requirements.txt` fixes it **without restarting the app** — every optional
backing is imported inside the function that needs it, and a failed import leaves no cached
sentinel behind (`core/data.py` assigns `_connection` only on success), so the next tool call
simply retries the import.

See **`README.md`** for the full environment setup — the quick start is at the top, and the collapsed "Full setup detail" section covers browser system libraries, the optional per-tool packages, and environment variables. (`SETUP.md` was merged into it; the two duplicated ~60% of their content and drifted apart.)

There are **no tests, linters, or type checks** configured — no `[tool.ruff]`/`[tool.black]`
in `pyproject.toml`, no `.pylintrc`, nothing runs in CI or on save. Sanity-check edits with
`python -m py_compile` and an import smoke test (`PYTHONPATH=.. python -c "import
core.chat"`). `pylint`/`mypy`/`black`/`ruff` (in whatever venv you run the project from) and
`shellcheck` (system) are not project dependencies but are safe to run by hand if present.

If you run `ruff`, read the output in two halves — **only the first half is by design.**
`ruff check --isolated core/` reports 45 findings on ruff 0.16.3, 30 of them `BLE001`
(blind-except). Most of those are the deliberate tool-execution isolation boundaries: each
local tool catches anything and returns an error string instead of raising, so one bad tool
call can't crash the chat loop (see `core/chat.py`'s `_run_tool_uses` /
`_resolve_pending_tool_uses`). Not all of them, though — the ones in `core/cli.py` (REPL
loop), `core/tools.py` (MCP execute path) and `core/chat.py` itself are loop guards rather
than per-tool boundaries, and `main.py`'s connect fallback adds two more outside `core/`.
Scope or ignore `BLE001` in a ruff config rather than "fixing" it by narrowing the excepts.
**The remaining 15 findings are not covered by that argument and are worth actually
reading** — `B006` at `core/claude.py:52` is the mutable-default `stop_sequences=[]` listed
as a known open issue in the `core/claude.py` bullet below, not a false positive. One caveat
on the counts: `BLE001` is not in ruff's historical default `E4`/`E7`/`E9`/`F` set, so an
older ruff than 0.16.3 reports none of it and the totals above won't match.

## Runtime configuration

- `ANTHROPIC_API_KEY` — read from the environment. `main.py` keeps an explicit `os.getenv("ANTHROPIC_API_KEY")` reference on purpose (the user runs a global key from `~/.bashrc`); **do not remove it** even though `core/claude.py` also constructs its own `Anthropic()` that reads the same env var.
- **MCP bearer tokens** — each `[mcp].servers` entry may set `token_env` naming the environment variable that holds its token; it is sent as `Authorization: Bearer <token>`. A server with no `token_env` connects unauthenticated. Tokens are never stored in `config.toml`.
- **`$VAR` in `[mcp].servers`** — `tomllib` does no substitution, so `main.py`'s `_expand_paths()` expands `~` and `$VAR`/`${VAR}` in `command`, `url`, and the *values* of `env` before the entry reaches `build_client()`. That is what lets the committed config say `/home/$USER/...` instead of one developer's home directory. `env`'s keys are variable names and are deliberately not expanded, and an undefined variable is left verbatim (`expandvars`' behaviour) so it surfaces in the "could not reach/launch" warning rather than collapsing to `/home//...`.
- `CLAUDE_SHOW_USAGE` — set to `1` to print per-request token and prompt-cache counters (see `core/chat.py`). Prompt caching fails silently, so this is how you confirm the `cache_control` breakpoint is landing.
- The Claude model comes from `config.toml` (`[claude] model`), overridable by the `CLAUDE_MODEL` env var (default `claude-sonnet-5`). The app does **not** load a `.env` file — `ANTHROPIC_API_KEY` and any MCP tokens come from the shell environment (e.g. `~/.bashrc`).
- The 2026 web-tool schemas (`web_search_20260318` / `web_fetch_20260318`) need a current `anthropic` SDK to parse the server-tool result blocks (`pip install -U anthropic`). These tools are versioned by capability rather than superseded — each dated variant is a superset of the last — so check the [tool reference](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-reference) for a newer date before assuming the pinned one is current.
- `CLAUDE_MEMORY_DIR` — where the `memory` tool's virtual `/memories` tree actually lives (default `./memories`, relative to the repo root the app must run from).
- `CLAUDE_DISPLAY_SIZE` — the logical screen size `computer` declares and downscales to, e.g. `1280x800` (default). Below ~1280x720 accuracy drops; it must never be set to something the module doesn't also resize screenshots to.
- `CLAUDE_COMPUTER_FORCE=1` — bypass the Wayland refusal in `computer`. Only useful on an XWayland-only setup; the real fix is an Xorg session or `xvfb-run`.
- Python 3.11+ (`pyproject.toml`) — the floor is `tomllib`, used by `main.py`.

## Architecture

Request flow: **CLI input → Chat.run() agentic loop → Claude API + (local tools | MCP server tools)**.

- **`main.py`** — entrypoint (in the repo root). Reads the API key, builds a `Claude` service, connects every enabled `[mcp].servers` entry over Streamable HTTP via `build_client()` / `_connect_mcp_servers()` (which runs `_expand_paths()` on each entry first, so `$USER`/`~` in paths resolve) — a server that fails is reported and skipped rather than aborting startup — plus any stdio servers passed as argv, registers `local_tools.shutdown` on the `AsyncExitStack`, wires everything into a `Chat`, and runs the `CliApp` loop.

- **`core/chat.py`** (`Chat`) — the agentic loop, plus `SYSTEM_PROMPT`, sent as `system` on every request. That prompt exists because with tool schemas alone Claude describes capabilities it doesn't have (it invented a sandboxed `code_execution` container in testing); it states the two execution locations, that nothing is sandboxed, which tools are stateful, and how to choose between the overlapping ones. Keep it factual — if you add or remove a tool, update it. Each turn it calls Claude with the merged tool set (`local_tools.TOOLS + ToolManager.get_all_tools(...)`, the MCP half fetched once per turn). While `stop_reason == "tool_use"` it routes each `tool_use` block — `local_tools.execute()` first, then `ToolManager.execute_blocks()` (MCP) if no local module owns the name — feeds the results back, and loops. `stop_reason == "pause_turn"` (server-side web tools mid-run) is handled by resending. Capped at `MAX_TOOL_ITERATIONS`.

- **`core/local_tools.py`** — the registry of client-executed tools. Every module in `MODULES` exposes the same three names (`TOOLS`, `handles(name)`, `await execute(name, input)`), so a new tool is one new module plus one line here rather than edits to the chat loop's declaration list *and* its routing chain. Raises at import on duplicate tool names, and `shutdown()` releases everything the tools may have started (browser, kernel, DuckDB).

- **`core/claude_learned_schemas.py`** — Anthropic's built-in ("learned") tools. `bash` (`bash_20250124`) and the text editor (`text_editor_20250728` / `str_replace_based_edit_tool`) are **client-executed** here; `web_search` (`web_search_20260318`) and `web_fetch` (`web_fetch_20260318`) are **server-executed** by Anthropic (declaration only, no local handler). `handles()` reports the two client-side names; `execute()` runs them off the event loop via `asyncio.to_thread`.

- **`core/memory.py`** — `memory` (`memory_20250818`), Anthropic's client-executed memory tool. A learned schema, so no description. `/memories` is a **virtual prefix**, not a real path: `_resolve()` maps it onto one real directory (`CLAUDE_MEMORY_DIR`, default `./memories`) and canonicalises *before* testing containment, so `..` segments and escaping symlinks are both caught — that confinement is the one hard requirement Anthropic places on the client, since `/memories/../../.ssh/id_rsa` is otherwise a key read. The return strings deliberately match the reference wording in Anthropic's docs; Claude was trained against it, so rewording them makes it misread ordinary outcomes as failures. Two deliberate deviations: `create` overwrites rather than erroring (Claude's own description says "creates or overwrites"), and `view` on a `.png`/`.jpg` returns an `image_result` marker. **This is the only local state that survives process exit** — the kernel, browser page, and DuckDB connection are all per-session.

- **`core/computer.py`** — `computer` (`computer_20251124`), Anthropic's client-executed computer use tool: screen capture plus mouse/keyboard via `pyautogui` (both imported lazily). Two things dominate the design. **Coordinates:** Claude answers in the coordinate space of the image it was sent, so a declared `display_width_px`/`display_height_px` that disagrees with the screenshot offsets every click. The module therefore declares one fixed logical size (`CLAUDE_DISPLAY_SIZE`, default 1280x800), always resizes captures to exactly that, and scales coordinates back to native in `_to_native()` — declared size and sent image cannot drift. **Beta gating:** `computer_20251124` needs the `computer-use-2025-11-24` header, exported here as `BETA_FLAG` and consumed by `core/claude.py`, which is why the whole app posts to the beta endpoint. On Wayland the tool refuses with an explanation instead of clicking into the void (XTEST is ignored there); `CLAUDE_COMPUTER_FORCE=1` overrides. Actions other than `wait` return a screenshot, matching the reference implementation Claude was trained against.

- **`core/browser.py`** — a custom **Playwright** browser tool (`browser_navigate` / `_extract` / `_click` / `_fill` / `_links` / `_back`). Fully custom schemas (Claude learns them from descriptions). A single headless page is kept alive across calls (lazy-launched — `playwright` is imported only on first use, so the module imports fine without it), and each tool trims its output to avoid context bloat. `shutdown()` closes the browser on exit. The point of this tool is **DOM-based surfing**, so `browser_navigate` is described as the primary way to read the web and every page-changing call reports the current URL — there is deliberately no separate "current URL" tool. `_trim()` flattens newlines and is for prose only; element lists use `clip()` so their line structure survives.

- **`core/documents.py`** — `document_convert`: headless LibreOffice (`soffice --convert-to`) with a **throwaway `-env:UserInstallation` profile per call**, because LibreOffice locks its user profile and a second concurrent call otherwise fails silently. Markdown sources route through **pandoc** (soffice has no dependable markdown import); `md → pdf` goes md → odt (pandoc) → pdf (soffice), since pandoc's own PDF writer needs a LaTeX engine.

- **`core/kernel.py`** — `python`: a persistent IPython kernel over `jupyter_client`. The one thing `bash` structurally cannot do, since **state survives between calls**; it also covers plotting/data/symbolic work as plain imports instead of more tool slots. ANSI codes are stripped from tracebacks, inline images are reported but not returned (save to disk instead), and `restart: true` gives a clean namespace.

- **`core/processes.py`** — `interactive_run`: `pexpect` on a pty for commands that prompt (passwords, `[y/N]`, ssh host keys, installers, REPLs), which the `bash` tool cannot answer and simply hangs on. One tool taking a scripted list of expect/send steps rather than a stateful spawn/send/expect trio; `secret: true` redacts a response from the returned transcript.

- **`core/config_edit.py`** — `config_edit`: round-trip YAML (`ruamel.yaml`), TOML (`tomlkit`), and JSON edits that **preserve comments**, key order, and quoting, which `sed` and stdlib YAML silently destroy. Dotted key paths with `[index]` support, `$…` JSONPath for read-only queries (`jsonpath-ng`), and writes go through a temp file + `os.replace`.

- **`core/data.py`** — `sql_query`: DuckDB against CSV/Parquet/JSON files in place, no import step. One in-memory connection is reused for the session, so views and temp tables persist across calls.

- **`core/files.py`** — `trash`: `send2trash`, the only recoverable delete available here given there is no approval gate. Paths are made absolute (send2trash fails opaquely on relative ones) and `TrashPermissionError` is translated, since GIO refuses to trash from tmpfs mounts like `/tmp` and raises it with an empty message.

- **`core/output.py`** — `clip(text, limit)`, the one truncation helper the local tool modules share (bash/editor/kernel/pexpect budget 12000 chars, browser 6000), plus `IMAGE_MEDIA_TYPES` and `image_result(...)`. The latter builds the `{"__kind__": "image", ...}` marker that a tool returns instead of a string when its result is pixels (file-editor/memory `view` on an image, every computer screenshot); `Chat._local_result_to_content` turns it into a real `image` content block.

- **`core/claude.py`** (`Claude`) — thin Anthropic SDK wrapper. It posts to **`client.beta.messages.create`, not `client.messages.create`**, with `betas=BETAS` — the `computer` tool's schema is beta-gated and `local_tools` declares it on *every* request, so the header is unconditional; omitting it 400s the whole request, not just computer use. The beta endpoint is a superset, but it returns **`BetaMessage`, which is not a subclass of `Message`** — hence `_RESPONSE_TYPES`; an `isinstance(message, Message)` check alone silently stuffs the response object into `content` instead of its blocks. Three more things to know before editing `chat()`: **no sampling parameters** — current models reject a non-default `temperature`/`top_p`/`top_k` with a 400 and only accept the default, so sending one can only fail; and **no `budget_tokens`** — adaptive thinking replaced it (`{"type": "enabled", "budget_tokens": N}` is a 400 now), with `output_config={"effort": ...}` as the depth knob if ever needed. Also open: `stop_sequences=[]` is a mutable default argument and is sent empty on every request, and `max_tokens=8000` is shared by thinking *and* the reply — a `/think` turn on a hard problem can end in `stop_reason: "max_tokens"`; raise it (streaming is advisable much above ~16K). `chat()` builds request params (max_tokens 8000, optional thinking/tools/system); helpers append user/assistant messages and extract text blocks.

- **`core/tools.py`** (`ToolManager`) — the MCP↔Anthropic bridge (remote server tools only). `get_all_tools` aggregates tool schemas across all MCP clients (called once per user turn by `Chat`, not per tool-use iteration); `execute_blocks` executes a list of `tool_use` blocks against the owning client, resolving owners via one `_tool_owners` map per call.

- **`core/cli.py`** (`CliApp`) — a minimal `prompt_toolkit` REPL (history + styling). Delegates all real work to `Chat`.

- **`mcp_client.py`** (`MCPClient`) — async context-manager over an MCP `ClientSession`, supporting three transports: `stdio` (spawn `command`+`args`), `sse` (`url`), and `http` (Streamable HTTP `url`) — the last two accept `headers` for auth (e.g. a Bearer token). Exposes `list_tools`, `call_tool`, `list_prompts`, `get_prompt`, `read_resource`.

### Key conventions

- **Two parallel tool systems.** MCP tools live on remote servers (any number, listed under `[mcp]`) and are discovered/executed via `ToolManager`. Local tools are declared and executed in-process, aggregated by `local_tools`. `Chat` merges both into one `tools=` list and routes execution by owner.
- **"Learned" vs custom.** The distinction is whether Claude already knows the schema, *not* which file it lives in. `claude_learned_schemas.py` holds the small Anthropic-defined tools (bash, text editor, and the two server-side web tools); `memory.py` and `computer.py` are also Anthropic-defined but got their own modules because their implementations are substantial. None of them carry descriptions — Claude is already trained on those schemas, so writing one is at best redundant and at worst contradicts what it was trained on. Every other local module holds fully custom tools Claude learns at runtime from its descriptions. Keep all of them separate from `tools.py`, which is strictly the MCP bridge.
- **A learned tool is exempt from the "must beat `bash`" test below** — the schema already exists in the model, so the only question is whether you want the capability, not whether it earns a slot on novelty.
- **A new local tool must beat `bash` at something structural** — statefulness (`python`), interactivity (`interactive_run`), a correctness guarantee (`config_edit`), recoverability (`trash`), or context economy — since Claude can already shell out to any CLI. Wrapping a command bash could run unaided just spends a tool slot.
- **Tool-selection accuracy degrades past roughly 30–50 loaded tools.** 18 local + whatever the connected MCP servers advertise leaves headroom; prefer one tool with a mode parameter (as `document_convert` and `config_edit` do) over one tool per variation.
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
7. `BETAS` in `core/claude.py`, **only if the tool's schema is beta-gated** (as
   `computer_20251124` is). Export the flag from the tool's own module the way
   `computer.BETA_FLAG` does, so the header and the tool version can't drift apart. This
   one has global blast radius: the header rides every request, and a *missing* one 400s
   the whole conversation rather than just that tool.
8. `README.md`'s environment-variable table and `CLAUDE.md`'s "Runtime configuration", if
   the tool reads any env var of its own.
`main.py` no longer needs touching — its MCP messages don't enumerate tools.

Then verify instead of trusting the list: `grep -rni <toolname>` across `*.py`/`*.md`/
`*.toml`/`*.txt` should come back empty on a removal, and the roster inside `SYSTEM_PROMPT`
should still match `local_tools.TOOLS` exactly. Counting `len(local_tools.TOOLS)` beats
counting by hand.

### Removed from the original tutorial

`mcp_server.py` (the bundled stdio document server) and `core/cli_chat.py` (the `@mention` / `/command` document-resource layer) have been **deleted** — that whole `docs://documents` resource/prompt system was tutorial scaffolding and is gone. Don't reintroduce a `doc_client`.
