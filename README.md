# Linux CLI Research Client for Claude

> *Unofficial, community-built client — not affiliated with or endorsed by Anthropic. "Claude" is a trademark of Anthropic.*

A terminal chat client for the Anthropic API that hands Claude real tools on your own Linux
machine: a shell, a file editor, a headless browser it can surf with, a persistent Python
session, and document conversion. Ask it something and it can look it up, read the pages,
run the commands, and hand you back a finished `.docx` — in one conversation.

## What it can do

**16 local tools**, plus whatever your MCP servers expose:

| Tool | For |
|---|---|
| `bash` | Shell commands as your user. Stateless — fresh subprocess each call |
| `str_replace_based_edit_tool` | View, create, and edit files |
| `web_search` · `web_fetch` | Anthropic's server-side search and page fetch |
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

```bash
sudo apt install python3 python3-venv python3-dev build-essential libreoffice pandoc

python3 -m venv ~/claude-chat-plus-more-tools
source ~/claude-chat-plus-more-tools/bin/activate
pip install -r requirements.txt

playwright install chromium           # pip installs the package, not the browser
sudo playwright install-deps chromium

export ANTHROPIC_API_KEY=sk-ant-...   # add to ~/.bashrc to keep it

python main.py
```

Then just type. **`/think <message>`** gives Claude longer to reason on hard problems;
**Ctrl-C** exits and shuts everything down cleanly.

**MCP servers are optional** — all 16 local tools work without any of them.

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

# One line per server. Add as many as you like — every reachable one connects and
# its tools join the same list Claude sees. token_env names the environment
# variable holding that server's bearer token; omit it if the server needs none.
servers = [
  { name = "n8n",    url = "http://192.168.2.12:5678/mcp-server/http", token_env = "N8N_MCP_TOKEN" },
  { name = "alpaca", url = "http://192.168.2.12:8000/mcp" },
]
```

A server that's unreachable prints a warning and is skipped, so one box being down
doesn't stop the app. Tokens are never written in this file — only the *name* of the
variable that holds them.

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Required |
| *(per server)* | Whatever each `token_env` names, e.g. `N8N_MCP_TOKEN` |
| `CLAUDE_MODEL` | Override the model |
| `CLAUDE_SHOW_USAGE=1` | Print token and prompt-cache counts per request |

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
- If Sonnet gets inconsistent on a complicated multi-tool request, set `model` to an Opus one.
- Optional packages are imported only when a tool is used, so a missing one breaks just that
  tool and tells you what to install.
- No tests or linters. Sanity-check edits with `python -m py_compile core/*.py main.py`.

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

**Optional Python packages** (all in `requirements.txt`; each is imported lazily):

| Tool | Needs |
|---|---|
| `python` | `jupyter_client`, `ipykernel` |
| `interactive_run` | `pexpect` |
| `config_edit` | `ruamel.yaml` (YAML), `tomlkit` (TOML), `jsonpath-ng` (`$…` queries); JSON needs nothing |
| `sql_query` | `duckdb` |
| `trash` | `send2trash` |

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
  browser.py                     Playwright DOM surfing
  documents.py                   LibreOffice / pandoc conversion
  kernel.py                      persistent IPython kernel
  processes.py                   pexpect — commands that prompt
  config_edit.py                 comment-preserving YAML/TOML/JSON edits
  data.py                        DuckDB queries
  files.py                       recoverable deletes
  output.py                      shared output trimming
  cli.py                         prompt_toolkit REPL
```

- **Add an MCP server:** pass a stdio server script as an argument
  (`python main.py path/to/server.py`), or add another `MCPClient(...)` in `main.py`
  (`transport="http"` for another HTTP server). Its tools appear to Claude automatically.
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
