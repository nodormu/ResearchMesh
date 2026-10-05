# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

ResearchMesh is a command-line chat client for the Anthropic API, built on the Model Context Protocol (MCP). The CLI talks to Claude and to any number of MCP servers declared under `[mcp]` in `config.toml` (Streamable HTTP or stdio), and gives Claude **27 local tools**: Anthropic's built-in "learned" schemas (bash, text editor, web search and fetch, cross-session memory, computer use) plus custom tools for DOM browsing, desktop windows and OCR, document conversion, stateful Python, a persistent bash session, interactive commands, config editing, SQL, recoverable deletes, vector embeddings and image queries against user-supplied private servers, local text-to-speech (Piper) and speech-to-text (faster-whisper), and MIDI 1.0 device I/O (mido/python-rtmidi). `mcp_server.py` serves the same agent to MCP clients as a `delegate` tool.

## Commands

Run the app from the repo root, not from `core/`:

```bash
python main.py
```

Environment variables, read from the shell (the app does not load `.env`):

```bash
export ANTHROPIC_API_KEY=...      # in practice lives in ~/.bashrc
export YOUR_SERVER_MCP_TOKEN=...  # one per server, named by its token_env in config.toml
```

Check every configured MCP server standalone (connect, list tools, report failures, exit):

```bash
python mcp_client.py
```

Node.js on `PATH` is needed only for Node-based MCP servers declared in `config.toml`; Playwright for Python bundles its own Node driver. Extra stdio MCP servers can be passed as argv: `python main.py path/to/other_server.py`.

```bash
pip install -r requirements.txt
playwright install chromium   # pip installs the package, not the browser itself
```

The full install walkthrough (apt packages, Playwright's OS libraries, X11/Wayland, environment variables, the per-tool package table) is README's "Setup (Linux)" section. The two commands above give a working dev environment; the rest is one-time OS-package setup.

`computer` runs on Wayland through the desktop's remote-control portal (`core/wayland_input.py`), so do not assume Wayland means no GUI automation. README's Setup step 3 also has a manual technique for one XWayland window.

Per-tool dependencies are imported lazily, inside the tool that needs them, but `requirements.txt` installs every one of them. If a tool's install hint names a package `requirements.txt` already lists, the active venv predates that line (every entry is a `>=` floor, with no lockfile); `pip install -r requirements.txt` fixes it without a restart, because a failed import leaves no cached sentinel (`core/data.py` assigns `_connection` only on success). To drop a tool, remove its module from `MODULES` in `core/local_tools.py`.

Three gates, all run by CI (`.github/workflows/ci.yml`) on every push and PR to `main`:

- **`ruff check .`** should come back clean. `pyproject.toml`'s `[tool.ruff.lint]` adds no rules; it lists exemptions, each with its reason. A finding is new: something you wrote, or a rule a newer ruff added (`select` stays at ruff's defaults, which shift between versions). Triage it.
- **`mypy .`** should come back clean. `[tool.mypy]` sets `exclude` and `ignore_missing_imports` (the per-tool packages are imported lazily and absent from a bare environment); strictness is at mypy's defaults, so unannotated function bodies go unchecked. It checks against the installed packages, which makes it the gate that notices a dependency changing shape, including in `core/tools.py`, which `smoke_test.py` cannot reach. Against an old installed version it stays quiet: it warns at upgrade time, not before.
- **`python smoke_test.py`** is not a test suite: it never exercises a tool's behaviour, which would need LibreOffice, a browser, a display and API credits. It checks that everything imports, that the tool registry is well-formed with no duplicate names, that **the tool count in the docs equals `len(local_tools.TOOLS)`** (it is stated in several places in README.md and here, and hand-checking drifts), and that `mcp_server.py` completes an MCP handshake advertising `delegate` (a stray byte on stdout desynchronises JSON-RPC and is invisible until a client connects). It needs no API key (a placeholder satisfies `_require_api_key`, and listing tools never reaches the API) and no per-tool packages, so CI installs only the five module-level dependencies and finishes in seconds. It also drives `Claude.chat()` and `Chat._run_tool_uses` against a fake API: the per-model tool-compatibility handler, the computer toolset's `tool_result` round trip and `cursor_position`, and the web tools' `allowed_callers`. It cannot see Anthropic rewording the "does not support tool types" error (`python test_model_compat_live.py` checks that against the real API, spends tokens and is not in CI) or a new computer-toolset member (`_MEMBERS` in `core/computer.py` is hand-kept).

The `test_*.py` scripts are local tools, not CI: they spawn real shells, pty children, browsers, a desktop session or MIDI ports. `test_browser_mode.py` needs a display: `xvfb-run -a python test_browser_mode.py < /dev/null`. There are no unit tests beyond these. `pylint`, `black` and `shellcheck` are unconfigured but safe to run by hand.

Two rules a linter will fight you on:

- **Blanket `except` is deliberate** (`BLE001`; re-run `ruff check . --select BLE001 --statistics` for the count). Every local tool must catch anything and return an error string rather than crash the chat loop (`core/chat.py`'s `_run_tool_uses` and `_resolve_pending_tool_uses`); the ones in `core/cli.py`, `core/tools.py`, `core/chat.py` and `main.py` guard the REPL, the MCP execute path and the connect fallback. Do not narrow them.
- **Cleanup paths must not fail, and must not fail silently.** `zmq.ZMQError` derives from `Exception`, not `OSError`, so narrowing the guard around `core/kernel.py`'s `stop_channels()` (it ends in pyzmq's `context.destroy()`) lets it escape through `local_tools.shutdown()` into a traceback on Ctrl-C. Use a blanket catch plus a `print()`: when `S110` (`except: pass`) fires, the defect is the silence, not the breadth.

**Shutdown is isolated per tool.** `local_tools.shutdown()` runs each tool's cleanup in its own `try`, because it is an `AsyncExitStack` callback: an exception escaping one skips every later one (leaking what that tool owns) and produces a traceback. `main.py`'s post-failed-connect cleanup follows the same rule.

**`subprocess.run` is deliberately `check=False`** in `core/claude_learned_schemas.py` and `core/documents.py`. Both turn a non-zero exit into a normal return value (a string for `bash`, an `(ok, output)` tuple for `document_convert`), so `check=True` is the wrong fix for `PLW1510`: in `claude_learned_schemas.py` the resulting `CalledProcessError` would be caught by the broad `except` below it and reported as a generic error with stdout lost, and in `documents.py` nothing would catch it (`_run` handles only `FileNotFoundError` and `TimeoutExpired`). The explicit `check=False` writes the default out and changes no behaviour.

## Runtime configuration

