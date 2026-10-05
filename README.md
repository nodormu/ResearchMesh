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
machine: a shell, a file editor, a browser it can surf with, a persistent Python
session, desktop control, and document conversion. Ask it something and it can look it up,
read the pages, run the commands, and hand you back a finished `.docx` — in one conversation.

It works in both directions: it connects out to your own MCP servers, and it can itself be
added to **Claude Code** as one, so Claude Code can hand it the jobs it can't do —
[see below](#mcp-in-both-directions).

## What it can do

**27 local tools**, plus whatever your MCP servers expose:

| Tool | For |
|---|---|
| `bash` | Shell commands as your user via `/bin/bash` by default. Stateless — fresh subprocess each call. See `[bash]` in config.toml to use a different shell instead (e.g. zsh) |
| `str_replace_based_edit_tool` | View, create, and edit files |
| `web_search` · `web_fetch` | Anthropic's server-side search and page fetch |
| `memory` | A `/memories` store that **persists across sessions** — the only state that outlives the process |
| `computer` | Screenshots plus mouse/keyboard control of your desktop, on X11 (`pyautogui`) or Wayland (xdg-desktop-portal remote control; needs `dbus-next` and `spectacle` or `grim`) — [see below](#setup-linux) |
| `desktop_window` | List windows, and focus, move, resize, full-screen, minimize or restore one, on a KDE desktop (KWin scripting; needs `dbus-next`), so keystrokes reach the right window |
| `screen_find` | Find on-screen text (`text`) or button-like blocks (`buttons: true`) by OCR, inside a `region` when one is given, and return click coordinates in `computer`'s space; reads text on coloured buttons that plain OCR misses (needs `tesseract`) |
| `browser_navigate` · `_links` · `_click` · `_fill` · `_extract` · `_back` · `_tab` | [Playwright](https://playwright.dev/) DOM browsing: renders JavaScript, follows links and new tabs, fills forms, saves downloads to `~/Downloads`. `_navigate` takes `mode` and `profile` ([see below](#browser-modes)); `_tab` lists, switches and closes tabs; `_fill` takes a `pass` vault entry (`value_secret`) without the value appearing in the conversation, or `submit` to press Enter afterwards |
| `document_convert` | LibreOffice + pandoc. Markdown → `.docx`/`.odt`/`.pdf`, or any office format to any other |
| `python` | Persistent IPython kernel — **variables survive between calls** |
| `bash_session` | Persistent shell — **cd/env/venvs/background jobs survive between calls** |
| `interactive_run` | Commands that prompt: passwords, `[y/N]`, ssh host keys, installers, REPLs |
| `config_edit` | Edit YAML/TOML/JSON **without destroying your comments** |
| `sql_query` | DuckDB straight against CSV/Parquet/JSON — no import step |
| `trash` | Recoverable deletes instead of `rm` |
| `text_embeddings` | Vector embeddings from an HTTP embedding server you configure — self-hosted or a paid API both work. See `[embeddings]` in config.toml for worked examples |
| `vision_query` | Ask a question about an image via a vision-capable chat server you configure — self-hosted or a paid API both work. See `[vision]` in config.toml for worked examples |
| `speak` · `listen` | Local text-to-speech (Piper) and speech-to-text (faster-whisper) through your own speaker and mic; no cloud audio API. Both return `not_configured` until `[speak]` and `[listen]` are set in config.toml, which also covers first-time device setup |
| `midi1` | MIDI 1.0 device discovery and I/O (`mido` for messages and files, ALSA sequencer ports) — list ports, open/close, send/poll channel and system messages, SysEx, and read/write `.mid`/`.syx` files |

Claude chooses the tools and keeps working until it has an answer.

### Browser modes

`browser_navigate` takes `mode` and `profile`:

- `headless` (default): no window. Uses installed Google Chrome if present, else the bundled Chromium.
- `headed`: a visible window on your desktop. `headed: true` is an alias.
- `virtual`: Chrome on a private hidden display (Xvfb); no window appears.
- `real`: your installed Chrome, started as a normal program and attached over CDP. It is the least detectable mode and opens a window you can click in. The client closes it on exit.
- `virtual` and `real` need Google Chrome (`google-chrome` or `google-chrome-stable` on `PATH`); `virtual` also needs `xvfb`. `headed` and `real` need `DISPLAY` or `WAYLAND_DISPLAY` and return an error without one.
- `profile` names a persistent profile (1-40 letters, digits, `-`, `_`) so cookies and logins survive restarts. Profiles live under `~/.cache/researchmesh/browser-profiles`, mode 700. Without one, the session's profile is deleted when it closes. Changing mode or profile restarts the browser.
- A report carries a `Human check:` line when a Cloudflare check appears. A first visit with no `mode` or `profile` that a check stops is reopened once in `virtual` mode; if the line still says pending, use `real` or click the check yourself.
- Downloads are saved to `~/Downloads` (`RESEARCHMESH_DOWNLOAD_DIR` overrides) under a unique name, so an existing file is never overwritten, and are listed as `Downloaded:` lines in the result.

## Good to know

- **There is no approval prompt.** Claude runs commands and file edits as your user with no y/n in between. This is built for local development; `trash` exists so deletes are recoverable.
- **Treat it as an employee, not just an unsupervised agent.** Beyond the OS-level limits below, give it its own email address, let it work with people and other AIs in Teams or Slack, and route its work through the systems everyone else uses: a CRM/CMDB such as ServiceNow or ConnectWise as the system of record, and change tickets for anything that touches production. None of that is built into the 27 local tools; [MCP, in both directions](#mcp-in-both-directions) is how to connect it to an email, Teams/Slack or CMDB MCP server, so it participates through the same front doors a new hire would. "No approval prompt" means no y/n dialog in this software, not that nothing gates a risky change: a maintenance request can be submitted instantly, but whether it runs depends on the same Change Advisory Board approval a human's request needs, because that gate lives in the change-management process, not in this client.
- **Constrain what this account can do, at the OS level.** With no approval prompt, Claude can do anything your user account can, so scope that account the way you would a laptop issued to a new employee: enough access for the job, no more. The OS enforces this independent of anything Claude decides, so it holds against a compromised or hallucinating agent.
  - Run as a dedicated, non-admin user, not your daily login and never root.
  - Use file and directory permissions (`chmod`, `chown`, group membership) to put anything sensitive outside its reach entirely.
  - No passwordless `sudo`; if one privileged command is needed, grant it narrowly in `sudoers`.
  - For stricter control, AppArmor or SELinux profiles and systemd sandboxing directives enforce restrictions the account cannot opt out of.
- It's your API key: one request can fan out into many tool calls (capped at 200 per turn).
- `bash` forgets everything between calls (`cd`, exports, activated venvs). Chain with `&&`, or use `bash_session` or `python`, which keep state.
- Ask for files by absolute path. If Claude offers a download link instead, tell it you need the file written to disk.
- Nothing under `/tmp` can be trashed (tmpfs has no trash), so deletes there are permanent; the tool says so.
- **`computer` on Wayland goes through the desktop's remote-control portal.** The desktop may ask for approval when a session starts, and a tray icon ("Remote Control" on KDE) shows while it lasts. See [Setup](#setup-linux) step 3.
- If Sonnet is inconsistent on a complicated multi-tool request, `/model swap` to an Opus model.
- `requirements.txt` installs every per-tool package, and each is imported lazily when its tool first runs. A missing one breaks only that tool and says what to install. If a tool reports a package that `requirements.txt` already lists, your venv predates that line (floors only, no lockfile); re-run `pip install -r requirements.txt`, with no restart.
- **`ruff check .` and `mypy .` should both pass.** Ruff adds no rules; `pyproject.toml` lists its exemptions, each with a reason. Mypy sets `exclude` and `ignore_missing_imports` (the per-tool packages are imported lazily). Both are in `requirements.txt`.
- **Run `python smoke_test.py` before you commit.** It takes seconds and needs no API key and no network. It checks imports, the tool-registry shape, that the documented tool count matches the code, and an MCP handshake. GitHub Actions runs it plus `ruff` and `mypy` on every push and PR to `main`, on Python 3.11 and 3.14.
- **`python test_model_compat_live.py` is separate and outside CI** (real API, about 9 requests): it checks the per-model tool-compatibility handler against Anthropic's actual error wording.
- **The other `test_*.py` scripts exercise individual tools** and run locally, not in CI, because the tools need LibreOffice, a browser, a display, MIDI ports or API credits. `test_midi1.py` checks message bytes against a recorded snapshot, the specs' worked examples, decoding and the `describe` docs; its live part sends through ALSA's built-in `Midi Through` port and reads it back, including bursts and SysEx up to 100,000 bytes. `Midi Through` comes from the `snd-seq-dummy` kernel module, normally loaded; without it the test prints `skip  live loopback` and runs the rest.
- **Two things a linter will flag are deliberate.** Broad `except Exception` and `BaseException` are the design: every local tool must catch anything and return an error string instead of crashing the chat loop (`BLE001` is off project-wide). Cleanup paths (`shutdown`, `close`) use a blanket catch plus `print()` on purpose: `zmq.ZMQError` is not an `OSError`, so a narrower catch turns an ordinary Ctrl-C into a traceback.
- **Memory** writes to `./memories` by default (`CLAUDE_MEMORY_DIR` to relocate). Claude sees it as `/memories`; a traversal path like `/memories/../../.ssh/id_rsa` is rejected.

<a id="setup-linux"></a>

## Setup (Linux)

You need **Linux**, **Python 3.11+** and an Anthropic **API key**. This is an API client, so a Claude subscription won't work.

### 1) Install system packages and create a venv

```bash
sudo apt install python3 python3-venv python3-dev build-essential \
                 libreoffice pandoc python3-tk scrot libasound2-dev pulseaudio-utils \
                 xvfb tesseract-ocr

python3 -m venv ~/claude-chat-plus-more-tools
source ~/claude-chat-plus-more-tools/bin/activate
pip install -r requirements.txt
```

`libreoffice` and `pandoc` back `document_convert`: `soffice` handles docx, odt, xlsx, pptx, html, rtf, txt and pdf, and `pandoc` handles markdown (soffice has no dependable markdown import; `md → pdf` goes through odt on the way). `libreoffice-writer`, `-calc` and `-impress` alone are enough if you don't want the whole suite. `python3-tk` and `scrot` back `computer` (step 3). `xvfb` backs the browser's `virtual` mode. `tesseract-ocr` backs `screen_find`. `libasound2-dev` backs `midi1` (see the table below). `pulseaudio-utils` backs `speak` and `listen`, which call `paplay` and `parecord` directly with no fallback, so a missing binary is a raw subprocess failure, not a tool that declares itself unavailable. Most desktops already have it (PipeWire's `pipewire-pulse` pulls it in); a headless server, WSL or a minimal container does not, so it is listed explicitly.

**Per-tool Python packages** (all installed by `requirements.txt`; each is imported lazily, when its tool first runs):

| Tool | Needs |
|---|---|
| `python` | `jupyter_client>=8.9.1`, `ipykernel>=7`; older versions work but the kernel traffic is unencrypted (step 6) |
| `interactive_run` | `pexpect` |
| `config_edit` | `ruamel.yaml` (YAML), `tomlkit` (TOML), `jsonpath-ng` (`$…` queries); JSON needs nothing |
| `sql_query` | `duckdb` |
| `trash` | `send2trash` |
| `computer` | `pyautogui`, `pillow`; plus `python3-tk` and `scrot` from apt on X11, or `dbus-next` and `spectacle` or `grim` on Wayland (step 3) |
| `desktop_window` | `dbus-next`; plus KDE Plasma (KWin scripting over D-Bus) |
| `screen_find` | `pillow`; plus `tesseract-ocr` from apt |
| `memory` | nothing, standard library only |
| `text_embeddings` · `vision_query` | `httpx2`, already pulled in by `anthropic` and `mcp`; listed because these modules import it directly |
| `speak` | `piper-tts`, **not** `sudo apt install piper` (an unrelated GTK app); playback calls `paplay` (`pulseaudio-utils`, installed above) |
| `listen` | `faster-whisper`; capture calls `parecord` (the same `pulseaudio-utils` package) |
| `midi1` | `mido[ports-rtmidi]` and `cffi`. `mido` builds messages, reads and writes `.mid`/`.syx` files, and lists ports through `python-rtmidi`, a C extension with no prebuilt Linux wheel for every Python version, so `pip` often compiles it; its build script requires ALSA dev headers on Linux unless JACK's are present, and without `libasound2-dev` the build fails with a `meson`/ALSA compiler error, not an obvious MIDI one. `cffi` opens the ports as ALSA sequencer clients through the system `libasound.so.2`; without it, opening a port returns an error while messages and files still work |

To drop a tool entirely, remove its module from `MODULES` in `core/local_tools.py` (for MIDI, also drop `libasound2-dev` from the apt line, and `mido[ports-rtmidi]` and `cffi` from `requirements.txt`); otherwise install everything as written so all 27 tools work. Everything in `requirements.txt` is a `>=` floor, not a pin: if a tool reports a package missing that is already listed, your venv predates that line; re-run `pip install -r requirements.txt` (no restart).

### 2) Playwright

```bash
playwright install chromium            # the browser binary — pip installs the package, not this
sudo playwright install-deps chromium  # OS libraries (e.g. libmanette)
```

`playwright install` with no browser name fetches all three engines; this app only launches Chromium, so the argument is worth keeping.

### 3) `computer` — extra apt packages, and X11 vs Wayland

`pip install pyautogui` succeeds on its own, so a missing-package failure here is
misleading — `computer` reports `pyautogui` as missing when it's really one of these two:

- **`python3-tk`** — `pyautogui` pulls in `mouseinfo`, which imports `tkinter` at module
  level. Without it, `import pyautogui` raises.
- **`scrot`** — `pyscreeze` needs `gnome-screenshot` (via Pillow's `ImageGrab`) or `scrot`
  for a screenshot path on X11. Either works; `scrot` is the lighter one.

`computer` works on **X11** (`pyautogui`) and on **Wayland** (`echo $XDG_SESSION_TYPE`).
On Wayland it goes through xdg-desktop-portal: the desktop may ask for approval when a
session starts, and while it lasts KDE shows a "Remote Control" tray icon whose **End**
entry stops it (the next action starts a new session). It needs `dbus-next` (in
`requirements.txt`) and `spectacle` or `grim` for screenshots. The screen is one monitor:
the leftmost one shared in the dialog, or `CLAUDE_COMPUTER_MONITOR=<index>`. Share every
monitor in the dialog. With only some shared, the screenshot scale is estimated (exact
when they span the desktop's width or height) and a warning is printed. Typing goes
through keysyms; on Plasma 6 capitals and symbols arrive as written. The pointer position is not
readable from Wayland: `cursor_position` returns an error until the pointer has moved
once. On KDE, `desktop_window` focuses the window that should receive keystrokes and
`screen_find` returns click coordinates by OCR. To use X11/XTEST on an XWayland-only
setup or inside a nested X server instead:

```bash
sudo apt install xvfb
xvfb-run -s '-screen 0 1280x800x24' python main.py   # nested X server
export CLAUDE_COMPUTER_FORCE=1                         # XWayland-only setup
```

**Alternative: an XWayland-backed target app.** If the specific app you want to control is
itself an XWayland client (true for many GUI toolkits without a native Wayland port:
Qt, GTK, Java/Swing, Unity Editor, JetBrains IDEs, and more), it has a real X11 window, and
Claude can drive *that one window* directly through X11 tools, bypassing `computer` and the
portal:

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
`enabled = false` and every server commented out, so a fresh clone runs on the 27 local
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

Just type. **`/think <message>`** gives Claude longer to reason on hard problems; **`/clear`** (alias **`/reset`**) drops the conversation without restarting the app; **Ctrl-C** exits and shuts everything down cleanly.

**`/voice [on|off]`** toggles whether Claude's replies are also spoken aloud (through `speak`, local Piper TTS). **`/listen [N]`** records `N` seconds from your mic (default `[listen].default_duration_seconds`), transcribes it locally (faster-whisper), and auto-submits the transcript as your next turn, with no extra Enter and whether or not `/voice` is on. Both need `[speak]` and `[listen]` set in `config.toml` first (see the tools table above); without that, `/voice` toggles but has nothing to speak, and `/listen` reports `not_configured` or `disabled`.

**`/model`** lists the models in `config.toml`'s `[claude] claude_models`, each with an index. **`/model swap <name or index>`** swaps the model for this session only; it never edits `config.toml`, so a new session starts on the first entry. The list is a live-refreshed cache, not hand-typed: about once a day (`model_scan_ttl_hours`, default 24) it re-scans Anthropic's `/v1/models` and rewrites `claude_models` to one entry per model family, newest first, sonnet first when present. A failed scan (offline, bad key) changes nothing on disk.

**Haiku 4.5 has no `computer` tool.** It rejects it, so the client drops the tool for Haiku after one rejected request (a `[model compat]` line is printed) and every other tool keeps working. If `computer` was used earlier in the conversation on another model, `/model swap` to Haiku fails every turn with a 400 (`toolset_name 'computer' on a tool_use block is not the family of a declared toolset entry (no toolset entry is declared)`): swap back, or `/clear`.

### 8) Test it

Each prompt below is meant to be pasted into the CLI as is.

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
NOTE: this is the most useful prompt on the list. Do it once and every later session
starts already knowing your machine.

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
TIP: don't touch your mouse or keyboard while it runs; fighting it for control makes
the task harder. On Wayland the desktop may ask for approval (step 3).

f) **Headless, DOM-based web browsing.**
```
Go to news.ycombinator.com using DOM-based browsing — not a visible browser window —
open the #1 story on the front page, and give me a short summary of it.
```
NOTE: this reads and surfs the web without opening a window or touching your mouse
and keyboard.

g) **Write a document, then convert it.**
```
Write a short one-page markdown file about the history of the QWERTY keyboard layout,
then convert it to a PDF and save both the markdown and the PDF to my Desktop.
```

h) What is the airspeed velocity of an unladen swallow?

### 9) Important

Make prompt (a) above your first message in a new session. Reading `01_environment_notes.md` and `01_system_tool_inventory.md` first is what lets it know your machine instead of guessing, and it re-verifies whatever has changed since it last looked.

**If it starts returning 400s and won't stop, run `/clear`.** Two failures last for the life of the process, an unanswered `tool_use` block and a conversation past the context window, and both make every later turn fail the same way. The error names which one you hit. `/clear` recovers from either while keeping the browser session, the kernel, your MCP connections and `/memories`. You may still need to Ctrl-C and restart, so avoid long unsupervised stretches: pre-building memory files and having Claude pause for status updates while logging progress to a task memory file helps a lot if a 400 does hit.

**Developed on** Ubuntu 26.04 LTS (kernel 7.0.0), Python 3.14.4, Playwright 1.61.0. `pyproject.toml` requires 3.11+ (the floor is `tomllib`, used by `main.py`). The apt commands above assume a Debian/Ubuntu system.

### 10) interactive_run — log in without Claude ever seeing your passwords

`interactive_run` answers a command's prompts (sudo, ssh, git, anything that asks
for a password) from a vault on your machine. The model supplies only the name of
an entry; the value is decrypted locally and never appears in the conversation.
`browser_fill` takes the same vault entries for web logins (`value_secret`). Set
up the vault once (below). After that, whenever a command needs a credential, the
agent asks you to pick from the names you saved.

**Name check.** An entry is decrypted only if you typed its name in one of your
own messages this session, so the model cannot pick one on its own. When it needs
a credential it lists the real entry names and waits for you to name one. A typed
name stays confirmed for the rest of the session and for any use. The match is on
the whole name anywhere in your message, so a passing mention ("push it to github"
with an entry named `github`) also confirms it. A task delegated to this instance
over MCP never counts as your message, so a delegating client cannot unlock an entry
on this machine.

**What is and is not protected:**

- The value goes from `pass show` to the child process over a pty and is never in
  a tool call. The transcript returned to the model has the value scrubbed, along
  with its percent, form, HTML, JSON, hex and base64 encodings. A reversed or
  otherwise transformed copy that the child prints is not caught and would reach
  Anthropic.
- sudo's password feedback (asterisks) shows the password's length in the
  transcript, not its text.
- `send_env` takes the NAME of an environment variable and is scrubbed the same
  way, without `pass`.
- `browser_fill` types a confirmed entry into whatever page is open. A malicious
  page that talks the model into filling its login form receives the real value,
  and scrubbing does not help, because the value never returns through the model.
  Name an entry only when you want it used, and watch which site the browser is on.
- Only the first line of a `pass` entry is used.
- A GPG passphrase prompt (`pinentry`) appears on your screen, not in the
  conversation. If the key is not cached and nobody answers, `pass show` times
  out after 30 s and its whole process group (`pass` and the `gpg` it started) is
  killed; unlock the key once in your own terminal first.
- `computer` has no vault option: type a password into a native window yourself.
- A one-time code (authenticator, SMS, email) is not a vault secret. Paste it in
  the chat and the agent enters it at once with `browser_fill` `submit: true`.

<details>
<summary><strong>Full <code>pass</code> vault setup, walkthrough + reference charts (click to expand)</strong></summary>

**One-time `pass` setup — install first:**
```
sudo apt install pass pinentry-curses
```

```
SETUP SEQUENCE SETTING UP A VAULT FROM SCRATCH
══════════════════════════════════════════════

Step 1: gpg --full-generate-key
  You type:   Name, Email, Passphrase
  Purpose:    Creates your encryption key (a public/private key pair)

Step 2: gpg --list-secret-keys
  You type:   Nothing — just run it
  Purpose:    Shows you the Key ID (long hex string) you'll need next

Step 3: pass init <key-id>
  You type:   The Key ID from step 2
  Purpose:    Tells pass "encrypt my whole vault using this key"

Step 4: pass insert <entry-name>
  You type:   A name you choose, then the secret value to store
  Purpose:    Encrypts and saves one password under that name

Step 5: pass show <entry-name>
  You type:   Nothing — just the entry name
  Purpose:    Decrypts and prints that password (needs your passphrase
              the first time; gpg-agent caches it for a while after)
```

**EXPLANATION FOR SETTING UP A VAULT FROM SCRATCH AND ADDING YOUR GITHUB PERSONAL ACCESS TOKEN (PAT) TO IT AS AN EXAMPLE**

Using a PAT specifically, not a password, because GitHub doesn't accept account
passwords for git/API operations at all anymore — a PAT is what actually goes in that
prompt. Generate one at github.com → Settings → Developer settings → Personal access
tokens.
```
Thing            Where it comes from              What it's actually for
─────────────────────────────────────────────────────────────────────────
Name / Email     You type it when you run         The vault never reads this
(= "User ID")    `gpg --full-generate-key`         — but YOU will. It's the
                 to create your key                only human-readable label
                                                    you'll see when running
                                                    `gpg --list-keys` later.
                                                    Pick something you'll
                                                    recognize (e.g. name:
                                                    "pass-vault"), not
                                                    garbage — you're the one
                                                    who has to remember it,
                                                    not the software.

Passphrase       You type it when you run         This passphrase allows
                 `gpg --full-generate-key`,        you to get into your
                 same command as above             vault.

Key ID           GPG generates this on its        An ID number you give to
(long hex        own, shown to you after           `pass init` one time, to
string)          you run `gpg --list-secret-       tell your (still-empty)
                 keys`                             vault which key to use.

Public key       Generated automatically           Locks up new passwords
                 alongside the key, same           you save — used the
                 command as above                  moment you run
                                                    `pass insert github`.

Private key      Generated automatically           Unlocks passwords so you
                 alongside the key, same           can read them — used the
                 command as above                  moment you run
                                                    `pass show github` (once
                                                    the passphrase has
                                                    unlocked the key itself).

─────────────────────────────────────────────────────────────────────────
Your Actual      You type it when you run          THIS is your actual
GitHub           `pass insert github` — pass       GitHub PAT — the real
Personal         then asks you for it on its       credential git sends to
Access Token     OWN separate line, AFTER you      GitHub over HTTPS. Lives
(PAT)            run that command                  INSIDE the vault,
                                                    encrypted. Retrieved
                                                    with `pass show github`.
                                                    GitHub sees THIS, never
                                                    the passphrase. NOT the
                                                    same as, and unrelated
                                                    to, the passphrase
                                                    above. NOT your GitHub
                                                    account password either
                                                    — GitHub no longer
                                                    accepts that for git/API
                                                    use at all.
```

Once set up, a tool call looks like:
```json
{"expect": "Password for", "send_secret": "github"}
```
Note: git's prompt says "Password for ..." although the PAT belongs there; the
`expect` regex has to match what git actually prints.

The model only ever sees the word `"github"`, never your real PAT.

```
BELOW IS HOW YOU BLOW THE WHOLE VAULT AWAY IF YOU WANT START OVER
═════════════════════════════════════════════════════════════════
gpgconf --kill gpg-agent
rm -rf ~/.password-store
```

Example run
═══════════

```
$ python main.py 
[mcp] disabled in config.toml — running with local tools only
> please run sudo whoami
Response:
please select the cred name I need to use:
super_secret_admin_password
> super_secret_admin_password
Response:
`sudo whoami` returned **`root`** — the `super_secret_admin_password` credential authenticated successfully.
```

The transcript the model receives shows the password as `***`.

</details>

## Configuration

Non-secret settings live in `config.toml`. Secrets stay in the environment; the app does
**not** read a `.env` file.

Below is a filled-in example with MCP turned on and three servers configured. A fresh
clone ships with `enabled = false` and every server commented out (Setup step 5):

```toml
[claude]
# First entry is what a new session starts on; swap mid-session with
# /model swap <name/index> (session-only, does not edit this file).
# This array is a cache refreshed by core/claude.py's refresh_claude_models,
# not hand-typed.
claude_models = ["claude-sonnet-5", "claude-fable-5-1", "claude-opus-5", "claude-haiku-4-5-20251001"]
model_scan_ttl_hours = 24
claude_models_checked_at = "2026-01-01T00:00:00+00:00"

[mcp]
enabled = true              # false skips every server; local tools still work

# One line per server. Every reachable one connects and its tools join the same
# list Claude sees. Two entry shapes:
#
#   Streamable HTTP (a server already running elsewhere):
#     url        the server's endpoint
#     token_env  names the environment variable holding that server's bearer
#                token; omit it if the server needs none
#
#   stdio (a local server main.py launches as a subprocess, JSON-RPC over
#   stdin/stdout, no separate process to start):
#     command    full argv as a list, e.g. ["node", "/path/to/bin.js"]
#     env        table of extra environment variables for it, if needed
servers = [
  { name = "n8n",    url = "http://192.168.2.12:5678/mcp-server/http", token_env = "N8N_MCP_TOKEN" },
  { name = "alpaca", url = "http://192.168.2.12:8000/mcp" },
  { name = "unreal", command = ["node", "$HOME/unreal-mcp/dist/bin.js"] },
]

# Commented out by default; text_embeddings returns an error naming `url` until it
# is set. config.toml's own [embeddings] comments have the full write-up and two
# worked examples (a self-hosted server and a paid API).
# [embeddings]
# url = "..."
# timeout = 30
```

A server that is unreachable (http) or fails to launch (stdio) prints a warning and is
skipped, so one being down doesn't stop the app. Tokens are never written in this file,
only the *name* of the variable that holds them.

`~`, `$USER`, `$HOME` and `${ANY_VAR}` expand in `command`, `url` and the *values* of
`env` (`env`'s own keys are left alone), so the checked-in config doesn't have to name
your home directory or mount point. An undefined variable is left as written, so a typo
shows up as a startup warning. Other absolute paths are machine-specific; edit them by hand.

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
| `CLAUDE_COMPUTER_FORCE=1` | Use X11/XTEST for `computer` on a Wayland session (XWayland-only setups, nested X servers) |
| `CLAUDE_COMPUTER_MONITOR` | Monitor index, counted left to right, for `computer` on Wayland. Default: the leftmost shared one |
| `RESEARCHMESH_DOWNLOAD_DIR` | Where browser downloads land. Default `~/Downloads` |
| `PASSWORD_STORE_DIR` | The `pass` store whose entry names `interactive_run` and `browser_fill` offer. Default `~/.password-store` |
| `CLAUDE_KERNEL_ENCRYPTION` | `auto` (default) tries CurveZMQ-encrypted TCP, then IPC, then plaintext TCP, printing why each tier fell through; `required` fails the tool instead of running unencrypted; `off` skips encryption. Covers this machine's `python` kernel only |

## MCP, in both directions

ResearchMesh is a client and a server at the same time — the two are independent, use
either, both, or neither:

```
   Claude Code  ──delegate──▶  ResearchMesh  ──▶  n8n / Unreal / Unity / …
   (any MCP client)            (server AND client)     (its own MCP servers)
        │                            │                          │
     mcp_server.py            27 local tools           [mcp] in config.toml
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

That is 256 bits from the OS CSPRNG. There is no `generate_token.py`: a file wrapping one
stdlib line adds nothing over running it.

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

A server URL may be `http://` or `https://`. TLS is verified by the `httpx2` client in
`mcp_client.py` (a dependency of the `mcp` package), offline: the CA is not contacted at
connect time.

**Verification goes through OpenSSL's default trust configuration, not a bundled `certifi`
list.** `httpx2` builds its default SSL context with `truststore.SSLContext`, which on
Linux uses `ssl.get_default_verify_paths()`, the mechanism `SSL_CERT_FILE` and
`SSL_CERT_DIR` feed, and falls back to a short list of common per-distro CA file
locations (`/etc/ssl/certs/ca-certificates.crt` on Debian and Ubuntu, etc.) if OpenSSL's
compiled-in defaults are empty. The OS trust store therefore affects this app.

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


</details>

<details>
<summary><b>Project layout and extending</b></summary>

```
main.py                          entrypoint — connects the MCP servers, wires Chat + REPL
mcp_client.py                    MCP client (stdio / SSE / Streamable HTTP)
mcp_server.py                    the other direction — serve this agent to an MCP client
.mcp.json                        example Claude Code registration for mcp_server.py
smoke_test.py                    fast wiring checks — no API key, no network
test_model_compat_live.py        live check of the model-compat handler (spends tokens, not in CI)
test_midi1.py                    midi1 tests; the live part uses ALSA's Midi Through (not in CI)
test_*.py                        behavioural tests for individual tools (local, not in CI)
test_midi1_snapshot.json         recorded message bytes test_midi1.py compares against
.github/workflows/ci.yml         runs ruff, mypy, smoke_test.py on push and PR
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
  computer.py                    screenshots + mouse/keyboard (X11 or Wayland portal)
  wayland_input.py               xdg-desktop-portal remote control + screenshots on Wayland
  dbus_loop.py                   asyncio loop thread for D-Bus connections
  desktop_window.py              list/focus/move windows on KDE (KWin scripting over D-Bus)
  screen_find.py                 OCR: find on-screen text and buttons in computer coordinates
  browser.py                     Playwright DOM surfing
  browser_session.py             browser launch modes, profiles, tabs, downloads
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
  midi1.py                       MIDI 1.0 device I/O (mido messages and files, ALSA sequencer ports)
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
<summary><b>MCP Inspector</b> — for debugging an MCP server (not needed to run the project)</summary>

Node.js is needed only for Node-based MCP servers declared in `config.toml` (e.g.
`command = ["node", ...]`) and for the Inspector; Playwright for Python bundles its own
Node driver. The [MCP Inspector](https://github.com/modelcontextprotocol/inspector) is
Node-based:

```bash
npx @modelcontextprotocol/inspector@latest
```

</details>

## Recommended local tools (not required — saves tokens)

None of these are dependencies; nothing here breaks without them. Claude works faster and cheaper with them: it reaches for a purpose-built local binary through `bash` instead of spending tokens re-implementing the job in `python`, or reading whole files through the editor to search them. Install whichever are useful and skip the rest. Everything below is `apt`, `snap` or `flatpak` (Rust uses the official `rustup` installer), and the commands are Debian/Ubuntu-specific; on another distro the tool names are the same, so use your own package manager. The `apt` and `flatpak` lines include `-y` because Claude may run them itself through `bash`, which has no terminal to prompt on; drop it if you want to review each one by hand.

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
# tar, gzip, zip, unzip and xz-utils are already on a standard Ubuntu install, which
# covers nearly every format. The gaps:
sudo apt install -y unrar            # RAR extraction (RAR is proprietary; nothing built in)
# 7-Zip's .7z format; add it only if you receive .7z files:
sudo apt install -y 7zip             # provides `7z`; current Ubuntu has `7zip`, not `p7zip-full`
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
# gcc/g++/make (build-essential) come from Setup step 1. clang is an alternative compiler:
sudo apt install -y clang            # self-contained C/C++ compiler, alternative to gcc
sudo apt install -y cmake            # build system generator
sudo apt install -y ninja-build      # fast build backend, pairs with cmake
# Rust: use the official rustup installer; apt's rustc/cargo lag well behind upstream
# and cannot be updated separately from the system:
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh

# --- System diagnostics ---------------------------------------------------------------
# strace and lsof ship with a standard Ubuntu install (`ubuntu-standard`); nothing to add.
# `strace <cmd>` traces syscalls (first move for "why is this hanging"); `lsof` shows
# what has a file or port open.
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
# DaVinci Resolve (another free NLE) has no apt/snap/flatpak package; Blackmagic
# distributes it by manual download after a free signup.

# --- Documents & writing -----------------------------------------------------------------
sudo apt install -y poppler-utils   # pdftotext/pdftoppm/pdfinfo/pdfimages — pull just the pages you
                                  # need out of a PDF as text, without going through LibreOffice
sudo apt install -y calibre         # ebook-convert (CLI) — epub/mobi/azw3/etc., more formats than
                                  # document_convert reaches
sudo apt install -y hunspell        # command-line spell-checking
```

## License

[MIT](LICENSE) — use it, fork it, ship it. No warranty; see the file for the full text.
