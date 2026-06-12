# CodeHydra Roadmap & Execution Plan

This document outlines the phased development strategy, milestones, and verification gates for CodeHydra—a pure Python, zero-GUI terminal coding agent utilizing Bring Your Own Subscription (BYOS) credentials.

---

## 🟩 Phase 1: Core Foundation (Authentication & Routing)
**Objective:** Establish the credential bypass layer and establish a unified model request proxy that handles rate-limit fallbacks transparently.

### Milestone 1.1: The Credential Scavenger
* **Target File:** `src/auth/scavenger.py`
* **Deliverables:**
  * Implement active filesystem scanning for local plaintext credentials (`~/.claude/.credentials.json`, `~/.copilot/config.json`).
  * Implement a macOS Keychain backup wrapper via `security find-generic-password` for Claude Code tokens.
  * Integrate Google Application Default Credentials (ADC) fallback pathways.
* **Verification Gate:** Run `scavenger.py` directly; it must return valid authorization header dictionaries without relying on standard system environment variables (`.env`).

### Milestone 1.2: The Multi-Agent Gateway
* **Target File:** `src/routing/gateway.py`
* **Deliverables:**
  * Initialize a `litellm` orchestration wrapper.
  * Map effort tiers (`low`, `medium`, `high`) to underlying models (`gemini-1.5-flash`, `claude-3-5-sonnet`, `claude-3-opus`).
  * Implement `429 RateLimitError` exception handling to automatically rotate providers and headers mid-flight.
* **Verification Gate:** Mock a rate limit error on an Anthropic endpoint and confirm the router seamlessly completes the request via an active Gemini token.

---

## 🟨 Phase 2: The Agentic Engine (Context & Memory)
**Objective:** Build out the interactive command-line workspace loop and give the agent structural awareness of the code repository.

### Milestone 2.1: The Terminal REPL
* **Target File:** `src/cli.py`
* **Deliverables:**
  * Build an interactive terminal shell loop using `typer`.
  * Support basic meta-commands (`/exit`, `/model`, `/effort`, `/clear`).
  * Handle real-time token streaming direct to standard output (`stdout`).

### Milestone 2.2: The Workspace Context Scanner
* **Target File:** `src/tools/scanner.py`
* **Deliverables:**
  * Build a filesystem crawler that respects `.gitignore` rules.
  * Generate a compact Abstract Syntax Tree (AST) tree-map summarizing the layout of classes and functions across files.
  * Automatically inject this tree-map as a structural layout into the system prompt on initialization.
* **Verification Gate:** Start the interactive shell and ask the agent to describe the project structure; it must correctly identify all local files and internal modules.

---

## 🟧 Phase 3: Direct File Execution & Self-Healing
**Objective:** Transition CodeHydra from a read-only chat application to an autonomous tool capable of manipulating code and fixing its own compilation errors.

### Milestone 3.1: The Markdown File Patcher
* **Target File:** `src/tools/patcher.py`
* **Deliverables:**
  * Create a parsing engine capable of parsing standard diff syntax from text streams:
    ```text
    <<<<<<< SEARCH
    [old code]
    =======
    [new code]
    >>>>>>> REPLACE
    ```
  * Implement safe string mutation algorithms that reject and report ambiguous patches back to the agent.

### Milestone 3.2: Automated Compilation Loop & Auto-Correction
* **Target File:** `src/tools/compiler.py`
* **Deliverables:**
  * Implement a project-level configuration parser (`.agentrc.toml`) to extract custom build commands (e.g., `npm run build`, `pytest`).
  * Wrap build commands in an isolated `subprocess.run` window.
  * Intercept non-zero exit codes, extract `stderr` diagnostics

---

## 🟦 Phase 4: Claude-Code Feature Parity (Model/Effort/CLI as the only knob)
**Objective:** CodeHydra should feel identical to driving Claude Code directly — agentic editing, tool use, sessions, streaming, cost tracking — except the user can swap the underlying model, effort tier, and CLI backend per turn.

### Milestone 4.1: Agentic Passthrough + `/cli` Override
* **Target Files:** `src/routing/gateway.py`, `src/cli.py`
* **Deliverables:**
  * Let the chosen backend CLI (`claude`/`gemini`/`codex`) edit files directly in agentic mode (appropriate auto-approve/yolo flags) instead of relying solely on the regex SEARCH/REPLACE patcher.
  * Add `/cli <claude|gemini|codex|auto>` to pin the backend for the session, mirroring `/effort`.
  * Add `/model <name>` to pin an explicit model, bypassing `MODEL_MAP`.

### Milestone 4.2: Streaming Output
* **Target Files:** `src/routing/gateway.py`, `src/cli.py`
* **Deliverables:**
  * Replace blocking `subprocess.run` with `subprocess.Popen` + incremental stdout streaming for live token output (parity with Claude Code's live response rendering).

### Milestone 4.3: Session Persistence & `/resume`
* **Target Files:** `src/routing/session.py` (new), `src/cli.py`
* **Deliverables:**
  * Persist conversation history + active model/effort/cli state to `.codehydra/sessions/<id>.json`.
  * Add `/resume [id]` and `/sessions` (list) commands.

### Milestone 4.4: Usage & Cost Tracking
* **Target Files:** `src/routing/gateway.py`, `src/cli.py`
* **Deliverables:**
  * Parse token/cost info from each CLI's output where available.
  * Add `/cost` command summarizing spend-equivalent and tier/CLI breakdown for the session.

### Milestone 4.5: MCP Tooling Wired In
* **Target Files:** `src/mcp/client.py`, `src/routing/gateway.py`, `.agentrc.toml`
* **Deliverables:**
  * Load MCP servers declared in `.agentrc.toml`.
  * Expose their tools to whichever backend CLI supports external tool/MCP configs (pass-through MCP config flags).

### Milestone 4.6: Packaging & Distribution
* **Target Files:** `pyproject.toml`, `.github/workflows/`
* **Deliverables:**
  * `uv tool install codehydra` / `pipx install codehydra` entry point.
  * Release workflow that builds + publishes to PyPI on tag push.