- `ANTHROPIC_API_KEY` — read from the environment. `main.py` keeps an explicit `os.getenv("ANTHROPIC_API_KEY")` reference on purpose (the user runs a global key from `~/.bashrc`); do not remove it, even though `core/claude.py` also constructs its own `Anthropic()` that reads the same variable.
- **Claude Code CLI vs. this app's key** — if the user also runs the `claude` CLI on a Pro/Max subscription, `ANTHROPIC_API_KEY` takes priority over subscription auth the moment it is set (Claude Code's documented behavior) and bills per token. `~/.bashrc`'s `alias claude='env -u ANTHROPIC_API_KEY claude'` hides the variable from that one invocation only (`env -u` strips it for the child process and never touches the shell's exported value). Do not suggest unsetting the variable itself; that breaks this app's own `Anthropic()` calls.
- **MCP bearer tokens** — each `[mcp].servers` entry may set `token_env`, naming the environment variable that holds its token; it is sent as `Authorization: Bearer <token>`. A server with no `token_env` connects unauthenticated. Tokens are never stored in `config.toml`.
- **`[embeddings]` in config.toml** — settings for `text_embeddings` (`core/text_embeddings.py`): `url` (required; the tool errors by name until it is set), `model`, `request_format` (`"openai"` default or `"simple"`), `api_key_env` (the same indirection as `token_env`, never the token itself) and `timeout` (default 30). Every key ships commented out. The table is read from disk on every call, unlike `[mcp].servers` and `[claude]`, which `main.py` reads once at startup.
- **`[vision]` in config.toml** — settings for `vision_query` (`core/vision.py`): `url` (required; errors by name until set), `model`, `max_tokens` (default 4000; a reasoning vision model can spend a small budget entirely on invisible `reasoning_content` before it writes an answer), `timeout` (default 180) and `api_key_env`. Every key ships commented out; read on every call. An unset or unreachable server returns a `local_unavailable` status and stops; the tool never falls back to Claude's own vision, which is a separate decision made in conversation.
- **`[speak]` in config.toml** — settings for `speak` (`core/speak.py`): `enabled` (default true; a hard off-switch checked before `voice_model`, so a disabled tool touches no filesystem and spawns no process), `voice_model` (path to a Piper `.onnx` file with a matching `<path>.json` sidecar; required), `sink` (PipeWire sink name; the system default if unset) and `timeout` (default 30, applied to synthesis and playback separately). Every key ships commented out, so `speak` returns `not_configured` until they are set; the example values are one machine's hardware and stay commented out in commits. Read on every call. Needs the PyPI package `piper-tts`, not `sudo apt install piper`, which installs an unrelated GTK app for configuring gaming mice.
- **`[listen]` in config.toml** — settings for `listen` (`core/listen.py`): `enabled` (default true; checked before `device`, so a disabled tool never opens the microphone), `device` (PipeWire source name, found with `pactl list sources short`; required), `model_size` (faster-whisper size: `tiny`, `base`, `small`, `medium` or `large-v3`; default `base`), `default_duration_seconds` (default 8) and `max_duration_seconds` (cap on any requested duration; default 30). Every key ships commented out, so `listen` returns `not_configured` until `device` is set. Read on every call.
- **`[bash]` in config.toml** — `shell`: the interpreter for `bash` (`core/claude_learned_schemas.py`), `bash_session` and `interactive_run` (`core/processes.py`): an absolute path or a bare name resolved through `$PATH`; unset, blank or unresolvable falls back to `/bin/bash`. Resolved once at import, so a change needs a restart. `claude_learned_schemas.SHELL_EXECUTABLE` is the one constant both call sites and `SYSTEM_PROMPT` (`core/chat.py`) read, so none of the three can drift. `SYSTEM_PROMPT` also names `SH_TARGET`, a live `os.path.realpath("/bin/sh")` taken at every process start, because `/bin/sh` varies by distro (dash on Debian and Ubuntu).
  - If `shell` resolves to zsh, `apply_shell_prelude()` prepends `setopt SH_WORD_SPLIT; unsetopt NOMATCH` to every command, in both `bash` and `interactive_run`. `KSH_ARRAYS` is not set to fix zsh's 1-based arrays: it changes what an unsubscripted `$array` means and makes braces mandatory on subscripts. That difference is stated in `SYSTEM_PROMPT` instead.
- `RESEARCHMESH_MCP_TOKEN` — the serving side of the bearer-token contract: the token `mcp_server.py --transport streamable-http` requires from its clients (rename with `--token-env`). Unset means the endpoint is unauthenticated, which is allowed; stdio needs no token because there is no port. Generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"`; there is no generator script, since a wrapper around one stdlib line adds nothing over `bash`. Placement rules that are easy to get wrong: a `systemd` unit does not read `~/.bashrc` (use `EnvironmentFile=`); an MCP client passes a stdio server only a small safe environment subset, so the variable must be named in that server's `env` block in the client config; and the literal token never goes in `.mcp.json` or `config.toml`, which are committed (use `${VAR}` and `token_env`). Each end reads the variable from its own environment, so serving and consuming machines normally share one name; a second name is needed only on a machine that both serves an endpoint and consumes another, where one variable would otherwise mean two secrets.
- **`$VAR` in `[mcp].servers`** — `tomllib` does no substitution, so `main.py`'s `_expand_paths()` expands `~` and `$VAR`/`${VAR}` in `command`, `url` and the values of `env` before the entry reaches `build_client()`, which lets the committed config say `/home/$USER/...`. `env` keys are variable names and are not expanded. An undefined variable is left as written (`expandvars`' behavior), so it shows up in the "could not reach/launch" warning instead of collapsing to `/home//...`.
- `CLAUDE_SHOW_USAGE=1` — print per-request token and prompt-cache counters (`core/chat.py`). Prompt caching fails silently, so this is how to confirm the `cache_control` breakpoint is landing.
- **The Claude model** comes from `config.toml` `[claude] claude_models` (a list); the first entry is what every new session starts on. The list is a cache: `core/claude.py`'s `refresh_claude_models()` is gated by `model_scan_ttl_hours` (default 24), re-scans `/v1/models` through `fetch_live_models()` once the cache is stale, and rewrites `claude_models` in place, one entry per model family, newest first, sonnet moved to the front. A failed scan (offline, bad key) writes nothing, which is why `smoke_test.py` can spawn `mcp_server.py` with a placeholder key and never touch the network. `main.py` computes `claude_model = claude_models[0]` at import, and `mcp_server.py` does `import main as app` and builds `Claude(model=app.claude_model)`, so an instance served over MCP gets the same default. No env var overrides it; `/model swap` (`core/cli.py`) changes the model mid-session. The app does not load a `.env` file: `ANTHROPIC_API_KEY` and MCP tokens come from the shell environment.
- The 2026 web-tool schemas (`web_search_20260318`, `web_fetch_20260318`) need a current `anthropic` SDK to parse the server-tool result blocks (`pip install -U anthropic`). They are versioned by capability, each dated variant a superset of the last, so check the [tool reference](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-reference) for a newer date before assuming the pinned one is current.
- **`mcp>=2,<3` is a hard floor** (`requirements.txt` has the full note). mcp 2.0 is a breaking major and this code uses its API on both sides: `Server(on_list_tools=…, on_call_tool=…)` in `mcp_server.py` (handlers take a leading request context and return a whole `ListToolsResult` or `CallToolResult`), `streamable_http_client` in `mcp_client.py` (headers ride an httpx2 client from `create_mcp_http_client()`, because the transport dropped `headers=`), and snake_case model fields in `core/tools.py` (`input_schema`, `is_error`, `mime_type`). The camelCase spellings survive as serialization aliases, so constructing a model works either way while attribute reads break: a 1.x/2.x mismatch in `core/tools.py` is a runtime `AttributeError` that no import check catches. The upper bound matches `claude-agent-sdk`'s, so both can share a venv.
- `CLAUDE_MEMORY_DIR` — where the `memory` tool's virtual `/memories` tree lives (default `./memories`, relative to the repo root the app must run from).
- `CLAUDE_KERNEL_ENCRYPTION` — `auto` (default), `required` or `off`, selecting the transport for the `python` kernel (`core/kernel.py`). `auto` tries CurveZMQ-encrypted TCP, then IPC, then plaintext TCP, printing why each tier fell through; `required` turns an unencrypted kernel into a tool error (every tier still works, and only the printed line distinguishes them); `off` skips the encrypted tier. An unrecognised value is reported and treated as `auto`.
- `CLAUDE_DISPLAY_SIZE` — the logical screen size `computer` declares and downscales to, e.g. `1280x800` (default). Below about 1280x720 accuracy drops. Never set it to something the module does not also resize screenshots to.
- `CLAUDE_COMPUTER_FORCE=1` — use X11/XTEST for `computer` on a Wayland session anyway (XWayland-only setups, nested X servers such as Xvfb). Without it a Wayland session goes through the xdg-desktop-portal in `core/wayland_input.py`.
- `CLAUDE_COMPUTER_MONITOR` — monitor index, counted left to right, for the Wayland route; a missing or out-of-range value selects the first monitor.
- `RESEARCHMESH_DOWNLOAD_DIR` — where browser downloads land (default `~/Downloads`); see the `core/browser_session.py` bullet.
- `PASSWORD_STORE_DIR` — the `pass` store read for vault entry names (default `~/.password-store`); see the `core/processes.py` bullet.
- Python 3.11+ (`pyproject.toml`): the floor is `tomllib`, used by `main.py`.

