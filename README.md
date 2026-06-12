# 🐍 CodeHydra: The BYOS Terminal Coding Agent

**Bring Your Own Subscription (BYOS).** CodeHydra is a zero-GUI, autonomous coding agent that bypasses pay-per-token API gates by scavenging local OAuth session tokens from your official developer tools.

[![Tests](https://github.com/ativin4/codehydra/actions/workflows/test.yml/badge.svg)](https://github.com/ativin4/codehydra/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## ⚡ Core Superpowers

- **Subscription Scavenging**: Automatically finds and uses tokens from `Claude Code`, `Gemini CLI`, `GitHub Copilot`/`Codex`, and `Google Cloud ADC`.
- **Multi-CLI Gateway**: Routes each turn to `claude`, `gemini`, or `codex` based on effort tier and which subscriptions are active, with automatic fallback if one fails.
- **Agentic Passthrough**: The chosen CLI edits files directly (auto-approve/yolo mode) — no fragile diff-parsing required.
- **Live Streaming**: Responses render incrementally as the underlying CLI produces them.
- **Self-Healing Loop**: If the agent makes a change that breaks your build, it reads the `stderr` and fixes it automatically.
- **Workspace-Aware**: Injects a compact AST-tree map of your entire project into the LLM context.
- **Sessions**: Conversation history, routing state, and usage are persisted to `.codehydra/sessions/` and resumable across runs.
- **Usage Tracking**: `/cost` summarizes which CLI/model/tier handled each turn and token counts where reported.
- **MCP Native**: Declare MCP servers in `.agentrc.toml` and they're wired into whichever backend CLI is active.
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
uv sync
```

### 3. Usage
```bash
# Start the interactive REPL
uv run python3 main.py
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
| `/cli <auto\|claude\|gemini\|codex>` | Pin the backend CLI for the session |
| `/model <name\|auto>` | Pin an exact model, bypassing the routing table |
| `/login <claude\|gemini\|codex>` | Launch a backend CLI's interactive auth flow |
| `/parallel "task 1" "task 2" ...` | Run multiple prompts concurrently (each in its own CLI process), results shown as they complete |
| `/sessions` | List saved sessions |
| `/resume [id]` | Resume a session (defaults to the most recent other than current) |
| `/cost` | Show per-turn CLI/model/tier and token usage for this session |
| `/clear` | Reset conversation history |
| `/exit` | Terminate session |

## ⚙️ Configuration (`.agentrc.toml`)

Beyond the `[build]` command, `.agentrc.toml` supports:

- **MCP servers** (`[mcp.servers.<name>]`): declared servers are passed to whichever CLI is active via its native MCP config (claude `--mcp-config`, gemini `.gemini/settings.json`, codex `-c mcp_servers.*`).
- **Routing overrides** (`[routing]`): set a custom CLI try-order (`priority`), override the default model per CLI/tier (`[routing.models.<cli>]`), or override the auto-mode model try-order (`[routing.model_map]`).

See the commented examples in `.agentrc.toml`.

## 🤝 Contributing

We welcome contributions! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for details.

## 📄 License

Distributed under the MIT License. See `LICENSE` for more information.
