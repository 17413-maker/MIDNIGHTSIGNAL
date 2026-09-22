# Changelog

## 2.1.0

- **Word-by-word streaming.** The stream renderer used to buffer text until
  it saw a newline, so a whole paragraph (which has no newline until it
  ends) printed all at once. It now emits each word the instant it
  arrives — the same feel as `ollama run` — while still styling markdown
  (headings, bullets, **bold**, *italic*, `code`, fenced code blocks,
  tables) live as it streams.
- **Sound effects.** Every sound is synthesized at runtime (no audio
  files, no extra dependencies) for boot, mode/theme switches, first
  token, tool calls, confirmations, approve/cancel, errors, task
  completion, and exit. `/sound`, `/sound on|off`, `/sound vol 0-100`,
  `/sound test`, and `--no-sound` control it; the setting persists.
- **Six color themes** (`sunset`, `ember`, `cyber`, `matrix`, `ice`,
  `noir`) repaint the whole UI live. `/theme` to list, `/theme <name>` to
  pin, `/theme auto` (default) to let each mode wear its own.
- **Status line** above the prompt: mode, working directory, git branch,
  and context usage. Toggle with `/statusbar`.
- **`--doctor`**: checks Python, the `ollama` package, the Ollama server,
  both models, tool-calling support, truecolor, and audio — and prints
  the exact fix for anything missing.
- **One-shot installers** (`scripts/install.sh`, `scripts/install.ps1`)
  and launchers (`run.sh`, `run.ps1`) for macOS, Linux, and Windows.
- A start-up check now warns immediately if the model isn't built or
  Ollama isn't reachable, instead of failing on the first message.
- Banner now reads "completely unhinged · no leash · no filter" in place
  of the base model name (still shown in `/status`).

### Fixed
- The confirmation box for `write_file` / `edit_file` / `run_command`
  could get overwritten by a background spinner, making it look like the
  prompt never appeared. The spinner now always stops before the box is
  drawn.
- If you declined a tool call, the model could immediately retry the
  identical call. Declines are now remembered per turn; an exact repeat
  ends the turn instead of asking again.
- Recovery for tool calls the model prints as plain text (instead of a
  proper structured call) now uses a real JSON scanner instead of brace
  counting, so it survives braces inside string content (e.g. code in a
  `write_file` body) and handles a few more wrapper shapes.
- Removed the context-usage pie chart in favor of a compact usage line
  under each reply (`38.2 tok/s · 142 gen · 1.8k prompt · ctx ▓░ 23%`)
  and a plainer `/context` breakdown.

### Known limitation
- A reply that ends in a single unmatched `*` with nothing after it can
  render as if that character vanished, since streaming can't know it
  was never closed until the reply is already over. Cosmetic only.

## 2.0.0

Initial public rebuild: gradient truecolor UI, true token streaming with
speed/context telemetry, persistent sessions, the `/radar` project
scanner, live model + sampling controls, cinematic mode-lock transitions,
and a tool-calling agent mode (read/write/edit files, list directories,
run shell commands, git) with confirmations and a yolo toggle.