## Architecture

Request flow: **CLI input → Chat.run() agentic loop → Claude API + (local tools | MCP server tools)**.

- **`main.py`** — entrypoint, in the repo root. Reads the API key, builds a `Claude` service, connects every enabled `[mcp].servers` entry over Streamable HTTP through `build_client()` and `_connect_mcp_servers()` (which runs `_expand_paths()` on each entry first, so `$USER` and `~` resolve) plus any stdio servers passed as argv, wires everything into a `Chat`, and runs the `CliApp` loop. A server that fails is reported and skipped, never fatal. Three things are registered on the `AsyncExitStack`, in an order that matters: `process_reaper`'s check is pushed first so it runs last (the stack unwinds LIFO), after every MCP client's `cleanup()` and `local_tools.shutdown()`, because it is a safety net for what they miss.
- **`core/process_reaper.py`** — `reap_orphans()`: the last-line exit safety net, independent of any tool's own `shutdown()`. It walks `/proc/<pid>/task/<TID>/children` for every thread, not only the main one: the blocking local tools run under `asyncio.to_thread()`, so a forked child sits under a worker thread's entry. It SIGKILLs whatever is still alive, then reaps zombie direct children with a bounded `waitpid(-1, WNOHANG)` retry loop, because a zombie does not appear the instant after the kill. Linux only; it reads `/proc` defensively, so a kernel without it finds nothing instead of crashing exit. It prints what it killed, or `[shutdown] clean exit, no leftover processes`.
- **`core/chat.py`** (`Chat`) — the agentic loop, plus `SYSTEM_PROMPT`, sent as `system` on every request. The prompt exists because with tool schemas alone Claude describes capabilities it does not have (it invented a sandboxed `code_execution` container); it states the two execution locations, that nothing is sandboxed, which tools are stateful, and how to choose between overlapping ones. It names every built-in tool and states their count, so adding or removing a tool means updating it (see "Adding or removing a tool"). Each turn calls Claude with the merged tool set (`local_tools.TOOLS` plus `ToolManager.get_all_tools(...)`, the MCP half fetched once per turn). While `stop_reason == "tool_use"` it routes each `tool_use` block, `local_tools.execute()` first and `ToolManager.execute_blocks()` if no local module owns the name, feeds the results back, and loops. `stop_reason == "pause_turn"` (server-side web tools mid-run) replaces the turn's assistant message and resends. The loop is capped at `MAX_TOOL_ITERATIONS`.
- **`core/local_tools.py`** — the registry of client-executed tools. Every module in `MODULES` exposes `TOOLS`, `handles(name)` and `await execute(name, input)`, so a new tool is one module plus one line here, not edits to the chat loop's declaration list and its routing chain. It raises at import on duplicate tool names. `shutdown()` releases everything the tools may have started (browser, computer session, window-control connection, kernel, shell session, DuckDB, MIDI ports), each in its own `try` (see "Shutdown is isolated per tool").
- **`core/claude_learned_schemas.py`** — Anthropic's built-in ("learned") tools. `bash` (`bash_20250124`) and the text editor (`text_editor_20250728`, `str_replace_based_edit_tool`) are client-executed here; `web_search` (`web_search_20260318`) and `web_fetch` (`web_fetch_20260318`) are server-executed by Anthropic (declaration only, no local handler). `handles()` reports the two client-side names, and `execute()` runs them off the event loop with `asyncio.to_thread`. `bash`'s interpreter (`SHELL_EXECUTABLE`) comes from `[bash]` under Runtime configuration.

