# 🐍 CodeHydra: The BYOS Terminal Coding Agent

**Bring Your Own Subscription (BYOS).** CodeHydra is a zero-GUI, autonomous coding agent that bypasses pay-per-token API gates by scavenging local OAuth session tokens from your official developer tools.

[![Tests](https://github.com/ativin4/codehydra/actions/workflows/test.yml/badge.svg)](https://github.com/ativin4/codehydra/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## ⚡ Core Superpowers

- **Subscription Scavenging**: Automatically finds and uses tokens from `Claude Code`, `GitHub Copilot`, and `Google Cloud ADC`.
- **Multi-Agent Gateway**: Intelligent routing across Claude 3.5 Sonnet, Gemini 1.5 Pro, and Codex based on task complexity.
- **Self-Healing Loop**: If the agent makes a change that breaks your build, it reads the `stderr` and fixes it automatically.
- **Workspace-Aware**: Injects a compact AST-tree map of your entire project into the LLM context.
- **MCP Native**: Supports Model Context Protocol for connecting to external databases, documentation, and tools.

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
export PYTHONPATH=$PYTHONPATH:.
uv run python3 src/cli.py chat
```

### 4. Configure Self-Healing
Add a `.agentrc.toml` to your project root to enable build verification:
```toml
[build]
command = "pytest" # or "npm run build", "go build", etc.
```

## 🛠️ Configuration

| Command | Action |
|---------|--------|
| `/effort low` | Use faster/cheaper models (Haiku/Flash) |
| `/effort high` | Use reasoning models (Opus/Pro) |
| `/clear` | Reset conversation history |
| `/exit` | Terminate session |

## 🤝 Contributing

We welcome contributions! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for details.

## 📄 License

Distributed under the MIT License. See `LICENSE` for more information.
