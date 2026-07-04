# CodeHydra

**One TUI. Every coding CLI you subscribe to.**

> Session expired mid-task. Again. You're on Claude Pro, Codex, and Agy — and none of them last the whole day. You manually copy your context, paste it into the next one, and wait hours for the reset. CodeHydra fixes this.

<!-- demo gif goes here -->
<!-- ![CodeHydra demo](assets/demo.gif) -->

[![Tests](https://github.com/ativin4/codehydra/actions/workflows/test.yml/badge.svg)](https://github.com/ativin4/codehydra/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

---

## The Problem

If you use Claude Code, Agy, or Codex with a **subscription** (not API keys), you hit:

- Session limits that expire mid-task
- 2–4 hour waits for reset windows
- Manual context copy-paste between CLIs
- Losing your train of thought every time

## What CodeHydra Does

Routes each prompt to whichever CLI is currently active. When one hits its session limit, it automatically falls back to the next — **without interrupting your workflow**.

```
Your prompt → [Claude] rate limited? → [Agy] rate limited? → [Codex] → [Ollama]
```

- **Auto-fallback** — session expires silently, work continues
- **Persistent sessions** — resume exactly where you left off (`codehydra --resume`)
- **Streaming responses** — token-by-token output, real-time
- **Parallel tasks** — `/parallel "task 1" "task 2"` runs multiple prompts concurrently
- **MCP native** — declare MCP servers in `.agentrc.toml`, they're wired in automatically
- **Effort tiers** — route low/medium/high effort prompts to appropriate models
- **Ollama fallback** — local or remote OSS models as last-resort backup

---

## Install

**Requirements:** Python 3.11+, [pipx](https://pipx.pypa.io), at least one of: `claude`, `agy`, or `codex` on PATH.

```bash
# From GitHub (no clone needed)
pipx install git+https://github.com/ativin4/codehydra.git

# Or clone and install in editable mode (code changes take effect immediately)
git clone https://github.com/ativin4/codehydra.git
cd codehydra
pipx install --editable .
```

> **No pipx?** `brew install pipx` (macOS) or `pip install pipx`

## Usage

```bash
codehydra              # start fresh session
codehydra --resume     # pick a previous session interactively
codehydra -r <id>      # resume a specific session by ID
```

---

## Commands

| Command | What it does |
|---|---|
| `/effort <low\|medium\|high>` | Set effort tier — affects which model gets used |
| `/cli <auto\|claude\|agy\|codex\|ollama>` | Pin a specific CLI for this session |
| `/model <name\|auto>` | Override the model directly |
| `/mode <plan\|yolo>` | `plan` = read-only; `yolo` = auto-approve edits (default) |
| `/parallel "task 1" "task 2"` | Run multiple prompts concurrently |
| `/resume [id]` | Resume a saved session in-TUI |
| `/sessions` | List all saved sessions |
| `/status` | Show CLI health, auth state, MCP status |
| `/cost` | Per-turn breakdown of CLIs, models, and token usage |
| `/clear` | Reset conversation history |
| `/login <claude\|agy\|codex\|ollama>` | Trigger auth flow for a CLI |
| Ctrl+C | Cancel active request |
| Ctrl+Y | Copy last response to clipboard |
| Ctrl+Enter / Shift+Enter | Insert newline in prompt |

---

## Configuration (`.agentrc.toml`)

Drop this in your project root:

```toml
[build]
command = "pytest"   # auto-runs after edits; feeds errors back to the agent

[mcp.servers.chrome]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-puppeteer"]

[routing]
priority = ["claude", "agy", "codex", "ollama"]

[routing.models.claude]
high   = "claude-sonnet-4-5"
medium = "claude-haiku-4-5"
low    = "claude-haiku-4-5"
```

---

## Ollama (OSS model fallback)

```bash
ollama pull llama3.2          # or qwen2.5-coder, deepseek-r1, etc.
# In CodeHydra:
/cli ollama
/model llama3.2

# Remote instance:
export OLLAMA_HOST=https://your-ollama-host
```

---

## Current State

This is an early-stage project. The core loop (routing, fallback, sessions, streaming) works. Known rough edges:

- Agy fallback sometimes needs a second prompt to kick in
- Extended thinking output can be slow to appear
- `/login agy` currently suspends the TUI instead of overlaying

**This is where you come in.**

---

## Contributing

The codebase is small and readable. Main files:

| File | What's in it |
|---|---|
| `codehydra/tui.py` | Textual TUI app, all UI and command handling |
| `codehydra/routing/gateway.py` | CLI routing, fallback logic, streaming |
| `codehydra/routing/claude_session.py` | Persistent `claude` stream-json session |
| `codehydra/routing/session.py` | Session persistence |
| `codehydra/cli.py` | Entry point, `--resume` flag, session picker |

**Good first issues:**
- [ ] `/login agy` should overlay the auth UI, not close the TUI
- [ ] `/parallel` results panel (show workers side-by-side)
- [ ] Better rate-limit detection for codex banners
- [ ] Windows support (currently macOS/Linux only)
- [ ] Web UI mode via textual-serve improvements

```bash
git clone https://github.com/ativin4/codehydra.git
cd codehydra
pip install -e ".[dev]"
pytest tests/
```

Open an issue, drop a comment, or just try it and tell me where it breaks. All feedback welcome.

---

## License

MIT — see [LICENSE](LICENSE).