- **`core/memory.py`** — `memory` (`memory_20250818`), Anthropic's client-executed memory tool; a learned schema, so it has no description. `/memories` is a virtual prefix, not a real path: `_resolve()` maps it onto one real directory (`CLAUDE_MEMORY_DIR`, default `./memories`) and canonicalises before testing containment, so `..` segments and escaping symlinks are caught. That confinement is the one hard requirement Anthropic places on the client, since `/memories/../../.ssh/id_rsa` would otherwise be a key read. The return strings match the reference wording in Anthropic's docs, because Claude was trained against them and rewording makes it misread ordinary outcomes as failures. Two deliberate deviations: `create` overwrites instead of erroring (Claude's description says "creates or overwrites"), and `view` on a `.png` or `.jpg` returns an `image_result` marker. This is the only local state that survives process exit; the kernel, browser and DuckDB connection are per-session.
- **`core/computer.py`** — `computer` (`computer_toolset_20260801`), Anthropic's client-executed computer-use toolset: screen capture plus mouse and keyboard through `pyautogui` or, on Wayland, the portal backend below (both imported lazily). One `{"type": "computer_toolset_20260801"}` entry, with no `name` of its own, expands into 17 member tools server-side. Claude's calls are `tool_use` blocks whose `name` is the member (`"left_click"`, `"type"`, ...) and which carry an extra `"toolset_name": "computer"` field; the paired `tool_result` must echo that `toolset_name` or the API rejects it (`core/chat.py`'s `_run_tool_uses` handles the round trip). `execute()` accepts only the 17 member names and refuses any other. The older single-tool `computer_20251124` schema is not declared. Two things dominate the design. **Coordinates:** Claude answers in the coordinate space of the last image it was sent, and the toolset has no display-size field, so the module declares one logical size (`CLAUDE_DISPLAY_SIZE`, default 1280x800), resizes every capture to exactly that, and scales coordinates back to native in `_to_native()`. **No beta gating:** the toolset needs no beta header, so `core/claude.py`'s `BETAS` is empty (the app still posts to the beta endpoint, a superset of the plain one). On a Wayland session input and capture go through `core/wayland_input.py`; `CLAUDE_COMPUTER_FORCE=1` uses X11/XTEST anyway. `computer.capture()` returns a native screenshot through the active backend (`_backend()` picks `wayland_input` or `pyautogui`), and `computer.shutdown()` ends the Wayland session. Actions other than `wait` and `cursor_position` (a text-only read) return a screenshot, matching the reference implementation Claude was trained against. A manual X11-protocol technique can still reach one XWayland-backed window and bypasses this module; README's Setup step 3 has the recipe.
- **`core/wayland_input.py`, `core/dbus_loop.py`** — the `computer` backend on a Wayland session: one xdg-desktop-portal RemoteDesktop + ScreenCast session held in-process on a private asyncio loop (`dbus_loop.Loop`, `dbus-next`), exposing the pyautogui calls `computer` already makes (`PortalInput`). The desktop may ask for approval (`_APPROVAL_SECONDS`, 150) and KDE shows a "Remote Control" tray icon with an "End" entry; after End the next action starts a new session. A scroll unit is 10 portal steps (`_STEPS_PER_NOTCH`). The screen is one monitor: the leftmost approved stream, or `CLAUDE_COMPUTER_MONITOR`. `spectacle` or `grim` capture every monitor but only the shared ones are known, so the scale is the smaller of the horizontal and vertical estimates over the shared monitors: exact when they span the desktop's width or height, otherwise estimated, with a one-time console warning. `position()` raises until the first move, because the portal cannot read the pointer back. `write()` presses each character as a keysym and does not press Shift or AltGr itself: the compositor supplies the shift level (KWin does; another compositor's portal may not). `test_wayland_input.py` drives a fake portal; the real session needs the approval dialog.
- **`core/desktop_window.py`** — `desktop_window`: list, focus, move, resize, full-screen, minimize and restore windows. KDE Plasma only, on Wayland or X11. A one-shot KWin script runs inside the compositor and calls back (`callDBus`) into a service this process exports on the session bus; the script embeds its arguments as JSON literals, so nothing the model sends is interpreted as code. `test_desktop_window.py` covers it and also lists the real windows when a KDE session is reachable.
- **`core/screen_find.py`** — `screen_find`: locates on-screen text and buttons with `tesseract`, in the `computer` tool's declared coordinates. It reads the screen through `computer.capture()` (X11 or the Wayland backend) in two passes: word-level OCR in normal and inverted form, and a detector for solid-colour rectangles of button size whose crops are read upscaled, because plain OCR misses light text on coloured buttons. Only the match list is returned. `test_screen_find.py` covers it.
- **`core/browser.py`** — the `browser_*` tools (`browser_navigate`, `_extract`, `_click`, `_fill`, `_links`, `_back`, `_tab`), with fully custom schemas (Claude learns them from the descriptions). One browser session is kept across calls, launched lazily by `core/browser_session.py` (`playwright` is imported on first use, so the module imports without it), and each tool trims its output to avoid context bloat. `shutdown()` closes the browser on exit. The tool is for DOM-based surfing, so `browser_navigate` is described as the primary way to read the web and every page-changing call reports the current URL; there is deliberately no separate "current URL" tool. `_trim()` flattens newlines and is for prose only; element lists use `clip()` so their line structure survives.
- **`core/browser_session.py`** — launch modes, profiles, tabs and downloads for the `browser_*` tools. `browser_navigate` takes `mode` (`headless` by default; `headed`, a visible window; `virtual`, Chrome on a private Xvfb with no window; `real`, installed Chrome started normally and attached over CDP on 127.0.0.1, with its own profile directory because Chrome refuses a debug port on its default profile) and `profile` (a mode-700 directory under `$XDG_CACHE_HOME/researchmesh/browser-profiles`; without one the profile is temporary); `headed` (a boolean) is an alias. Playwright modes pass `--disable-blink-features=AutomationControlled`, because without it `navigator.webdriver` is true and Cloudflare Turnstile fails. `real` passes no automation flags, and its debug port is chosen in advance (`_free_port`), not `0`: Chrome treats `--remote-debugging-port=0` as automation and sets `navigator.webdriver` to true. A change of mode or profile restarts the browser. A click that opens a tab switches to it; `browser_tab` lists, switches and closes. Downloads go to `~/Downloads` (`RESEARCHMESH_DOWNLOAD_DIR` overrides) under a unique name and are listed as `Downloaded:` lines; a CDP-attached Chrome overwrites a same-name file, so it writes to a staging directory first. Reports carry a `Human check:` line, and a fresh default-mode visit that a check stops is reopened once in `virtual` mode, never `real`, which opens a window. `browser_fill` takes exactly one of `value` or `value_secret` (a `pass` vault entry, typed without appearing in the conversation and scrubbed from every browser tool result before clipping) and `submit` (press Enter and return the next page), so a one-time code goes in within its few seconds. `test_browser_mode.py` and `test_browser_secret.py` cover it; run the first as `xvfb-run -a python test_browser_mode.py < /dev/null`.
- **`core/documents.py`** — `document_convert`: headless LibreOffice (`soffice --convert-to`) with a throwaway `-env:UserInstallation` profile per call, because LibreOffice locks its user profile and a second concurrent call otherwise fails silently. Markdown sources go through pandoc (soffice has no dependable markdown import); `md → pdf` goes md → odt (pandoc) → pdf (soffice), since pandoc's PDF writer needs a LaTeX engine.

- **`core/kernel.py`** — `python`: a persistent IPython kernel over `jupyter_client`. It does what `bash` cannot, since state survives between calls, and it covers plotting, data and symbolic work as plain imports instead of more tool slots. ANSI codes are stripped from tracebacks, inline images are reported but not returned (save to disk instead), and `restart: true` gives a clean namespace.
  - **The ZeroMQ link is encrypted** (`_start_encrypted()`). jupyter_client's default is plaintext on four loopback TCP ports carrying all code and results, and `ipykernel` warns about it on every start; the kernel inherits our stderr, so that warning would land in the chat. `KernelManager(transport_encryption="required")` provisions a CurveZMQ keypair and passes it in the mode-600 connection file, so both ends use CURVE. It is `"required"`, not `"auto"`: `auto` provisions nothing and silently runs in the clear when the kernelspec does not declare `metadata.supported_encryption`, the one case worth hearing about. It depends on `jupyter_client>=8.9.1` (8.9.0 shipped the trait but broke restart, which this module uses), an `ipykernel` whose kernelspec advertises curve support, and a pyzmq built with libsodium, all checked at runtime.
  - `_start_manager()` is the resulting ladder: **encrypted TCP → IPC → plaintext TCP**, each tier printing why it fell through, with `CLAUDE_KERNEL_ENCRYPTION` to force or skip the first. IPC is second because Curve is provisioned for `transport="tcp"` only, so the two cannot be combined; its `ip` is an absolute pid-stamped prefix in the Jupyter runtime dir, because jupyter_client's ipc default is the relative `kernel-ipc`, which would drop socket files in the repo root and collide between two instances.
  - `_new_client()` works around an upstream bug in `jupyter_client` 8.9.1: `get_connection_info()` `.decode()`s the keypair to `str` while `client()` feeds it into `Bytes` traits, so `manager.client()` on an encrypted kernel raises `TraitError` before a message moves. The keys are passed back as bytes through the `**kwargs` override `client()` documents.

- **`core/bash_session.py`** — `bash_session`: one persistent shell, so `cd`,
  exports, venvs and background jobs survive across calls. Same idea as
  `core/kernel.py`, driven over a pty with `pexpect`; a module-level singleton.
  It takes its shell from `[bash].shell` and `apply_shell_prelude()`, so it
  cannot drift from the stateless `bash` tool.
  - Framing: each command, a prompt reset and a `printf` of a per-spawn
    sentinel plus `$?` are sent as one brace group. bash reads `PROMPT_COMMAND`
    only between top-level reads, never inside a compound command, so the reset
    wins even against a command that reassigns `PROMPT_COMMAND` (conda,
    direnv).
  - Shells: bash, zsh and dash. `_PS1_RESET` is `PROMPT_COMMAND='PS1=""'` on
    bash, a `precmd()` function on zsh, and a plain `PS1=''` on dash, which has
    no prompt hook. zsh also needs `_ZSH_SESSION_PRELUDE`, which disables its
    line editor. fish, tcsh and ksh hang or need different grouping syntax and
    are unsupported.
  - Timeout: sends Ctrl-C, then checks with `os.tcgetpgrp` that bash owns the
    terminal again. A sentinel match alone is not enough, because a raw-mode
    program such as `less`, `vim` or `top` can echo the sentinel itself. If
    bash does not own the terminal, it respawns a fresh shell through the path
    `restart: true` uses and returns `state_reset: true`. The shell is spawned
    with SIGINT and SIGQUIT reset to their defaults (`_restore_default_signals`):
    a client started with them ignored (`nohup cmd &`) would otherwise pass that
    on, Ctrl-C could not stop a command, and every timeout would end in a respawn.
  - `local_tools.shutdown()` calls `bash_session.shutdown`.
- **`core/processes.py`** — `interactive_run`: spawns a command on a pty and
  answers its prompts (passwords, `[y/N]`, ssh host keys) from a `steps` script
  the model supplies. Each step carries exactly one reply source:
  - `send` — a literal reply the model writes.
  - `send_env` — the NAME of an environment variable, read locally.
  - `send_secret` — the NAME of a `pass` entry, resolved locally with
    `pass show`. Only the first line is used. A `pass show` that outlasts
    `_SEND_SECRET_TIMEOUT` returns an error asking the user to unlock the GPG
    key in their own terminal first.

  A real secret sent as a literal `send` passes through Anthropic's API twice,
  once to the model and once back in its tool call, so use `send_env` or
  `send_secret` for those. `pass` is GPG-backed and needs no desktop session;
  `secret-tool`/libsecret needs a keyring daemon and does not work headless.

  **Redaction.** `send_env` and `send_secret` replies are always treated as
  secret, whatever the step's `secret` field says. `_redact()` makes a single
  pass over the complete final transcript, so a child process that echoes the
  value back later is also scrubbed. It covers the forms in `_secret_forms()`:
  percent, form, HTML, JSON, hex, and base64 (both alphabets, padded or not,
  at every alignment, so `Authorization: Basic ...` is covered). Derived forms
  shorter than 6 characters are dropped because they would match ordinary text.
  A reversed or otherwise transformed copy is not caught.
  `test_processes.py` `check_secret_redacted_even_when_echoed_back_later`
  covers the echo case.

  **Name gate.** The model does not choose which entry gets decrypted.
  `resolve_secret()` decrypts only an entry whose name the user typed in one of
  their own messages this session. `CliApp._submit()` passes the typed or dictated text to `note_user_message()`, which records every vault entry named in it (whole-name
  match, not a substring) in `_confirmed_secret_entries`. Any other name, or
  `"?"`, returns the fixed prompt from `_select_entry_prompt()`
  (`"please select the cred name I need to use:"` plus every real entry) and
  decrypts nothing. A confirmed name stays confirmed for the rest of the
  session and for any use; it is not tied to the request it was named for.
  The match is on the whole name anywhere in the message, so a passing mention
  ("push it to github" with an entry named `github`) confirms it. A confirmed
  entry is typed into whatever page is open: a page that talks the model into
  filling its form receives the real value, and scrubbing does not cover that,
  because the value never returns through the model. A task delegated over MCP
  (`mcp_server.py` to `Chat.run`) never passes through `_submit()`, so it confirms
  nothing and a worker cannot decrypt a vault entry on its own.

  **Entry names.** `_select_entry_prompt()` reads `$PASSWORD_STORE_DIR`
  (default `~/.password-store`) and walks it for `*.gpg` filenames; nothing is
  decrypted. It does not parse `pass ls`, whose tree drawing loses the folder of
  a nested entry (`aws/prod` becomes `prod`, not a valid `pass show` argument).
  A failed `pass show` gets `_available_entries_hint()` appended: the entry
  names from `pass ls`, never values. `_SEND_SECRET_TIMEOUT` (30 s) bounds
  `pass show` because an uncached GPG key can raise a `pinentry` popup on the
  user's screen, and answering it takes longer than a few seconds; a timeout
  still fails clearly instead of waiting out the whole `interactive_run`
  timeout. `pass show` runs in its own process group (`_pass_show`) and a
  timeout kills the group, so the `gpg` it started does not outlive it.

  **Browser.** `browser_fill` takes exactly one of `value` or `value_secret`.
  A `value_secret` goes through the same `resolve_secret()` gate, is typed into
  the field, and is added to `_filled_secrets` in `core/browser.py`. Every
  browser tool result is scrubbed of those values before clipping, because a
  clip can cut a secret in half and leave a prefix. `test_browser_secret.py` runs against a local login form that
  reflects the password back.

- **`core/config_edit.py`** — `config_edit`: round-trip YAML (`ruamel.yaml`), TOML (`tomlkit`), and JSON edits that **preserve comments**, key order, and quoting, which `sed` and stdlib YAML silently destroy. Dotted key paths with `[index]` support, `$…` JSONPath for read-only queries (`jsonpath-ng`), and writes go through a temp file + `os.replace`.
- **`core/data.py`** — `sql_query`: DuckDB against CSV/Parquet/JSON files in place, no import step. One in-memory connection is reused for the session, so views and temp tables persist across calls.
- **`core/files.py`** — `trash`: `send2trash`, the only recoverable delete available here given there is no approval gate. Paths are made absolute (send2trash fails opaquely on relative ones) and `TrashPermissionError` is translated, since GIO refuses to trash from tmpfs mounts like `/tmp` and raises it with an empty message.
- **`core/text_embeddings.py`** — `text_embeddings`: POSTs to the HTTP endpoint set as `url` under `[embeddings]` in `config.toml` and returns the vectors it gets back. Anthropic has no first-party embeddings endpoint (the documented path is Voyage AI, a separate paid API), and this avoids wiring in a second SDK. Config is the extension point: `request_format` picks `"openai"` (`{"input": [...]}` in, `{"data": [{"embedding": [...]}]}` back, the shape most self-hosted servers speak) or `"simple"` (`{"text": [...]}`), and `_extract_vectors()` also accepts a bare `{"embedding": [...]}` or `{"embeddings": [[...]]}` response whichever format was sent. `api_key_env` mirrors `token_env`: the token lives in the environment, never in this committed file. The tool is declared to Claude unconditionally and returns an error naming the missing `url` instead of silently doing nothing. Config is re-read on every call, so a `config_edit` write applies on the next call with no restart.
- **`core/vision.py`** — `vision_query`: POSTs an OpenAI-compatible chat-vision request (`{"messages": [{"role": "user", "content": [{"type": "text", ...}, {"type": "image_url", ...}]}]}`) to the endpoint set as `url` under `[vision]`, asking about an image through your own vision-capable server instead of Anthropic's API. `image` accepts a local file path (read, base64-encoded and wrapped as a `data:` URI), an `http(s)://` URL, or an already-encoded `data:` URI, detected by prefix. `max_tokens` defaults to 4000 because a reasoning vision model can spend the whole budget on invisible `reasoning_content` and return an empty or truncated answer with no error, only `finish_reason: "length"`. **The tool has no fallback to Claude's own vision**: on failure (server unset, unreachable, timeout) it returns `{"status": "local_unavailable", "reason": ..., "image": ..., "prompt": ...}` and stops, echoing `image` and `prompt` so a Claude-vision attempt, if the user agrees to one, needs nothing re-read. That attempt is an ordinary conversational decision (the file editor's image view, or `browser_navigate` for a remote image), as the tool's `description` tells Claude. Config is re-read on every call.
- **`core/speak.py`** — `speak`: text-to-speech through Piper (`python3 -m piper`, PyPI package `piper-tts`, not `sudo apt install piper`, an unrelated GTK app of the same name), then plays the WAV through the configured PipeWire sink with `paplay`. Two non-raising decline reasons come back as a `status` field, the same shape as `vision_query`: `"disabled"` (`[speak].enabled` false, checked before anything touches the filesystem) and `"not_configured"` (`voice_model` unset or missing). Config is re-read on every call. Each call runs two subprocesses (synthesis, then playback), each under `[speak].timeout` (default 30).
- **`core/listen.py`** — `listen`: records from the configured PipeWire source with `timeout <N> parecord`, then transcribes the WAV in-process with `faster-whisper` (`WhisperModel(...).transcribe(...)`, CPU/CTranslate2). There is no second subprocess, unlike `speak.py`, because faster-whisper is a library with no CLI entry point. It uses the same `"disabled"`/`"not_configured"` status pattern as `speak.py` (`[listen].enabled` is checked before `device`, which is required). `model_size` (default `"base"`), `default_duration_seconds` (8) and `max_duration_seconds` (30, a hard cap on any requested duration) come from `[listen]`, re-read on every call, through the shared `_resolve_duration()`, which also sizes the timeout below so the two cannot drift. **`parecord`'s exit code 124 (it was cut off by `timeout`) is the normal success path**; only other non-zero exits are capture failures (device busy, bad device name). **`execute()` wraps the whole call in `asyncio.wait_for`** (`capture_timeout + max(60, duration*4) + margin`): `model.transcribe()` returns a lazy generator, so the real decoding runs on iteration, and without this bound a hung transcription blocks forever with no error. Like `midi1.py`'s in-process calls, this bounds the caller's wait, not the underlying thread.
- **`core/midi1.py`** — `midi1`: MIDI 1.0 device discovery and I/O. Messages and files go through `mido`; ports are ALSA sequencer clients the module opens through cffi (`_AlsaInput`, `_AlsaOutput`, needing `libasound.so.2`), so port I/O is Linux-only. `list_devices`, `open`, `close`, `send` and `poll` cover named-port discovery, channel, System Common and System Real-Time messages, and generic SysEx. `poll` returns the messages buffered on an input handle with a wall-clock `received_at`, and can block for `timeout_seconds` (capped at 60 s) on a `threading.Event`; an `overflow` entry marks lost messages and an `at_open` flag marks the burst a device sends as the input opens. Typed messages (`mtc_full`, `mmc` with the full Information-Field register, `masked_write` and `decode_mmc_response`, `msc`, `rpn`/`nrpn`, `gm_system`, `device_inquiry`/`device_control`, `channel_mode`, `midi_tuning`, `notation`, `mtc_cueing`/`mtc_cueing_nrt`, `file_dump`, `mtc_nak`, `mtc_quarter_frame_sequence`) are validated payloads built on the generic `sysex` mechanism, not separate code paths; `.mid` and `.syx` files are read and written through `mido`. The module opens its own ALSA clients because rtmidi's input queue holds 200 events and loses bursts (an `_AlsaInput` client has the kernel's 2000) and its output stops at 16,353 SysEx bytes (`_AlsaOutput` splits SysEx into 256-byte events). Active Sensing is dropped unless `open` passes `active_sensing: true`. Every hardware-touching call (`open`, `send`, `close`, and a blocking `poll`) runs through `asyncio.wait_for(asyncio.to_thread(...), timeout=...)`, so a hung driver cannot wedge the caller; that bounds the caller's wait but cannot kill the underlying thread. `close_all()` is wired into `local_tools.shutdown()` so open ports are released on exit. MIDI 2.0/UMP is a separate project.
- **`core/output.py`** — `clip(text, limit)`, the one truncation helper the local tool modules share (bash/editor/kernel/pexpect budget 12000 chars, browser 6000), plus `IMAGE_MEDIA_TYPES` and `image_result(...)`. The latter builds the `{"__kind__": "image", ...}` marker that a tool returns instead of a string when its result is pixels (file-editor/memory `view` on an image, every computer screenshot); `Chat._local_result_to_content` turns it into a real `image` content block.

- **`core/claude.py`** — thin Anthropic SDK wrapper, same as ResearchMesh's. It
  posts to `client.beta.messages.create` with an empty `BETAS`:
  `computer_toolset_20260801` needs no beta header, and the beta endpoint is a
  superset of the plain one. It returns `BetaMessage`, which is not a subclass of
  `Message`, so the isinstance checks use the `_RESPONSE_TYPES` tuple; without it
  the response object lands in `content` instead of its blocks. Top-level
  `cache_control` works on both endpoints.

  Constraints on `chat()`:
  - **No sampling parameters.** Current models (Sonnet 5, Opus 5, Opus 4.7+)
    reject a non-default `temperature`, `top_p` or `top_k`. Steer with
    `SYSTEM_PROMPT`.
  - **No `budget_tokens`.** Adaptive thinking replaced it; the old
    `{"type": "enabled", "budget_tokens": N}` is a 400. The depth knob is
    `output_config={"effort": …}`.
  - **`max_tokens=20000`**, shared by thinking and the reply. A lower cap let a
    single large `create` call be cut off mid-tool_use, leaving an unanswered
    `tool_use` that poisons every later turn. It stays under the SDK's
    ~21,333-token non-streaming ceiling (`self.client = Anthropic()` sets no
    `timeout=`, so `client.messages.create` raises "Streaming is required…"
    above it); streaming would need rework of how
    `core/chat.py` reads `response.content`, `stop_reason` and `usage` as one
    object.
  - **Prompt caching fails silently.** A prefix under the model's minimum (1024
    tokens on Sonnet 5) is not cached, with no error, and any early byte change
    invalidates everything after it. `CLAUDE_SHOW_USAGE=1` is the only way to
    confirm it is landing.

  **Per-model tool compatibility is self-healing.** Not every tool type works on
  every model (Haiku 4.5 rejects `computer_toolset_20260801`), and the API fails
  the whole request over one, so `/model swap` to such a model would 400 every
  turn. `Claude.chat()` catches a `BadRequestError` matching Anthropic's fixed
  "does not support tool types: ..." wording, parses the types, records them in
  `self._unsupported_by_model` (keyed by model), filters them out and retries
  once with a `[model compat]` console note. Later requests for that model
  filter up front, so only the first turn pays a retry and nothing is
  hand-maintained. `web_search` and `web_fetch` declare
  `allowed_callers: ["direct"]` in `claude_learned_schemas.py`: unset, Haiku
  rejects them ("does not support programmatic tool calling") because it cannot
  be a `code_execution` caller, which this project never uses.
