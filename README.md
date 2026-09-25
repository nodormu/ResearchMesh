# ResearchMesh, a Linux CLI Research Client for Claude

> *Unofficial, community-built client — not affiliated with or endorsed by Anthropic. "Claude" is a trademark of Anthropic.*

                    ┌── /think
                    ├── /clear
                    ├── /voice
                    ├── /listen
                    ├── /model
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
session, desktop control, and document conversion. Ask it something and it can look it up,
read the pages, run the commands, and hand you back a finished `.docx` — in one conversation.

It works in both directions: it connects out to your own MCP servers, and it can itself be
added to **Claude Code** as one, so Claude Code can hand it the jobs it can't do —
[see below](#mcp-in-both-directions).

## What it can do

**24 local tools**, plus whatever your MCP servers expose:

| Tool | For |
|---|---|
| `bash` | Shell commands as your user via `/bin/bash` by default. Stateless — fresh subprocess each call. See `[bash]` in config.toml to use a different shell instead (e.g. zsh) |
| `str_replace_based_edit_tool` | View, create, and edit files |
| `web_search` · `web_fetch` | Anthropic's server-side search and page fetch |
| `memory` | A `/memories` store that **persists across sessions** — the only state that outlives the process |
| `computer` | Screenshots plus mouse/keyboard control of your desktop. **Needs an X11 session** ([see below](#setup-linux)) |
| `browser_navigate` · `_links` · `_click` · `_fill` · `_extract` · `_back` | Headless [Playwright](https://playwright.dev/) — real DOM surfing: renders JavaScript, follows links, fills forms |
| `document_convert` | LibreOffice + pandoc. Markdown → `.docx`/`.odt`/`.pdf`, or any office format to any other |
| `python` | Persistent IPython kernel — **variables survive between calls** |
| `bash_session` | Persistent shell — **cd/env/venvs/background jobs survive between calls** |
| `interactive_run` | Commands that prompt: passwords, `[y/N]`, ssh host keys, installers, REPLs |
| `config_edit` | Edit YAML/TOML/JSON **without destroying your comments** |
| `sql_query` | DuckDB straight against CSV/Parquet/JSON — no import step |
| `trash` | Recoverable deletes instead of `rm` |
| `text_embeddings` | Vector embeddings from an HTTP embedding server you configure — self-hosted or a paid API both work. See `[embeddings]` in config.toml for worked examples |
| `vision_query` | Ask a question about an image via a vision-capable chat server you configure — self-hosted or a paid API both work. See `[vision]` in config.toml for worked examples |
| `speak` · `listen` | Local text-to-speech (Piper) and speech-to-text (faster-whisper) through your own speaker/mic — no cloud audio API. Disabled by default; see `[speak]`/`[listen]` in config.toml, including first-time device setup |
| `midi1` | MIDI 1.0 device discovery and I/O via `mido`/`python-rtmidi` — list ports, open/close, send/poll channel and system messages, SysEx, and read/write `.mid`/`.syx` files |

Claude chooses the tools and keeps working until it has an answer.

## Good to know

- **There is no approval prompt.** Claude runs commands and file edits as your user, no
  y/n in between. Built for local development. `trash` exists so deletes are recoverable.
- **This is meant to be an AI *employee*, not just an unsupervised agent.** The
  OS-level restrictions below are the last line of defense, but the fuller model goes
  further: give it its own email address, let it talk to humans and other AIs in
  Teams or Slack like any other coworker, and route its actual work through the same
  systems everyone else's work goes through — a CRM/CMDB (ServiceNow, ConnectWise,
  whatever the organization already runs) as its system of record, change tickets
  opened for anything that touches production. Those are examples, not a fixed list.
  None of that is built into this app's 24 tools directly; it's what
  [MCP, in both directions](#mcp-in-both-directions) is *for* — connect it to an
  email MCP server, a Teams/Slack one, your CMDB's — and it participates the same way
  a new hire would, through the same front doors, not a side channel. That reframes
  what "no approval prompt" actually means: no y/n dialog *in this software*, not that
  nothing ever gates a risky change — a maintenance request can be drafted and
  submitted instantly, but whether it actually *runs* still depends on the same
  Change Advisory Board approval a human's request would need, because that gate
  lives in the change-management process, not in this client.
- **Constrain what this account can actually do, at the OS level.** No approval
  prompt means Claude can do anything your user account can — so scope that account
  the way you'd scope a laptop issued to a new employee: enough access to do the job,
  not more. This is enforced by the OS itself, independent of anything Claude decides
  to do, so it holds even against a fully compromised or badly hallucinating agent.
  - Run as a **dedicated, non-admin user account** — not your daily-driver login, never root.
  - **File/directory permissions** (`chmod`/`chown`, group membership) scope what
    that account can read, write, or execute — put anything sensitive outside its
    reach entirely, rather than trusting it won't be touched.
  - **No passwordless `sudo`** for that account; if a specific privileged command is
    genuinely needed, grant it narrowly via `sudoers`, not blanket admin rights.
  - For stricter control, **AppArmor**/**SELinux** profiles and systemd sandboxing
    directives enforce restrictions the account can't opt itself out of.
- It's your API key: one request can fan out into many tool calls (capped at 75 per turn).
- `bash` forgets everything between calls — `cd`, exports, activated venvs. Chain with `&&`,
  or use `python`, which keeps state.
- Ask for files by absolute path. If Claude offers a download link instead, tell it you
  need the file written to disk.
- Nothing under `/tmp` can be trashed (tmpfs has no trash), so deletes there are permanent
  — the tool says so rather than pretending.
- **`computer` does not work on Wayland.** It drives the screen through X11/XTEST, which
  Wayland compositors ignore by design. Check with `echo $XDG_SESSION_TYPE`; if it prints
  `wayland`, the tool refuses up front rather than clicking into the void. Fix with an Xorg
  session, or `xvfb-run` — see [Setup](#setup-linux). One exception: if the actual app you
  need to control is itself an XWayland client (common for Qt/GTK/Java desktop apps), Claude
  can still drive *that one window* directly — see [Setup](#setup-linux) for the recipe.
- If Sonnet gets inconsistent on a complicated multi-tool request, set `model` to an Opus one.
- Every per-tool package is installed unconditionally by `requirements.txt` — none of
  them are meant to be skipped. They're just *imported* lazily, only when that tool
  runs, so if one's ever missing anyway (a stale venv), it breaks just that tool and
  tells you what to install rather than crashing the whole client. If a tool reports one
  missing that `requirements.txt` already lists, your venv just predates that line (no
  lockfile, floors only) — re-run `pip install -r requirements.txt`, no restart needed.
- **`ruff check .` and `mypy .` should both pass.** Ruff adds no rules, only turns two
  off (reasons inline in `pyproject.toml`). Mypy sets one option
  (`ignore_missing_imports`, since per-tool backing packages are lazily imported).
  Neither is a dependency — install them yourself if you want them.
- **`python smoke_test.py` before you commit.** Seconds, no API key, no network. Checks
  imports, tool-registry shape, that the doc tool-count matches the code, and an MCP
  handshake. GitHub Actions runs it plus `ruff`/`mypy` on every push/PR to `main`, on
  Python 3.11 and 3.14.
- **No unit tests, and CI doesn't exercise the tools themselves** — that needs LibreOffice,
  a browser, an X11 display, and real API credits.
- **Two things a linter will flag that are deliberate.** Broad `except Exception`/
  `BaseException` is the design — every local tool must catch anything and return an error
  string instead of crashing the chat loop (`BLE001` is off project-wide for this reason).
  And cleanup paths (`shutdown`, `close`) use a blanket catch plus `print()` on purpose —
  narrowing one already caused a real bug (`zmq.ZMQError` isn't an `OSError`, so a
  narrower catch turned an ordinary Ctrl-C into a traceback).
- **Memory** writes to `./memories` by default (`CLAUDE_MEMORY_DIR` to relocate). Claude
  sees it as `/memories`; a traversal path like `/memories/../../.ssh/id_rsa` is rejected.
  Private scratchpad for Claude, not a place for your project files — persists until you
  delete it.

<a id="setup-linux"></a>

## Setup (Linux)

You need **Linux**, **Python 3.11+**, and an Anthropic **API key** — this is an API
client, so a Claude subscription won't work.

### 1) Install system packages and create a venv

```bash
sudo apt install python3 python3-venv python3-dev build-essential \
                 libreoffice pandoc python3-tk scrot libasound2-dev pulseaudio-utils

python3 -m venv ~/claude-chat-plus-more-tools
source ~/claude-chat-plus-more-tools/bin/activate
pip install -r requirements.txt
```

`libreoffice` + `pandoc` back `document_convert` — `soffice` handles docx/odt/xlsx/pptx/
html/rtf/txt/pdf, `pandoc` handles markdown (soffice has no dependable markdown import;
`md → pdf` goes through odt on the way). `libreoffice-writer`/`-calc`/`-impress` alone are
enough if you don't want the whole suite. `python3-tk` and `scrot` back `computer` — see
step 3. `libasound2-dev` backs `midi1` — see the table below for why it's a hard
requirement, unlike some of the packages near it that aren't. `pulseaudio-utils` backs
`speak`/`listen` — both shell out to it directly (`paplay`/`parecord`) with no fallback,
so unlike most per-tool packages below, a missing binary here isn't a clean "tool
declares itself unavailable" story, just a raw subprocess failure. It's genuinely already
present on most real desktop installs (pulled in by PipeWire's `pipewire-pulse`), which is
why it's easy to assume it's a given — but that assumption doesn't hold on a headless
server, WSL, or a minimal container, all realistic ways to run a CLI tool like this one,
so it's listed here explicitly rather than left to chance.

**Per-tool Python packages** (all installed unconditionally via `requirements.txt` —
none of these are meant to be skipped; each is only *imported* lazily, at the moment
its tool actually runs):

| Tool | Needs |
|---|---|
| `python` | `jupyter_client>=8.9.1`, `ipykernel>=7` — older works too, just unencrypted (see step 6) |
| `interactive_run` | `pexpect` |
| `config_edit` | `ruamel.yaml` (YAML), `tomlkit` (TOML), `jsonpath-ng` (`$…` queries); JSON needs nothing |
| `sql_query` | `duckdb` |
| `trash` | `send2trash` |
| `computer` | `pyautogui`, `pillow` — plus `python3-tk`/`scrot` from apt and an X11 display (step 3) |
| `memory` | nothing — standard library only |
| `text_embeddings` · `vision_query` | `httpx2` — already pulled in transitively by both `anthropic` and `mcp`, listed explicitly since these modules import it directly |
| `speak` | `piper-tts` — **not** `sudo apt install piper` (an unrelated GTK app); playback shells out to `paplay` (`pulseaudio-utils`, installed above) |
| `listen` | `faster-whisper`; capture shells out to `parecord` (same `pulseaudio-utils` package as above) |
| `midi1` | `mido[ports-rtmidi]` — pulls in `python-rtmidi`, a C extension. No prebuilt Linux wheel exists for every Python version, so `pip` frequently compiles it from source — and its own build script makes ALSA dev headers a **hard requirement** on Linux unless JACK's are present instead. Without `libasound2-dev` (installed above) the build fails with a `meson`/ALSA-related compiler error, not an obvious "MIDI" one |

To drop a tool entirely, remove its module from `MODULES` in `core/local_tools.py` (e.g.
if you don't want MIDI, also drop `libasound2-dev` from the apt line above and
`mido[ports-rtmidi]` from `requirements.txt`) — otherwise, install everything as
written so all 24 tools actually work.
Everything in `requirements.txt` is a `>=` floor, not a pin — if a tool ever reports a
package missing that's already listed there, your venv just predates that line; re-run
`pip install -r requirements.txt` (no restart needed).

### 2) Playwright

```bash
playwright install chromium            # the browser binary — pip installs the package, not this
sudo playwright install-deps chromium  # OS libraries (e.g. libmanette)
```

`playwright install` with no browser name fetches all three engines; this app only
launches Chromium, so the argument is worth keeping.

### 3) `computer` — extra apt packages, and X11 vs Wayland

`pip install pyautogui` succeeds on its own, so a missing-package failure here is
misleading — `computer` reports `pyautogui` as missing when it's really one of these two:

- **`python3-tk`** — `pyautogui` pulls in `mouseinfo`, which imports `tkinter` at module
  level. Without it, `import pyautogui` raises.
- **`scrot`** — `pyscreeze` needs `gnome-screenshot` (via Pillow's `ImageGrab`) or `scrot`
  for a screenshot path on X11. Either works; `scrot` is the lighter one.

`computer` also needs a real **X11** display — it synthesises input via X11/XTEST, which
Wayland compositors ignore by design, so it refuses up front on a Wayland session (check
`echo $XDG_SESSION_TYPE`) instead of clicking into the void. Options:

```bash
# 1. Log in to an "Xorg"/"X11" session at your display manager, or
# 2. Run the whole client inside a nested X server:
sudo apt install xvfb
xvfb-run -s '-screen 0 1280x800x24' python main.py
# 3. XWayland-only setup and you want to try regardless:
export CLAUDE_COMPUTER_FORCE=1
```

**Exception: an XWayland-backed target app.** A *whole-desktop* capture genuinely can't
work on Wayland — no root window to grab. But if the specific app you want to control is
itself an XWayland client (true for many GUI toolkits not yet ported to native Wayland —
Qt, GTK, Java/Swing, Unity Editor, JetBrains IDEs, and more), it still has a real X11
window, and Claude can drive *that one window* directly, bypassing `computer` entirely:

```bash
# 1. Confirm it's XWayland-backed:
xwininfo -root -tree | grep -i "<window title>"
# 2. Find its window id and raise it:
wmctrl -l
wmctrl -i -a 0x<id>
# 3. Screenshot just that window (ImageMagick):
import -window 0x<id> /tmp/shot.png
# 4. Send it genuine XTEST input (works even without xdotool):
python3 -c "
from Xlib import X, XK, display
from Xlib.ext import xtest
d = display.Display()
xtest.fake_input(d, X.KeyPress, d.keysym_to_keycode(XK.XK_Escape))
d.sync()
xtest.fake_input(d, X.KeyRelease, d.keysym_to_keycode(XK.XK_Escape))
d.sync()
"
```

`xdotool` is the usual wrapper for step 4; `python-xlib`'s `Xlib.ext.xtest.fake_input`
calls the same XTEST extension directly if it isn't installed. One gotcha: a click needs
to land on a real interactive control — not empty space — before a subsequent injected
key event reliably reaches the app's own handlers.

`computer` reports a fixed logical screen size (`CLAUDE_DISPLAY_SIZE`, default
`1280x800`) and downscales every screenshot to it, scaling coordinates back up to your
real resolution — that's what keeps clicks landing where Claude aims. Accuracy drops
below roughly `1280x720`.

### 4) Environment variables

`main.py` calls `os.getenv()` directly, so keys must be exported for the account you
launch as — put them in `~/.bashrc` (interactive shells) or `~/.bash_profile`/`~/.profile`
(login shells, e.g. SSH). Note `export`, and no spaces around `=` (`VAR = value` is a
bash syntax error):

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

If you also use Claude Code with a subscription, add this alias too (same file) so it
doesn't shadow your subscription auth with the API key:

```bash
alias claude='env -u ANTHROPIC_API_KEY claude'
```

If you're using any MCP servers with a bearer token, export their `token_env` variable
the same way (see `config.toml`'s `[mcp]` block). Open a fresh shell (or `source` the
file) afterward, and check without revealing anything:

```bash
echo "key: ${ANTHROPIC_API_KEY:+set}"
```

### 5) Run it

```bash
python main.py
```

**MCP servers ship disabled** — the `[mcp]` block in `config.toml` ships with
`enabled = false` and every server commented out, so a fresh clone runs on the 24 local
tools alone. The commented entries are worked examples of both entry shapes (Streamable
HTTP and stdio) — replace the machine-specific addresses/paths with your own before
uncommenting and setting `enabled = true`.

### 6) Kernel encryption (automatic, no action needed)

The `python` tool's ZeroMQ sockets are plaintext by default on four loopback TCP ports.
ResearchMesh instead provisions a CurveZMQ keypair so both ends talk CURVE — needs
`jupyter_client>=8.9.1` + `ipykernel>=7` (already in `requirements.txt`) and a pyzmq built
with libsodium (the wheels are). On older versions it falls back to a Unix socket, then
plaintext TCP, printing which tier and why each time. Set
`CLAUDE_KERNEL_ENCRYPTION=required` to make an unencrypted kernel a hard error instead of
a silent fallback.

### 7) Using it

Just type. **`/think <message>`** gives Claude longer to reason on hard problems;
**`/clear`** (alias **`/reset`**) drops the conversation without restarting the app;
**Ctrl-C** exits and
shuts everything down cleanly. **`/voice [on|off]`** toggles whether Claude's replies are
also spoken aloud (via `speak`, local Piper TTS); **`/listen [N]`** records `N` seconds
from your mic (default from `[listen].default_duration_seconds`), transcribes it locally
(faster-whisper), and auto-submits the transcript as your next turn — no extra Enter
needed, regardless of whether `/voice` is on. Both need `[speak]`/`[listen]` configured in
`config.toml` first (see the tools table above); without that, `/voice` toggles but has
nothing to speak, and `/listen` reports a clear `not_configured`/`disabled` message.
**`/model`** lists the models in `config.toml`'s `[claude] claude_models`, each with an
index; **`/model swap <name or index>`** swaps the model for the rest of this session
only — it never edits `config.toml`, so the next new session always starts back on the
first entry in the list. That list itself is a live-refreshed cache, not hand-typed:
roughly once a day (`model_scan_ttl_hours`, default 24) it re-scans Anthropic's actual
`/v1/models` and rewrites `claude_models` to one entry per model family, newest release
first — sonnet is always placed first when present, matching Anthropic's own documented
default recommendation. A failed scan (offline, bad key) changes nothing on disk; the
existing cached list is used as-is.

### 8) Test it

Each of these is meant to be copy/pasted as-is directly into the CLI assistant.

a) **Build your own persistent memory of this machine — do this one first, always.**
```
Before we do anything else, I want you to build yourself some persistent memory about
this machine, since /memories is the only state that survives a session reset or a
restart — everything else (the Python kernel, the browser page, the DuckDB connection)
resets every time. Figure out what Linux distro and version this actually is first
(don't assume — check `/etc/os-release`, `uname -a`, etc.), then scan this machine's
real hardware (CPU, RAM, GPU, disks) and what's actually installed: CLI tools on PATH
via `command -v`, packages via whichever package manager this distro actually uses
(`dpkg`/`apt` on Debian/Ubuntu, `rpm`/`dnf` on Fedora, `pacman` on Arch, `zypper` on
openSUSE, etc. — check which one applies here rather than guessing), plus snap/flatpak
if either is present. Then write two files: 01_environment_notes.md (hardware specs,
the distro/OS version you actually found, disk layout, and any quirks or behaviors you
run into along the way — display server, privilege model, which package manager(s) are
in play) and 01_system_tool_inventory.md (a categorized inventory of what's already
installed — GUI apps, CLI tools, dev-assistant tools, reusable scripts you find lying
around — so you reach for a real local tool instead of writing something from scratch
every time). In both files, add a short instruction near the top telling your future
self to re-scan and refresh the file's contents the next time you're asked to read them,
rather than trusting old data blindly — so this stays accurate as things change on this
machine over time.
```
NOTE: this is the single most useful prompt on this list. Do it once, and every future
session starts already knowing your machine instead of re-discovering it from scratch.

b) **List its own slash commands.**
```
List all your custom commands and their options.
```

c) **Understand why any of this is worth doing.**
```
Now that you've looked at what's installed on my machine, explain in plain terms why
it's worth installing extra local command-line tools — like ripgrep, fd, jq, ffmpeg,
ImageMagick — instead of just having you write a one-off script from scratch every
time I ask for something similar. What's actually being saved by doing this?
```

d) **Install the recommended tools, one at a time.**
```
Look at the "Recommended local tools" section further down in this project's
README.md, and install every tool listed there via apt/snap/flatpak/rustup — one at
a time. Wait for each install to fully finish and tell me whether it succeeded or
failed before starting the next one. Don't batch them together.
```

e) **Mouse/keyboard GUI control.**
```
Open a text editor (gedit, kate, or whatever opens by default), type "Hello, I am
controlling your mouse and keyboard," save it to my Desktop, then export that same
file as a PDF, also saved to my Desktop.
```
TIP: don't touch your own mouse and keyboard while it's doing this — fighting it for
control just makes it harder for the AI. Needs an X11 session — see step 3 above if
you're on Wayland.

f) **Headless, DOM-based web browsing.**
```
Go to news.ycombinator.com using DOM-based browsing — not a visible browser window —
open the #1 story on the front page, and give me a short summary of it.
```
NOTE: this is an example of it reading and surfing the web without ever opening a
visible browser window or touching your mouse/keyboard.

g) **Write a document, then convert it.**
```
Write a short one-page markdown file about the history of the QWERTY keyboard layout,
then convert it to a PDF and save both the markdown and the PDF to my Desktop.
```

h) What is the airspeed velocity of an unladen swallow?

### 9) Important

Always make prompt (a) above your literal first message in a new session — reading
`01_environment_notes.md` and `01_system_tool_inventory.md` first is what lets it
actually know your machine instead of guessing, and (per that prompt's own instructions)
triggers it to re-verify and refresh whatever's changed since the last time it looked.

**If it starts returning 400s and won't stop, run `/clear`.** Two failures persist for
the life of the process — an unanswered `tool_use` block, and a conversation past the
context window — and both make every later turn fail the same way. The error names which
one you hit. `/clear` recovers from either while keeping the browser page, the kernel,
your MCP connections, and `/memories`. You may still need to Ctrl-C and restart, so keep
requests from running the model for long unsupervised stretches — pre-building memory
files and having Claude pause for status updates while logging progress to a task memory
file helps a lot if a 400 does hit.

**Built and tested on** Ubuntu 26.04 LTS (kernel 7.0.0), Python 3.14.4, Playwright
1.61.0. `pyproject.toml` requires 3.11+ (the floor is `tomllib`, used by `main.py`); 3.14
is just what it was run on. The apt commands above assume a Debian/Ubuntu system.

## Configuration

Non-secret settings live in `config.toml`. Secrets stay in the environment — the app does
**not** read a `.env` file.

Below is a filled-in example with MCP turned on and three servers configured — a fresh
clone instead ships with `enabled = false` and every server commented out (see Setup
step 5):

```toml
[claude]
# First entry is what a new session starts on; swap mid-session with
# /model swap <name/index> (session-only, does not edit this file).
# This array is a live-refreshed cache (see core/claude.py
# refresh_claude_models), not hand-typed — shown here already populated.
claude_models = ["claude-sonnet-5", "claude-fable-5-1", "claude-opus-5", "claude-haiku-4-5-20251001"]
model_scan_ttl_hours = 24
claude_models_checked_at = "2026-01-01T00:00:00+00:00"

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
#     env        table of extra environment variables for it, if needed
servers = [
  { name = "n8n",    url = "http://192.168.2.12:5678/mcp-server/http", token_env = "N8N_MCP_TOKEN" },
  { name = "alpaca", url = "http://192.168.2.12:8000/mcp" },
  { name = "unreal", command = ["node", "$HOME/unreal-mcp/dist/bin.js"] },
]

# Commented out by default — text_embeddings errors with a clear message
# telling you to set this until you do. See config.toml's own [embeddings]
# comments for the full write-up and two worked examples (a self-hosted
# server and a paid API) — not duplicated here so this stays in sync with
# the one copy that matters.
# [embeddings]
# url = "..."
# timeout = 30
```

A server that's unreachable (http) or fails to launch (stdio) prints a warning and is
skipped — one being down doesn't stop the app. Tokens are never written in this file,
only the *name* of the variable that holds them.

`~`, `$USER`, `$HOME` and `${ANY_VAR}` expand in `command`, `url`, and the *values* of
`env` (`env`'s own keys are left alone), so the checked-in config doesn't have to name
your home directory or mount point. An undefined variable is left as written rather than
expanding to nothing, so a typo shows up as a startup warning instead of a silently wrong
path. Absolute paths beyond that are machine-specific — edit those by hand.

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Required |
| *(per server)* | Whatever each `token_env` names, e.g. `N8N_MCP_TOKEN` |
| *(embeddings server)* | Whatever `[embeddings].api_key_env` names, if your server needs auth |
| *(vision server)* | Whatever `[vision].api_key_env` names, if your server needs auth |
| `RESEARCHMESH_MCP_TOKEN` | Bearer token clients must present to `mcp_server.py --transport streamable-http`; unset = no auth |
| `CLAUDE_SHOW_USAGE=1` | Print token and prompt-cache counts per request |
| `CLAUDE_MEMORY_DIR` | Where `memory` stores `/memories` (default `./memories`) |
| `CLAUDE_DISPLAY_SIZE` | Logical screen size `computer` reports, e.g. `1280x800` |
| `CLAUDE_COMPUTER_FORCE=1` | Let `computer` try anyway on a Wayland session |
| `CLAUDE_KERNEL_ENCRYPTION` | `auto` (default) encrypts the `python` kernel's sockets with CurveZMQ and falls back if it can't; `required` fails the tool instead of running unencrypted; `off` skips it |
## MCP, in both directions

ResearchMesh is a client and a server at the same time — the two are independent, use
either, both, or neither:

```
   Claude Code  ──delegate──▶  ResearchMesh  ──▶  n8n / Unreal / Unity / …
   (any MCP client)            (server AND client)     (its own MCP servers)
        │                            │                          │
     mcp_server.py            24 local tools           [mcp] in config.toml
```

**As a client**, it connects out to MCP servers and merges their tools with its own —
that's `[mcp]` in [Configuration](#configuration) above. **As a server**, it hands
another client two tools: `delegate`, the whole agent in one call, so Claude Code can
offload what it structurally can't do itself — drive GUI apps, answer password/`[y/N]`
prompts, keep a live Python kernel between steps, surf a real DOM, and reach
ResearchMesh's own servers — and `model`, a direct list/swap of which Claude model
*this* worker uses, the same mechanism as its own `/model` command but reachable
remotely (no agent turn spent, no API call made just to check or change it).

### Add it to Claude Code

```bash
claude mcp add researchmesh --scope user \
  --env ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  -- "$HOME/tif-env/bin/python" /path/to/ResearchMesh/mcp_server.py
```

That's it — no token, no ports, nothing to start. Claude Code launches the server itself
when it needs it. Then just ask it to delegate something: *"use researchmesh to take a
screenshot and tell me what window is focused."*

Two ways it fails, both at the first call:

- **`ANTHROPIC_API_KEY` not set** — a client passes stdio servers only a small safe
  subset of the environment, so exporting it in your shell isn't enough; that's what
  `--env` above is for. The server says so at startup rather than failing cryptically.
- **Wrong python** — use the venv interpreter with the dependencies, not bare `python`.
  The client spawns this with no `PATH` of yours and no activated venv.

A `.mcp.json` ships in the repo as a working equivalent if you'd rather commit the
config than run the command.

<details>
<summary><b>Streamable HTTP</b> — for clients that connect to an already-running endpoint</summary>

stdio (above) is right whenever the client launches its own server — Claude Code, Claude
Desktop, most editors. Use HTTP instead to share one agent between several clients, or
for a client that only speaks HTTP:

```bash
python mcp_server.py --transport streamable-http --port 8765
# point the client at http://127.0.0.1:8765/mcp
```

`--host` defaults to **127.0.0.1**, reachable only from this machine. `--path`, `--port`
and `--json-response` are there too (`--json-response` returns one JSON body instead of
an SSE stream).

**Auth is the `token_env` arrangement from `config.toml`, pointed the other way.** Set
the variable and it's required; leave it unset and the endpoint is unauthenticated —
allowed by design, and announced at startup:

```bash
export RESEARCHMESH_MCP_TOKEN=<token>          # see Tokens below
python mcp_server.py --transport streamable-http --host 0.0.0.0
```

Clients send `Authorization: Bearer <token>` — exactly what a `token_env` entry
produces, so another ResearchMesh consumes this one with a plain `config.toml` line.
Same token, same variable name, set on both machines:

```toml
{ name = "desktop", url = "http://192.168.2.5:8765/mcp", token_env = "RESEARCHMESH_MCP_TOKEN" }
```

Unauthenticated *and* bound off-loopback prints a warning — at that point anyone who can
reach the port has unrestricted shell and desktop control of the machine. The token is
read from the environment, never passed as an argument, so it stays out of `ps` and
shell history. `--token-env VAR` renames the variable.

**TLS is a pair of paths, not a mode.** Without them the endpoint is plain HTTP — the
bearer token and every task/result cross the network in the clear, called out at
startup on a non-loopback bind:

```bash
python mcp_server.py --transport streamable-http --host 0.0.0.0 \
    --ssl-certfile /etc/ssl/certs/worker-fullchain.pem \
    --ssl-keyfile  /etc/ssl/private/worker.key
```

The startup line then says `https://`. Give `--ssl-certfile` the **full chain** — leaf
first, then intermediates — since a leaf-only file verifies on the box that has the
intermediate cached and fails everywhere else. Both flags must be given together
(uvicorn quietly serves plain HTTP with only one, so this refuses instead), and both
paths are checked to exist before the port opens.

The client does no app-specific certificate setup: the URL becomes `https://…` and the
underlying HTTP client verifies certificates against its runtime's normal trust
configuration. A company CA or private certificate works only if that CA is already
trusted there, or `SSL_CERT_FILE=/path/ca.pem` / `SSL_CERT_DIR=/path/to/certs` is set
for that process.

Both transports are the same server object — no separate build. Under HTTP the stdout
guard is skipped (fd 1 isn't the wire there), so the app's messages become ordinary
line-buffered service logs. Running the stdio form by hand just waits on stdin — a
healthy stdio server behaving normally.

</details>

<details>
<summary><b>Tokens</b> — generating one, and where it actually has to live</summary>

**Only needed for `--transport streamable-http`.** Under stdio there's no port and
nothing to authenticate.

Generate one with the interpreter this project already requires — no `openssl` needed:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

256 bits from the OS CSPRNG. There's deliberately no `generate_token.py` here — wrapping
one stdlib line in a file would be the same mistake as a tool wrapping a command `bash`
could already run.

The value lives in an environment variable; only its *name* goes in a file. Which file
depends on how the process starts — this is the part that catches people:

| How it starts | Where the token has to be |
|---|---|
| You, from an interactive shell | `export RESEARCHMESH_MCP_TOKEN=…` in `~/.bashrc` |
| `systemd` unit | `EnvironmentFile=` — a unit does **not** read `~/.bashrc` |
| Spawned by an MCP client | the `env` block of that server's entry in the client config |

Two things to get right:

- **Never put the literal token in a committed file.** `.mcp.json` and `config.toml` are
  both in git — use `${RESEARCHMESH_MCP_TOKEN}` and `token_env` respectively.
- **One name is normally right.** It's one token, and each end reads it from its own
  environment, so both machines can call it `RESEARCHMESH_MCP_TOKEN`. You only need a
  second name if a *single* machine both serves an endpoint and consumes someone else's
  — then one variable would have to mean two different secrets. Rename either end with
  `--token-env VAR` or `token_env = "VAR"`.

**Can you just ask ResearchMesh to set it up?** Mostly — it can generate the token,
append the export to `~/.bashrc`, write a systemd `EnvironmentFile`, and update a
consuming `config.toml`. It *cannot* set the variable in your current shell (`bash` is a
fresh subprocess per call, and a child can't alter its parent's environment anyway), so
you still need a new shell (or `source ~/.bashrc`) and a server restart. Tell it not to
write the literal token into anything in the repo.

</details>

<details>
<summary><b>HTTPS and TLS</b> — for an MCP server with a self-signed or private-CA certificate</summary>

A server URL may be `http://` or `https://`. TLS is verified by the `httpx2` client
inside `mcp_client.py` (via the `mcp` package's own dependency — confirmed live,
`mcp` requires `httpx2`, independent of whatever `anthropic` itself uses), offline —
the CA is not contacted at connect time.

**Verification goes through OpenSSL's own default trust configuration, not a bundled
`certifi` list.** `httpx2` builds its default SSL context with `truststore.SSLContext`
(confirmed live: a plain `httpx2.Client()`'s transport uses `truststore._api.SSLContext`,
not `ssl.SSLContext` directly), which on Linux defers to `ssl.get_default_verify_paths()`
— the same mechanism `SSL_CERT_FILE`/`SSL_CERT_DIR` have always fed on this platform —
and only falls back to a short list of common per-distro CA file locations
(`/etc/ssl/certs/ca-certificates.crt` on Debian/Ubuntu, etc.) if OpenSSL's own compiled-in
defaults come up empty.

A publicly-signed certificate (Let's Encrypt, DigiCert, …) works with no configuration —
the system's own CA bundle already covers it. A self-signed or internal-CA certificate
isn't in that bundle, so override the default verify paths:

```bash
export SSL_CERT_FILE=/path/to/your-ca-chain.pem   # or SSL_CERT_DIR for a hashed dir
```

Two things that catch people out:

- `SSL_CERT_FILE` **replaces** the default verify paths rather than adding to it. If the
  same process also needs public HTTPS hosts, concatenate your CA with the system bundle
  (`/etc/ssl/certs/ca-certificates.crt` on Debian/Ubuntu — check which of the paths above
  actually exists on your distro) rather than with `certifi`'s, since that system bundle
  is what's actually being overridden now:
  `cat /etc/ssl/certs/ca-certificates.crt your-ca.pem > combined-ca.pem`
- Your server (or reverse proxy) must present its **full chain** — a missing
  intermediate is the most common "the cert is valid but it still won't connect" cause,
  and the fix is on the server side; the client only needs the root.

**The OS trust store genuinely does affect this app now** — this is a real behavior
change from the SDK's pre-1.0 `httpx`-based transport, which used a bundled `certifi`
list regardless of the OS. Don't assume the old "OS trust store is irrelevant" framing
still holds if you're used to it from an earlier version of this doc.

</details>

<details>
<summary><b>Project layout and extending</b></summary>

```
main.py                          entrypoint — connects the MCP servers, wires Chat + REPL
mcp_client.py                    MCP client (stdio / SSE / Streamable HTTP)
mcp_server.py                    the other direction — serve this agent to an MCP client
.mcp.json                        example Claude Code registration for mcp_server.py
smoke_test.py                    fast wiring checks — no API key, no network
.github/workflows/ci.yml         runs ruff + smoke_test.py on push and PR
config.toml                      model + MCP server list (no secrets; committed)
pyproject.toml                   metadata, deps, and the ruff exemptions (lint config)
requirements.txt                 the same deps, for `pip install -r`
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
  bash_session.py                persistent shell — cd/env/venvs/bg jobs survive across calls
  processes.py                   pexpect — commands that prompt
  config_edit.py                 comment-preserving YAML/TOML/JSON edits
  data.py                        DuckDB queries
  files.py                       recoverable deletes
  text_embeddings.py             embeddings from your own private HTTP endpoint
  vision.py                      vision-capable image queries against your own private endpoint
  speak.py                       local text-to-speech via Piper
  listen.py                      local speech-to-text via faster-whisper
  midi1.py                       MIDI 1.0 device I/O via mido/python-rtmidi
  output.py                      shared output trimming + image results
  process_reaper.py              exit-time safety net: kills real leftover child processes
  cli.py                         prompt_toolkit REPL
```

- **Add an MCP server:** add an entry under `[mcp].servers` in `config.toml` — see
  [Configuration](#configuration) above for both entry shapes. Its tools appear to
  Claude automatically once it connects. A one-off Python stdio script can also be
  passed as an argument instead (`python main.py path/to/server.py`), no config edit
  needed.
- **Add a local tool:** write a module exposing `TOOLS`, `handles(name)`, and
  `async execute(name, tool_input)`, then add it to `MODULES` in `core/local_tools.py` —
  the only registration step. Update `SYSTEM_PROMPT` in `core/chat.py` too, since it
  describes the tool set to Claude.
- **Keep the list lean.** Tool-selection accuracy degrades past roughly 30–50 tools, so
  prefer one tool with a mode parameter over several near-duplicates, and don't wrap a
  command `bash` could already run.

Check every configured server on its own with `python mcp_client.py` — it connects to
each in turn, lists its tools, and reports failures without starting the chat.

</details>

<details>
<summary><b>Not required: MCP Inspector</b> — for debugging an MCP server</summary>

This project is **Python-first**, but the full-feature setup needs Node.js — the repo
supports Node-based MCP servers in `config.toml` (e.g. `command = ["node", ...]`), and
the browser tooling's Playwright is Node-backed in practice. So: for the full MCP +
browser workflow, install Node.js and keep it on PATH.

The [MCP Inspector](https://github.com/modelcontextprotocol/inspector) isn't required,
and is also Node-based:

```bash
npx @modelcontextprotocol/inspector@latest
```

A separate debugging aid, not the core of the project runtime.

</details>

## Recommended local tools (not required — saves tokens)

None of these are dependencies — nothing here breaks without them. They're suggested
purely so Claude reaches for a fast, purpose-built local binary via `bash` instead of
burning tokens re-implementing the same job in `python`, or reading whole files through
the file editor just to search them. Install whichever are useful to you; skip the rest.
Everything below is `apt`/`snap`/`flatpak`, or (for Rust) the official `rustup`
installer — commands as written are Debian/Ubuntu-specific. On another distro, the
tool names are the same; swap in your own package manager (`dnf`, `pacman`, `zypper`,
etc.) yourself. `apt`/`flatpak` lines include `-y` since Claude may run these itself via
`bash`, which has no terminal for either to prompt against; drop it if running by hand
and you'd rather review each one first.

```bash
# --- Search, text & structured data -----------------------------------------------
sudo apt install -y ripgrep       # rg — recursive search, instead of reading whole files to grep them
sudo apt install -y fd-find       # fd — fast, .gitignore-aware find. NOTE: the binary is `fdfind`,
                                # not `fd` (Debian name clash with an unrelated package)
sudo apt install -y bat           # cat with syntax highlighting + line numbers. NOTE: the binary is
                                # `batcat`, not `bat` (same kind of Debian name clash as fd-find)
sudo apt install -y jq            # jq — query/reshape JSON from the shell
sudo apt install -y yq            # yq, but for YAML. NOTE: Debian's `yq` is the OLD Python
                                # jq-wrapper-for-YAML (`yq '.filter' file.yaml`), NOT the popular
                                # Go-based mikefarah/yq most online docs assume (`yq e '.path' file`)
sudo apt install -y miller         # mlr — CSV/TSV/JSON reshape/filter/stats from the shell
sudo apt install -y fzf            # fuzzy finder; use `--filter` for non-interactive/scripted matching

# --- File search & disk usage ------------------------------------------------------
sudo apt install -y plocate        # modern `locate` — instant filename search across the whole disk,
                                 # from a background-updated index (run `sudo updatedb` once first)
sudo apt install -y tree           # directory-structure dumps
sudo apt install -y ncdu            # interactive, curses-based disk usage — see what's eating space
sudo snap install dust           # fast, visual `du` — not in the default apt repos, snap only
sudo apt install -y duf             # nicer `df`, disk-space-by-volume at a glance

# --- Archives & binary inspection ---------------------------------------------------
# tar/gzip already exist on every Debian/Ubuntu system (Essential: yes — no install
# possible even if you wanted to skip them), and zip/unzip/xz-utils ship as part of the
# standard Ubuntu task. Between those four, "basically every format" is already covered
# before you install anything — unlike Windows, which has no built-in CLI archiver at
# all. The one real gap:
sudo apt install -y unrar            # RAR extraction — the one common format Linux has
                                   # nothing built in for (RAR itself is proprietary)
# 7-Zip's own .7z format is the other thing genuinely missing — worth adding only if you
# actually receive .7z files, not as a general-purpose necessity:
sudo apt install -y 7zip             # NOTE: this used to be `p7zip-full` — that package no
                                   # longer exists on current Ubuntu, replaced by the
                                   # upstream-maintained `7zip` package (still gives `7z`)
sudo apt install -y hexyl            # colorized hex+ASCII dump, e.g. for raw SysEx/firmware bytes
sudo apt install -y binwalk          # scans a binary for embedded file signatures/firmware images —
                                   # the closest apt-packaged equivalent to a deep file-type identifier

# --- Git / GitHub / diffing ---------------------------------------------------------
sudo apt install -y gh              # GitHub CLI — PRs/issues/releases from the shell
sudo apt install -y git-delta       # syntax-highlighted, side-by-side git diff pager. NOTE: the plain
                                  # `delta` apt package is a DIFFERENT, unrelated 2006 tool and
                                  # installs no `delta` binary at all — `git-delta` is the one that
                                  # actually provides the `delta` command

# --- HTTP / API testing --------------------------------------------------------------
sudo apt install -y httpie          # much more readable than raw curl for poking at APIs. NOTE: the
                                  # request-sending command is `http`, not `httpie` — the bare
                                  # `httpie` command is a separate plugin-manager subcommand

# --- C / C++ / Rust toolchains --------------------------------------------------------
# gcc/g++/make (build-essential) are already installed if you followed Setup step 1 —
# nothing missing there. clang is a genuine alternative compiler worth having on top:
sudo apt install -y clang            # self-contained C/C++ compiler, alternative to gcc
sudo apt install -y cmake            # build system generator
sudo apt install -y ninja-build      # fast build backend, pairs with cmake
# Rust: use the official rustup installer, not a distro package — apt's rustc/cargo lag well
# behind upstream and can't be updated independently of the whole system:
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh

# --- System diagnostics ---------------------------------------------------------------
# strace and lsof are already on any standard Ubuntu install (both are part of the
# `ubuntu-standard` task) — nothing to add there, they're just worth knowing about:
# `strace <cmd>` traces a process's syscalls (first move for "why is this hanging"),
# `lsof` shows what has a given file/port open.
sudo apt install -y htop            # interactive process viewer, nicer than plain `top`
sudo apt install -y procs           # modern `ps` replacement, colorized/tree-aware output
sudo apt install -y hyperfine       # benchmarking — compare two commands' real run time

# --- Audio production & media metadata ------------------------------------------------
sudo apt install -y ffmpeg                    # ffmpeg/ffprobe — audio/video transcoding and inspection
sudo apt install -y sox                       # CLI audio conversion/trim/resample, complements ffmpeg
sudo apt install -y mediainfo                 # instant codec/bitrate/duration metadata
sudo apt install -y libimage-exiftool-perl    # exiftool — metadata on images/audio/PDFs/almost anything
                                            # (package name differs from the `exiftool` command it installs)

# --- Images & graphic design -----------------------------------------------------------
sudo apt install -y imagemagick     # convert/mogrify/compare — image conversion & editing from the shell
sudo apt install -y krita           # digital painting/illustration, distinct from GIMP (raster) and
                                  # Inkscape (vector)
sudo apt install -y webp            # cwebp/dwebp — encode/decode the WebP image format from the shell

# --- Video editing -----------------------------------------------------------------------
sudo apt install -y handbrake-cli   # video transcoding with sane presets, complements ffmpeg
sudo flatpak install -y flathub org.shotcut.Shotcut   # free timeline-based video editor, not
                                                     # reliably in the default apt repos
# DaVinci Resolve (the other obvious free NLE) has no apt/snap/flatpak package — Blackmagic
# only distributes it via a manual download + free account signup from their own site.

# --- Documents & writing -----------------------------------------------------------------
sudo apt install -y poppler-utils   # pdftotext/pdftoppm/pdfinfo/pdfimages — pull just the pages you
                                  # need out of a PDF as text, without going through LibreOffice
sudo apt install -y calibre         # ebook-convert (CLI) — epub/mobi/azw3/etc., more formats than
                                  # document_convert reaches
sudo apt install -y hunspell        # command-line spell-checking
```

## License

[MIT](LICENSE) — use it, fork it, ship it. No warranty; see the file for the full text.
