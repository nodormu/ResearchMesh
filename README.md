# ResearchMesh, a Linux CLI Research Client for Claude

> *Unofficial, community-built client — not affiliated with or endorsed by Anthropic. "Claude" is a trademark of Anthropic.*

                    ┌── /think
                    │
                    ├── Bash / Linux
                    ├── Filesystem
                    ├── LibreOffice
     ResearchMesh ──┼── Playwright
                    ├── MCP #1
                    ├── MCP #2
                    ├── MCP #3
                    └── ...

A terminal chat client for the Anthropic API that hands Claude real tools on your own Linux
machine: a shell, a file editor, a headless browser it can surf with, a persistent Python
session, and document conversion. Ask it something and it can look it up, read the pages,
run the commands, and hand you back a finished `.docx` — in one conversation.

## What it can do

**18 local tools**, plus whatever your MCP servers expose:

| Tool | For |
|---|---|
| `bash` | Shell commands as your user. Stateless — fresh subprocess each call |
| `str_replace_based_edit_tool` | View, create, and edit files |
| `web_search` · `web_fetch` | Anthropic's server-side search and page fetch |
| `memory` | A `/memories` store that **persists across sessions** — the only state that outlives the process |
| `computer` | Screenshots plus mouse/keyboard control of your desktop. **Needs an X11 session** ([see below](#full-setup-detail)) |
| `browser_navigate` · `_links` · `_click` · `_fill` · `_extract` · `_back` | Headless [Playwright](https://playwright.dev/) — real DOM surfing: renders JavaScript, follows links, fills forms |
| `document_convert` | LibreOffice + pandoc. Markdown → `.docx`/`.odt`/`.pdf`, or any office format to any other |
| `python` | Persistent IPython kernel — **variables survive between calls** |
| `interactive_run` | Commands that prompt: passwords, `[y/N]`, ssh host keys, installers, REPLs |
| `config_edit` | Edit YAML/TOML/JSON **without destroying your comments** |
| `sql_query` | DuckDB straight against CSV/Parquet/JSON — no import step |
| `trash` | Recoverable deletes instead of `rm` |

Claude chooses the tools and keeps working until it has an answer.

## Quick start

You need **Linux**, **Python 3.11+**, and an Anthropic **API key** — this is an API client,
so a Claude subscription won't work.

**MCP servers are optional.** The `[mcp]` block in `config.toml` ships with example
servers pointing at a private LAN address — replace those URLs with your own, or set
`enabled = false` to run on the 18 local tools alone.

mcp_client.py is just a script to connect to your MCP server and pull a list of tools, be sure you change the IP address in the code.

```bash
sudo apt install python3 python3-venv python3-dev build-essential libreoffice pandoc

python3 -m venv ~/claude-chat-plus-more-tools
source ~/claude-chat-plus-more-tools/bin/activate
pip install -r requirements.txt

playwright install chromium           # pip installs the package, not the browser
sudo playwright install-deps chromium

export ANTHROPIC_API_KEY=sk-ant-...   # add to ~/.bashrc to keep it, and put your N8N API key in .bashrc as well, or you will have to rewrite code to make it elsewhere if not exporting it before runnning main.py

python main.py
```

Then just type. **`/think <message>`** gives Claude longer to reason on hard problems;
**Ctrl-C** exits and shuts everything down cleanly.

**MCP servers are optional** — all 18 local tools work without any of them.

## Try it

```
Run uname -a and tell me what kernel I'm on.

What's the latest stable Python release? Cite your source.

Open news.ycombinator.com, list the top links, then open the first one and summarise it.

Load ~/data.csv and show me the five biggest rows by revenue.

Write a one-page summary of the Raft consensus algorithm as markdown,
then convert it to a .docx in ~/Documents.

Give me a Cisco IOS 17.15 config for a 9200 24-port switch: VTP client so my VLAN
database isn't overwritten, two uplinks active/standby at 1 Gbps, all 24 ports up and
ready for voice + data VLANs pushed from the VLAN server, uplink trunk on VLAN 100.
Note what I need to change for my environment, then write it to /tmp/switch.txt.

What is the airspeed velocity of an unladen swallow?
```

## Configuration

Non-secret settings live in `config.toml`. Secrets stay in the environment — the app does
**not** read a `.env` file.

```toml
[claude]
model = "claude-sonnet-5"   # CLAUDE_MODEL overrides this

[mcp]
enabled = true              # false skips every server; local tools still work

# One line per server. Add as many as you like — every reachable/launchable one
# connects and its tools join the same list Claude sees. Two entry shapes:
#
#   Streamable HTTP (a server already running elsewhere):
#     url        the server's endpoint
#     token_env  names the environment variable holding that server's bearer
#                token; omit it if the server needs none
#
#   stdio (a local server main.py launches itself, no separate process to start
#   by hand — it talks JSON-RPC over the subprocess's stdin/stdout):
#     command    full argv as a list, e.g. ["node", "/path/to/bin.js"]
#     env        optional table of extra environment variables for it
servers = [
  { name = "n8n",    url = "http://192.168.2.12:5678/mcp-server/http", token_env = "N8N_MCP_TOKEN" },
  { name = "alpaca", url = "http://192.168.2.12:8000/mcp" },
  { name = "unreal", command = ["node", "$HOME/unreal-mcp/dist/bin.js"] },
]
```

A server that's unreachable (http) or fails to launch (stdio) prints a warning and is
skipped, so one being down doesn't stop the app. Tokens are never written in this file —
only the *name* of the variable that holds them.

`~`, `$USER`, `$HOME` and `${ANY_VAR}` are expanded in `command`, `url` and the *values* of
`env`, so the checked-in config doesn't have to name your home directory or mount point.
(`env`'s keys are variable names and are left alone.) An undefined variable is left as
written rather than expanding to nothing, so a typo shows up in the startup warning instead
of becoming a silently wrong path. Absolute paths beyond that are still machine-specific —
those you edit by hand.

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Required |
| *(per server)* | Whatever each `token_env` names, e.g. `N8N_MCP_TOKEN` |
| `CLAUDE_MODEL` | Override the model |
| `CLAUDE_SHOW_USAGE=1` | Print token and prompt-cache counts per request |
| `CLAUDE_MEMORY_DIR` | Where `memory` stores `/memories` (default `./memories`) |
| `CLAUDE_DISPLAY_SIZE` | Logical screen size `computer` reports, e.g. `1280x800` |
| `CLAUDE_COMPUTER_FORCE=1` | Let `computer` try anyway on a Wayland session |

## Good to know

- **There is no approval prompt.** Claude runs the commands and file edits it decides on, as
  your user, with no y/n in between. Built for local development. `trash` exists so deletes
  are at least recoverable.
- It's your API key: one request can fan out into many tool calls (capped at 30 per turn).
- `bash` forgets everything between calls — `cd`, exports, activated venvs. Chain with `&&`,
  or use `python`, which keeps state.
- Ask for files by absolute path. If Claude offers a download link instead, tell it you need
  the file written to disk.
- Nothing under `/tmp` can be trashed (tmpfs has no trash), so deletes there would be
  permanent — the tool says so rather than pretending.
- **`computer` does not work on Wayland.** It drives the screen through X11/XTEST, which
  Wayland compositors ignore by design, so clicks and keystrokes never reach native windows
  and screenshots come back blank. Check with `echo $XDG_SESSION_TYPE`; if it prints
  `wayland`, the tool refuses up front and tells you why rather than clicking into the void.
  Fix it with an Xorg session or `xvfb-run -s '-screen 0 1280x800x24' python main.py` —
  details under [Full setup detail](#full-setup-detail). Every other tool is unaffected.
- If Sonnet gets inconsistent on a complicated multi-tool request, set `model` to an Opus one.
- Optional packages are imported only when a tool is used, so a missing one breaks just that
  tool and tells you what to install.
- If a tool reports a missing package that `requirements.txt` already lists (e.g.
  `sql_query`'s `duckdb`, or `config_edit`'s `ruamel.yaml`/`jsonpath-ng`), that's not a docs
  gap — your venv just predates that line. Everything in `requirements.txt` is a `>=` floor
  rather than a pin (there's no lockfile), so a venv can satisfy it and still miss a package
  added later. Re-run `pip install -r requirements.txt`; you don't need to restart the app,
  because each optional package is imported at the moment its tool is called.
- No tests or linters are wired into the project — no `[tool.ruff]`/`[tool.black]` in
  `pyproject.toml`, no `.pylintrc`, nothing runs automatically. Sanity-check edits with
  `python -m py_compile core/*.py main.py`. If your venv happens to have `pylint`/`mypy`/
  `black`/`ruff` installed (none are project dependencies — add them yourself if you want
  them) or the system has `shellcheck`, they're safe to run by hand.
- **If you run `ruff`, don't dismiss the whole report.** This codebase deliberately uses
  broad `except Exception`/`except BaseException` at tool-execution boundaries throughout
  `core/` (each local tool must catch anything and return an error string rather than crash
  the chat loop), so most `BLE001` (blind-except) findings really are by design — on ruff
  0.16.3 that's 30 of the 45 findings in `core/`. The other 15 aren't, and some are real:
  `B006` flags a genuine mutable-default argument in `core/claude.py`. Counts are
  version-sensitive — `BLE001` isn't in ruff's historical default rule set, so an older ruff
  won't report it at all.

<a id="full-setup-detail"></a>

<details>
<summary><b>Full setup detail</b> — OS libraries, document tools, which package backs which tool</summary>

**Playwright.** `pip` installs the Python package but not the browser or its OS libraries:

```bash
playwright install chromium            # the browser binary
sudo playwright install-deps chromium  # OS libraries (e.g. libmanette)
```

`playwright install` with no browser name fetches all three engines; this app only launches
Chromium, so the argument is worth keeping.

**Document conversion.** `soffice` (LibreOffice) handles docx/odt/xlsx/pptx/html/rtf/txt and
PDF output, each call in a throwaway user profile so two conversions can't collide on the
profile lock. `pandoc` handles markdown, because `soffice` has no dependable markdown
import; `md → pdf` goes through odt on the way, since pandoc's own PDF writer would need a
LaTeX engine. `libreoffice-writer`/`-calc`/`-impress` alone are enough if you don't want the
whole suite.

**Computer use needs X11.** The `computer` tool synthesises input through X11/XTEST, which
Wayland compositors deliberately ignore — on a Wayland session clicks and keystrokes never
reach native windows and screenshots come back blank, so the tool refuses up front and says
so instead of failing silently. Check with `echo $XDG_SESSION_TYPE`. Options:

```bash
# 1. Log in to an "Xorg"/"X11" session at your display manager, or
# 2. Run the whole client inside a nested X server:
sudo apt install xvfb
xvfb-run -s '-screen 0 1280x800x24' python main.py
# 3. XWayland-only setup and you want to try regardless:
export CLAUDE_COMPUTER_FORCE=1
```

The tool reports a fixed logical screen size (`CLAUDE_DISPLAY_SIZE`, default `1280x800`)
and downscales every screenshot to exactly that, scaling Claude's coordinates back up to
your real resolution. That's what keeps clicks landing where Claude aims — the declared
size and the image it sees can never drift apart. Below roughly `1280x720`, accuracy drops.

**Memory** writes to `./memories` by default (`CLAUDE_MEMORY_DIR` to relocate). Claude sees
it as `/memories`; every command is confined to that directory, so a traversal path like
`/memories/../../.ssh/id_rsa` is rejected rather than served. It's a private scratchpad for
Claude, not a place for your project files — and it persists until you delete it.

**Optional Python packages** (all in `requirements.txt`; each is imported lazily):

| Tool | Needs |
|---|---|
| `python` | `jupyter_client`, `ipykernel` |
| `interactive_run` | `pexpect` |
| `config_edit` | `ruamel.yaml` (YAML), `tomlkit` (TOML), `jsonpath-ng` (`$…` queries); JSON needs nothing |
| `sql_query` | `duckdb` |
| `trash` | `send2trash` |
| `computer` | `pyautogui`, `pillow` — **plus an X11 display** (see below) |
| `memory` | nothing — standard library only |

To drop a tool entirely, remove its module from `MODULES` in `core/local_tools.py`.

**Environment variables** must be exported for the user account you launch as — `main.py`
calls `os.getenv()` directly. Put them in `~/.bashrc` for interactive shells, or
`~/.bash_profile` / `~/.profile` for login shells (e.g. SSH). Note `export`, and **no spaces**
around `=`; `VAR = value` is a bash syntax error. Then open a fresh shell or `source` it, and
check without revealing anything:

```bash
echo "key: ${ANTHROPIC_API_KEY:+set}  token: ${N8N_MCP_TOKEN:+set}"   # per your token_env names
```

**Built and tested on** Ubuntu 26.04 LTS (kernel 7.0.0), Python 3.14.4, Playwright 1.61.0.
`pyproject.toml` requires 3.11+ (the floor is `tomllib`, used by `main.py`); 3.14 is just what
it was run on. The `install-deps` step assumes a Debian/Ubuntu `apt` system.

</details>

<details>
<summary><b>HTTPS and TLS</b> — for an MCP server with a self-signed or private-CA certificate</summary>

A server URL may be `http://` or `https://`. TLS is verified by the `httpx` client inside
`mcp_client.py`, offline, against a local CA bundle — the CA is not contacted at connect time.

A publicly-signed certificate (Let's Encrypt, DigiCert, …) works with no configuration. A
self-signed or internal-CA certificate isn't in `certifi`, so point `httpx` at a bundle that
contains your CA:

```bash
export SSL_CERT_FILE=/path/to/your-ca-chain.pem   # or SSL_CERT_DIR for a hashed dir
```

Two things that catch people out:

- `SSL_CERT_FILE` **replaces** the default trust store rather than adding to it. If the same
  process also needs public HTTPS hosts, concatenate:
  `cat "$(python -m certifi)" your-ca.pem > combined-ca.pem`
- Your server (or its reverse proxy) must present its **full chain**. A missing
  intermediate is the most common "the cert is valid but it still won't connect" cause, and
  the fix is on the server — the client only needs the root.

The OS trust store (`/etc/ssl/certs`) does not affect this app.

</details>

<details>
<summary><b>Project layout and extending</b></summary>

```
main.py                          entrypoint — connects the MCP servers, wires Chat + REPL
mcp_client.py                    MCP client (stdio / SSE / Streamable HTTP)
CLAUDE.md                        architecture + conventions, for AI coding agents
core/
  chat.py                        agentic loop, tool routing, SYSTEM_PROMPT
  claude.py                      Anthropic SDK wrapper
  local_tools.py                 registry of every locally-executed tool
  tools.py                       MCP <-> Anthropic bridge
  claude_learned_schemas.py      bash, file editor, web_search, web_fetch
  memory.py                      /memories store, persists across sessions
  computer.py                    screenshots + mouse/keyboard (X11 only)
  browser.py                     Playwright DOM surfing
  documents.py                   LibreOffice / pandoc conversion
  kernel.py                      persistent IPython kernel
  processes.py                   pexpect — commands that prompt
  config_edit.py                 comment-preserving YAML/TOML/JSON edits
  data.py                        DuckDB queries
  files.py                       recoverable deletes
  output.py                      shared output trimming + image results
  cli.py                         prompt_toolkit REPL
```

- **Add an MCP server:** add an entry under `[mcp].servers` in `config.toml` — see
  "Configuration" above for both entry shapes (`url` for Streamable HTTP, `command` for a
  local stdio server main.py launches itself). Its tools appear to Claude automatically once
  it connects. A one-off Python stdio script can also be passed as an argument instead
  (`python main.py path/to/server.py`) without touching config.toml.
- **Add a local tool:** write a module exposing `TOOLS`, `handles(name)`, and
  `async execute(name, tool_input)`, then add it to `MODULES` in `core/local_tools.py`.
  That's the only registration step. Update `SYSTEM_PROMPT` in `core/chat.py` too — it
  describes the tool set to Claude.
- **Keep the list lean.** Tool-selection accuracy degrades past roughly 30–50 tools, so prefer
  one tool with a mode parameter over several near-duplicates, and don't wrap a command
  `bash` could already run.

Check every configured server on its own with `python mcp_client.py` — it connects to each
in turn, lists its tools, and reports failures without starting the chat.

</details>

<details>
<summary><b>Optional: MCP Inspector</b> — for debugging an MCP server (needs Node)</summary>

This project is **pure Python; Node.js is not a dependency.** For hand-calling the tools your
MCP endpoint exposes, the [MCP Inspector](https://github.com/modelcontextprotocol/inspector)
runs on demand with no install step:

```bash
npx @modelcontextprotocol/inspector@latest
```

An external debugging aid, nothing in the repo depends on it.

</details>

## License

[MIT](LICENSE) — use it, fork it, ship it. No warranty; see the file for the full text.