- **Haiku 4.5 has no computer tool (known limitation).** The compat handler
  drops the toolset after the first rejected request per model per process, and
  every other tool keeps working. `computer_20250124` (beta header
  `computer-use-2025-01-24`) is accepted on Haiku but not declared: it needs a
  per-request beta header and a second executor path (`name: "computer"` with
  `input.action`; `computer.handles("computer")` is False on purpose, asserted in
  the smoke test), and declaring both computer tools in one request is a 400 on
  every model. After the computer tool was used on another model in the same
  conversation, `/model swap` to Haiku fails every turn with a 400
  (`toolset_name 'computer' on a tool_use block is not the family of a declared
  toolset entry (no toolset entry is declared)`); swap back or `/clear`.

- **`core/tools.py`** (`ToolManager`) — the MCP↔Anthropic bridge (remote server tools only). `get_all_tools` aggregates tool schemas across all MCP clients, called once per user turn by `Chat`, not per tool-use iteration. `execute_blocks` runs a list of `tool_use` blocks against the owning client, resolving owners through one `_tool_owners` map per call.
- **`core/cli.py`** (`CliApp`) — a minimal `prompt_toolkit` REPL (history and styling) that delegates all real work to `Chat`. Commands: `/think <message>`, `/clear` (also `/reset`), `/voice [on|off]`, `/listen [N]`, and `/model` or `/model swap <name/index>`.
  - **`/clear`** is the recovery path from the two failures that last for the life of the process: an unanswered `tool_use` block, which stays in `self.messages` and fails every later request, and a conversation past the context window. Both look the same ("it started 400ing and won't stop"), and without `/clear` the only way out is killing the app, which takes the browser, the kernel and every MCP connection with it. It leaves all of those, and `/memories`, alone. `Chat._report_api_failure` says which failure you hit by checking for orphaned `tool_use` ids directly, and its size report is in characters, not tokens, because `count_tokens` rejects the server tools (`web_search`, `web_fetch`).
  - **`/voice`** toggles `self.auto_speak` (default off), which gates only whether replies are also spoken through `speak.py`'s `_run` after a turn. It does not affect whether `speak` and `listen` are reachable as tools (`[speak].enabled`, `[listen].enabled`) or whether a `/listen` dictation is submitted.
  - **`/listen [N]`** records through `listen.py`'s `_run` (`N` overrides `[listen].default_duration_seconds` for that call), prints the transcript, and auto-submits it as a turn through `_submit()`, the method typed input also uses. There is no edit step: a garbled transcript is sent as is. `_submit()` also passes the text to `processes.note_user_message()`, which is how a vault entry name you type or dictate becomes confirmed.
  - **`/model`** (bare) lists `config.toml`'s `[claude] claude_models` with 1-based indices and marks the current one. `/model swap <name or index>` sets `chat.claude_service.model` directly (`Claude.chat()` reads it fresh each call, so it applies on the next turn). It is session-only: it never writes `config.toml`, so a new session starts on `claude_models[0]`. The config read (`load_claude_models`) and the index and name matching (`resolve_model_swap`) live in `core/claude.py`, kept pure and testable (`smoke_test.py`'s `check_model_command`); `cli.py` only prints and mutates state. An unrecognised name or index, or a bare `/model swap`, is rejected with a message and does not swap, as with `/voice` and `/listen`.
