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