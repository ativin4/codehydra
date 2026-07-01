# 🐍 CodeHydra: The BYOS Terminal Coding Agent

**Bring Your Own Subscription (BYOS).** CodeHydra is a zero-GUI, autonomous coding agent that bypasses pay-per-token API gates by scavenging local OAuth session tokens from your official developer tools.

[![Tests](https://github.com/ativin4/codehydra/actions/workflows/test.yml/badge.svg)](https://github.com/ativin4/codehydra/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## ⚡ Core Superpowers

- **Subscription Scavenging**: Automatically finds and uses tokens from `Claude Code`, `Agy CLI`, `GitHub Copilot`/`Codex`, and `Google Cloud ADC`.
- **OSS Model Fallback**: Falls back to [Ollama](https://ollama.com) (local or cloud) during rate-limits or refresh windows — set `OLLAMA_HOST` for a remote instance; no subscription gap means no conversation interruption.
- **Multi-CLI Gateway**: Routes each turn to `claude`, `agy`, `codex`, or `ollama` based on effort tier and which subscriptions/daemons are active, with automatic fallback if one fails.
- **Agentic Passthrough**: The chosen CLI edits files directly (auto-approve/yolo mode) — no fragile diff-parsing required.
- **Live Streaming**: Responses render incrementally, token-by-token, as `claude`/`agy` produce them (via `stream-json`).
- **Persistent Claude Session**: `claude` runs as a long-lived `stream-json` process, reused across turns - only the first turn pays CLI startup cost, and `/mode`/`/model` changes or a new conversation transparently restart it.
- **Claude Code-style TUI**: A scrollable history pane with a pinned input box at the bottom, built with `textual`.
- **Self-Healing Loop**: If the agent makes a change that breaks your build, it reads the `stderr` and fixes it automatically.
- **Workspace-Aware**: Injects a compact AST-tree map of your entire project into the LLM context.
- **Sessions**: Conversation history, routing state, and usage are persisted to `.codehydra/sessions/` and resumable across runs.
- **Usage Tracking**: `/cost` summarizes which CLI/model/tier handled each turn and token counts where reported.
- **MCP Native**: Declare MCP servers in `.agentrc.toml` and they're wired into whichever backend CLI is active.
- **Autonomous Subagents**: Every backend CLI is given a built-in `dispatch_agents` tool (mirroring Claude Code's Task tool) so it can fan independent sub-tasks out to parallel CodeHydra-routed agents on its own, mid-turn.
- **Background Tasks**: The same built-in toolset gives the CLI `run_in_background`/`get_background_output`/`stop_background_task` (mirroring Claude Code's background Bash + Monitor) for long-lived processes like dev servers, surviving past the current turn.
- **Configurable Routing**: Override CLI priority and model choices per tier in `.agentrc.toml` without touching code.

## 🚀 Quick Start

### 1. Prerequisites
Ensure you have [uv](https://github.com/astral-sh/uv) installed:
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2. Installation
```bash
git clone https://github.com/ativin4/codehydra.git
cd codehydra
pipx install --editable .   # installs `codehydra` on PATH (same dir as claude / agy)
```

> **No pipx?** `brew install pipx` (macOS) or `pip install pipx`. Editable install means code changes take effect immediately — no reinstall needed.

### 3. Usage
```bash
codehydra                   # fresh session
codehydra --resume          # resume most recent session
codehydra --resume <id>     # resume a specific session by ID
```

### 4. Configure Self-Healing
Add a `.agentrc.toml` to your project root to enable build verification:
```toml
[build]
command = "pytest" # or "npm run build", "go build", etc.
```

## 🛠️ Commands

| Command | Action |
|---------|--------|
| `/effort <low\|medium\|high>` | Set the effort tier (affects model choice) |
| `/cli <auto\|claude\|agy\|codex\|ollama>` | Pin the backend CLI for the session |
| `/model <name\|auto>` | Pin an exact model, bypassing the routing table |
| `/mode <plan\|yolo>` | `plan` = read-only (no edits/commands); `yolo` = auto-approve everything (default) |
| `/login <claude\|agy\|codex\|ollama>` | Launch auth flow (or show setup/model info for Ollama) |
| `/parallel "task 1" "task 2" ...` | Run multiple prompts concurrently (each in its own CLI process), results shown as they complete |
| `/sessions` | List saved sessions |
| `/resume [id]` | Resume a session in-TUI (defaults to most recent); also available as `codehydra --resume [id]` at launch |
| `/cost` | Show per-turn CLI/model/tier and token usage for this session |
| `/clear` | Reset conversation history |
| `/exit` | Terminate session |

## 🤖 OSS Model Fallback (Ollama)

Use Ollama as a fallback when your paid subscriptions hit rate limits:

**Local**
```bash
# Install: https://ollama.com/download
ollama pull llama3.2        # or mistral, qwen2.5-coder, deepseek-r1, etc.
# Then in CodeHydra:
/cli ollama                 # pin to ollama for the session
/model llama3.2             # pick any pulled model
/login ollama               # shows available models + usage hint
```

**Cloud / remote Ollama instance**
```bash
export OLLAMA_HOST=https://your-ollama-host
# CodeHydra detects it automatically on next start (or /login ollama to refresh)
```

Ollama is detected on startup and added as the last-tier fallback, activating automatically when claude/agy/codex all fail.

## ⚙️ Configuration (`.agentrc.toml`)

Beyond the `[build]` command, `.agentrc.toml` supports:

- **MCP servers** (`[mcp.servers.<name>]`): declared servers are passed to whichever CLI is active via its native MCP config (claude `--mcp-config`, agy `.agy/settings.json`, codex `-c mcp_servers.*`).
- **Routing overrides** (`[routing]`): set a custom CLI try-order (`priority`), override the default model per CLI/tier (`[routing.models.<cli>]`), or override the auto-mode model try-order (`[routing.model_map]`).

See the commented examples in `.agentrc.toml`.

## 🤝 Contributing

We welcome contributions! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for details.

## 📄 License

Distributed under the MIT License. See `LICENSE` for more information.