- **`mcp_client.py`** (`MCPClient`) — async context-manager over an MCP `ClientSession`, supporting three transports: `stdio` (spawn `command`+`args`), `sse` (`url`), and `http` (Streamable HTTP `url`) — the last two accept `headers` for auth (e.g. a Bearer token). Exposes `list_tools`, `call_tool`, `list_prompts`, `get_prompt`, `read_resource`.

- **`mcp_server.py`** — the opposite direction: serves this agent to an MCP client (Claude Code, or a ResearchMesh-Router worker entry) as two tools, so the app is a server and a client at once.
  - **`delegate(task, session, thinking)`** is the whole agent in one call. It is one tool, not 27, because `bash_20250124`, `memory_20250818` and `computer_toolset_20260801` are learned schemas Claude is trained on the exact wire shape of; re-exporting them over MCP's generic schema would hand the caller a lookalike with the trained shape discarded. Wrapping `Chat.run()` keeps them intact and keeps `SYSTEM_PROMPT` in force, which is why the overlap with the caller's own `bash` and editor is deliberate and why the 30-50 tool ceiling does not apply. One `Chat` is kept per `session` id so a caller can follow up on a previous delegation; a new id starts clean. Calls are serialised behind an `asyncio.Lock`: one mouse, one browser page, one kernel. The server `chdir`s to the repo root, because a client starts it with the client's project as cwd and `CLAUDE_MEMORY_DIR` defaults to a relative `memories`.
  - **The stdout guard must run before any `core/` import.** On stdio, fd 1 is the JSON-RPC channel, and the app prints to stdout in many places, so the guard `dup`s fd 1 for JSON-RPC and points fd 1 itself at stderr. Doing it at the file-descriptor level, not by reassigning `sys.stdout`, also catches subprocesses that inherit fd 1, such as a downstream stdio MCP server's startup banner. It is stdio-only: under HTTP fd 1 is not the wire, and `sys.stdout` is set line-buffered instead, because Python block-buffers a redirected stdout and would otherwise hold the whole startup log, including the `listening on ...` line, until exit.
  - **`model(action, arg)`** is the second tool: it lists or swaps this instance's Claude model remotely, so a caller such as ResearchMesh-Router can change a worker's model. It shares none of `delegate`'s machinery (no `Chat`, no session, no API call, no lock) and mirrors `core/cli.py`'s `/model` and `/model swap` (the same `load_claude_models()` and `resolve_model_swap()` calls from `core/claude.py`). `action: "list"` reads this instance's `config.toml` and marks the current model; `action: "swap"` (needs `arg`, a 1-based index or model name) mutates the module-level `_claude` object that every `delegate` session shares, so it applies to the next `delegate` call from any session, until changed again or the process restarts. A bad `action`, a missing `arg` or an unrecognised name or index returns `is_error=True` with a message and never raises.
  - **Transports.** `--transport stdio|streamable-http` (default stdio; `--host` defaults to `127.0.0.1`) drives both from the same `Server` object: the low-level `Server.run()` takes only read and write streams, so a transport supplies streams and is not a different server. Do not port this to the high-level server (`FastMCP` in mcp 1.x, `MCPServer` in 2.0): its stdio path calls `stdio_server()` with no arguments and insists on the real `sys.stdout`, the descriptor the guard has to take away.
  - **HTTP auth is `config.toml`'s `token_env` contract inverted.** `--token-env` (default `RESEARCHMESH_MCP_TOKEN`) names the variable holding a bearer token; the token never appears in a file or argv, and an unset variable means unauthenticated, not an error, matching `build_client()`'s "no `token_env` connects unauthenticated". Another ResearchMesh can consume this one with an ordinary `{ url = …, token_env = … }` entry. `_bearer_auth()` is ASGI middleware around the `Mount`, comparing with `hmac.compare_digest`. Starlette 307-redirects `/mcp` to `/mcp/` before the mounted app runs, so an un-slashed request is redirected rather than rejected with 401; the endpoint is still guarded, so a 307 in testing is not the guard failing.
  - **TLS** is `--ssl-certfile` and `--ssl-keyfile`, passed straight to `uvicorn.Config`, and it is the serving half only: consuming an `https://` worker needs no app-side TLS setup, because the HTTP client verifies certificates with its normal runtime trust configuration. A private CA or company certificate works only if that CA is already trusted by the client environment, or `SSL_CERT_FILE` or `SSL_CERT_DIR` is set for the process, so `create_mcp_http_client` exposes no `verify` parameter to plumb. A company CA or a paid certificate therefore needs work on the worker and none on the consumer. The two flags are required together, because uvicorn ignores a lone `--ssl-certfile` and serves plain HTTP; `_run_http()` refuses instead, and checks both paths exist before the port opens, since uvicorn's own failure for a missing file is a bare traceback. The startup line's scheme follows the flags, and a plaintext non-loopback bind says so alongside the unauthenticated warning.

### Key conventions

- **Two parallel tool systems.** MCP tools live on remote servers (any number, listed under `[mcp]`) and are discovered/executed via `ToolManager`. Local tools are declared and executed in-process, aggregated by `local_tools`. `Chat` merges both into one `tools=` list and routes execution by owner.
- **"Learned" vs custom.** The distinction is whether Claude already knows the schema, *not* which file it lives in. `claude_learned_schemas.py` holds the small Anthropic-defined tools (bash, text editor, and the two server-side web tools); `memory.py` and `computer.py` are also Anthropic-defined but got their own modules because their implementations are substantial. None of them carry descriptions — Claude is already trained on those schemas, so writing one is at best redundant and at worst contradicts what it was trained on. Every other local module holds fully custom tools Claude learns at runtime from its descriptions. Keep all of them separate from `tools.py`, which is strictly the MCP bridge.
- **A learned tool is exempt from the "must beat `bash`" test below** — the schema already exists in the model, so the only question is whether you want the capability, not whether it earns a slot on novelty.
- **A new local tool must beat `bash` at something structural** — statefulness (`python`), interactivity (`interactive_run`), a correctness guarantee (`config_edit`), recoverability (`trash`), or context economy — since Claude can already shell out to any CLI. Wrapping a command bash could run unaided just spends a tool slot.
- **Tool-selection accuracy degrades past roughly 30–50 loaded tools.** 27 local + whatever the connected MCP servers advertise leaves headroom; prefer one tool with a mode parameter (as `document_convert` and `config_edit` do) over one tool per variation.
- **`web_search` (discovery) and the browser tool (navigate/interact) are complementary**, not redundant — don't reimplement search inside Playwright.
- Add an MCP server by passing its script as argv (stdio) or by declaring it under `[mcp].servers`; its tools then appear to Claude automatically.
- `bash` is **stateless between calls** (a fresh subprocess each time; `cd` and env do not persist). `bash_session`, the `python` kernel, the browser session and the DuckDB connection **are** stateful within a session.
- **No approval gating** — Claude executes whatever bash commands, file edits, browser actions, conversions, kernel code, interactive commands, and MCP server tools it chooses. This is intended for local dev only, and is why `trash` exists.
- The app must run from the **repo root** (`main.py` and `mcp_client.py` live there; `core/` is the importable subpackage).

### Adding or removing a tool

A tool's name and behaviour are described in about ten places, and nothing enforces agreement between them. Missing any one leaves a doc that lies or a prompt that names a tool Claude doesn't have. Touch them all:

1. The module in `core/` — exposing `TOOLS`, `handles(name)`, and `await execute(name, input)`.
2. `MODULES` in `core/local_tools.py` (the import *and* the list entry), and its `shutdown()` tuple if the tool holds a resource.
3. `requirements.txt` and `pyproject.toml`, if it has a third-party dependency.
4. `SYSTEM_PROMPT` in `core/chat.py` — both the explicit roster **and** any tool-choice
   guidance, which is the half no automation can generate.
5. `README.md` — the tool table, the per-tool package table, the project layout, and the
   tool count (stated more than once).
6. `CLAUDE.md` — the module bullet in Architecture, the count in Overview, and the count in
   Key conventions.
7. `BETAS` in `core/claude.py`, **only if the tool's schema is beta-gated**. Nothing declared today needs it, so `BETAS` is empty. If a tool ever does, export the flag from the tool's own module so the header and the tool version cannot drift. This has global blast radius: the header rides every request, and a missing one 400s the whole conversation, not just that tool.
8. `README.md`'s environment-variable table and `CLAUDE.md`'s "Runtime configuration", if
   the tool reads any env var of its own.

`main.py` does not need touching: its MCP messages do not enumerate tools.

Then verify instead of trusting the list: `grep -rni <toolname>` across `*.py`/`*.md`/`*.toml`/`*.txt` should come back empty on a removal, and the roster inside `SYSTEM_PROMPT` should still match `local_tools.TOOLS` exactly. Counting `len(local_tools.TOOLS)` beats counting by hand.

### Removed from the original tutorial

The tutorial's bundled stdio document server and `core/cli_chat.py` (the `@mention` and `/command` document-resource layer) are gone: that `docs://documents` system was tutorial scaffolding. Do not reintroduce a `doc_client`. Today's `mcp_server.py` is unrelated to the deleted one: it is the delegation server described under Architecture.
