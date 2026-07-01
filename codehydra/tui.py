import re
import shlex
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import TextArea, Markdown, Static

from codehydra.routing.claude_session import THINKING_END, THINKING_START
from codehydra.routing.constants import CLI, Mode, Role, Tier
from codehydra.routing.gateway import Gateway
from codehydra.routing.oss_provider import OllamaProvider
from codehydra.routing.session import SessionManager
from codehydra.tools.compiler import Compiler
from codehydra.tools.media import MEDIA_EXTS, image_to_base64, pdf_to_text
from codehydra.tools.patcher import Patcher
from codehydra.tools.scanner import Scanner
from codehydra.tools.websearch import fetch_url, format_results, search


SKILLS_DIR = Path(".codehydra/skills")
MEMORY_FILE = Path(".codehydra/memory.md")

# Human-readable display names for CLI binaries.
_CLI_DISPLAY = {
    CLI.CLAUDE: "claude",
    CLI.AGY:    "agy",
    CLI.CODEX:  "codex",
    CLI.OLLAMA: "ollama",
}

# Accept friendly aliases for /login (e.g. /login gemini → /login agy).
_LOGIN_ALIASES: dict[str, str] = {
    "gemini": CLI.AGY,
    "google": CLI.AGY,
}

# Regex to detect meta-questions about conversation history so we can
# answer them locally rather than routing to a CLI that only sees its
# own subprocess invocation and has no awareness of prior turns.
_HISTORY_META_RE = re.compile(
    r"\bwhat\s+(was|is|were)\s+(my|the)\s+(last|previous|prior)\s+(message|prompt|question|input)\b|"
    r"\bwhat\s+did\s+i\s+(just\s+)?(say|ask|type|write)\b|"
    r"\b(repeat|show)\s+(my\s+)?(last|previous)\s+(message|prompt)\b|"
    r"\bwhat\s+was\s+my\s+(last|previous)\b",
    re.IGNORECASE,
)

SLASH_COMMANDS = [
    f"/effort {Tier.LOW}", f"/effort {Tier.MEDIUM}", f"/effort {Tier.HIGH}",
    f"/cli auto", f"/cli {CLI.CLAUDE}", f"/cli {CLI.AGY}", f"/cli {CLI.CODEX}", f"/cli {CLI.OLLAMA}",
    "/model auto",
    f"/mode {Mode.PLAN}", f"/mode {Mode.YOLO}",
    f"/login {CLI.CLAUDE}", f"/login {CLI.AGY}", f"/login {CLI.CODEX}", f"/login {CLI.OLLAMA}",
    "/parallel",
    "/compare",
    "/skills",
    "/sessions", "/resume",
    "/cost", "/clear", "/compact", "/help", "/exit",
    "/memory", "/remember", "/forget",
    "/search",
    "/doctor",
    "/commit",
    "/pr",
    "/tasks",
    "/sdd",
]

HELP_TEXT = """\
## CodeHydra Commands

### Routing
- `/effort low|medium|high` — set effort tier (model quality)
- `/cli auto|claude|agy|codex|ollama` — pin backend CLI
- `/model <name|auto>` — pin exact model name
- `/mode plan|yolo` — `plan`=read-only, `yolo`=auto-approve edits

### Auth
- `/login <cli>` — launch auth flow (or setup info for ollama)
- `/doctor` — check CLI auth, MCP server, and project health

### Context
- `/compact` — summarise old history, keep 2 recent turns
- `/memory` — show project memory
- `/remember <fact>` — save a fact (persists across sessions)
- `/forget <pattern>` — remove memory lines matching pattern
- `/search <query>` — DuckDuckGo search, results injected as context

### Inline expansions (in any prompt)
- `@path/to/file` — inject file content
- `@https://url` — fetch URL and inject content
- `@image.png` — attach image (agy: native multimodal; ollama: vision)
- `@doc.pdf` — attach PDF (agy: native; others: pdftotext)

### Skills
- `/skills` — list skills in `.codehydra/skills/`
- `/skill <name> [prompt]` — run a skill

### Multi-CLI
- `/compare <prompt>` — same prompt to all active CLIs, compare results
- `/parallel "t1" "t2"` — run tasks concurrently, each in its own CLI

### Session
- `/sessions` — list saved sessions
- `/resume [id]` — resume a session (defaults to most recent)
- `/cost` — per-turn CLI/model/tier and token usage
- `/clear` — reset conversation history

### Spec-Driven Development
- `/sdd <task>` — phase 1: spec via high-tier agent; phase 2: parallel implementation
- `/loop [N] <prompt>` — repeat prompt N times (omit N = infinite; Ctrl+C stops)

### Background Tasks
- `/tasks` — list background tasks spawned by agents via MCP

### Git
- `/commit [msg]` — stage all + AI commit message (or pass your own)
- `/pr [title]` — generate PR description, push branch, create PR via `gh`

### Input
- `Tab` — autocomplete `@path` file reference
- `Ctrl+Enter` / `Shift+Enter` — insert newline (multi-line prompt)
- `Ctrl+E` — open prompt in `$EDITOR`
- `↑` / `↓` — navigate prompt history
- `Ctrl+C` — cancel active request / clear input / quit
"""


def _skill_names() -> list[str]:
    if not SKILLS_DIR.is_dir():
        return []
    return [p.stem for p in SKILLS_DIR.glob("*.md")]


class PromptTextArea(TextArea):
    """Prompt input where Enter submits and Ctrl+Enter / Shift+Enter adds a newline."""

    def on_key(self, event) -> None:
        if event.key == "up":
            row, _ = self.cursor_location
            if row == 0:
                event.prevent_default()
                event.stop()
                self.app.action_history_up()
        elif event.key == "down":
            row, _ = self.cursor_location
            last_row = self.text.count("\n")
            if row == last_row:
                event.prevent_default()
                event.stop()
                self.app.action_history_down()
        elif event.key == "enter":
            event.prevent_default()
            event.stop()
            self.app.action_submit_input()
        elif event.key in ("ctrl+enter", "shift+enter"):
            event.prevent_default()
            event.stop()
            self.insert("\n")
        elif event.key == "tab":
            event.prevent_default()
            event.stop()
            self.app._complete_at_ref()
        elif event.key == "ctrl+e":
            event.prevent_default()
            event.stop()
            self.app._open_in_editor()
        elif event.key == "ctrl+c":
            # Textual runs the tty in raw mode (ISIG off), so Ctrl+C arrives here
            # as a key event, not an OS SIGINT — this is the one live path.
            event.prevent_default()
            event.stop()
            if self.app._request_active and not self.app._cancel_event.is_set():
                # Active request, not already cancelling: cancel it.
                self.app._cancel_request()
            elif self.text:
                self.text = ""
            else:
                self.app._ctrl_c_idle()


class HydraApp(App):
    """Claude Code-style TUI: scrollable history with a pinned input box."""

    CSS = """
    #history {
        height: 1fr;
        padding: 0 2;
    }
    #status {
        height: 1;
        background: $boost;
        color: $text-muted;
        padding: 0 2;
    }
    #input-area {
        height: auto;
        max-height: 8;
        padding: 0 1;
        border: solid $accent;
        margin: 0 1;
    }
    .user-msg {
        color: $accent;
        margin-top: 1;
    }
    .thinking-active {
        color: $text-muted;
        text-style: italic;
        padding-left: 2;
        border-left: solid $accent;
        margin-top: 1;
        margin-bottom: 0;
    }
    .thinking-done {
        color: $text-muted;
        padding-left: 2;
        margin-bottom: 0;
    }
    .response-turn {
        margin-top: 0;
        margin-bottom: 1;
    }
    """

    BINDINGS = []

    def __init__(self, resume: str | None = None):
        super().__init__()
        self.gateway = Gateway()
        self.scanner = Scanner()
        self.patcher = Patcher()
        self.compiler = Compiler()
        self.sessions = SessionManager()

        self.system_msg = self._build_system_msg_base()
        self.history = [{"role": Role.SYSTEM, "content": self.system_msg}]
        self._refresh_system_msg()
        self.effort_tier = None
        self.cli_override = None
        self.model_override = None
        self.mode = Mode.YOLO
        self.usage_log = []
        self.session_id = self.sessions.new_session_id()
        self._resume_target = resume  # resolved in on_mount after widgets exist
        self._prompt_history = []
        self._history_index = -1
        # Tracks whether the main prompt worker is running.
        self._request_active = False
        self._cancel_event = threading.Event()
        self._loop_stop = threading.Event()
        # Counter for background workers (parallel/sdd/compare/commit/search…).
        # Distinct from _request_active so both can be true simultaneously.
        self._active_workers = 0
        # Armed by the first Ctrl+C while workers run; a second one force-quits.
        self._quit_armed = False

    def on_unmount(self) -> None:
        self._save_session()
        self.gateway.close(stop_mcp_server=True)

    def _cancel_request(self) -> None:
        if self._cancel_event.is_set():
            return
        self._cancel_event.set()
        self._loop_stop.set()
        self._add_message(Text("⏹ Cancelling…", style="dim yellow"))

    def _ctrl_c_idle(self) -> None:
        """Ctrl+C with no active request and an empty input box: quit, but guard
        against killing background workers with an accidental single press."""
        if self._active_workers > 0:
            if self._quit_armed:
                self.exit()
            else:
                self._quit_armed = True
                n = self._active_workers
                label = "worker" if n == 1 else "workers"
                self._add_message(Text(
                    f"⚠ {n} background {label} still running — press Ctrl+C again to force quit.",
                    style="yellow",
                ))
        else:
            self.exit()

    def _inc_workers(self) -> None:
        self._active_workers = getattr(self, "_active_workers", 0) + 1
        self._update_status()

    def _dec_workers(self) -> None:
        self._active_workers = max(0, getattr(self, "_active_workers", 1) - 1)
        if self._active_workers == 0:
            self._quit_armed = False
        self._update_status()

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="history")
        yield Static(id="status")
        text_area = PromptTextArea(id="input-area", language="markdown")
        text_area.text = ""
        yield text_area

    def on_mount(self) -> None:
        self._history_scroll = self.query_one("#history", VerticalScroll)
        auth_status = self.gateway.cli_auth_status
        ready = [cli for cli, ok in auth_status.items() if ok]
        unauthenticated = [cli for cli, ok in auth_status.items() if not ok and cli in Gateway.LOGIN_COMMANDS]

        ready_names = [_CLI_DISPLAY.get(c, c) for c in ready]
        header = Text("CodeHydra", style="bold green")
        if ready_names:
            sub = Text(f"  {', '.join(ready_names)} ready", style="dim")
            self._add_message(Group(header, sub))
        else:
            self._add_message(header)

        if unauthenticated:
            lines = ["**Not authenticated** — run the command to log in:\n"]
            for cli in unauthenticated:
                display = _CLI_DISPLAY.get(cli, cli)
                lines.append(f"- **{display}**: `/login {display}`\n")
            self._add_message(Markdown("".join(lines)))
        elif not ready:
            self._add_message(Markdown(
                "No CLIs active. Run `/login claude`, `/login agy`, or `/login codex`."
            ))

        self._update_status()
        self.query_one("#input-area", TextArea).focus()

        if self._resume_target is not None:
            target = self._resume_target or self.sessions.latest_session_id(exclude=self.session_id)
            data = self.sessions.load(target) if target else None
            if not data:
                self._add_message(Text(f"No session found: {target or '(none saved)'}", style="red"))
            else:
                self.session_id = target
                self.history = data.get("history", self.history)
                self.effort_tier = data.get("effort_tier")
                self.cli_override = data.get("cli_override")
                self.model_override = data.get("model_override")
                self.mode = data.get("mode", Mode.YOLO)
                self.usage_log = data.get("usage_log", [])
                self.system_msg = self._build_system_msg_base()
                self._refresh_system_msg()
                self._render_history()
                self._add_message(Text(f"Resumed session {self.session_id}", style="yellow"))
                self._update_status()

    # -- helpers -----------------------------------------------------

    def _update_status(self) -> None:
        parts: list[str] = []
        if getattr(self, "_request_active", False):
            parts.append("⏳ thinking… (Ctrl+C to cancel)")
        _aw = getattr(self, "_active_workers", 0)
        if _aw > 0:
            label = "agent" if _aw == 1 else "agents"
            parts.append(f"⚙ {_aw} {label} working…")
        if getattr(self, "cli_override", None):
            parts.append(f"cli:{_CLI_DISPLAY.get(self.cli_override, self.cli_override)}")
        if getattr(self, "model_override", None):
            parts.append(f"model:{self.model_override}")
        if getattr(self, "effort_tier", None):
            parts.append(f"effort:{self.effort_tier}")
        if getattr(self, "mode", Mode.YOLO) != Mode.YOLO:
            parts.append(f"mode:{self.mode}")
        if getattr(self, "usage_log", None):
            last = self.usage_log[-1]
            display_cli = _CLI_DISPLAY.get(last["cli"], last["cli"])
            short_model = last["model"].split("/")[-1]
            tier_tag = f"/{last['tier']}" if last.get("tier") else ""
            parts.append(f"via {display_cli}/{short_model}{tier_tag}")
        rl_parts = []
        for name in CLI:
            secs = self.gateway.rate_limit_resets_in(name)
            if secs is not None:
                rl_parts.append(f"{_CLI_DISPLAY.get(name, name)}⏳{secs}s")
        if rl_parts:
            parts.append(f"rate-limited: {' '.join(rl_parts)}")
        if getattr(self, "session_id", None):
            parts.append(f"session:{self.session_id}")
        status = "  ".join(parts) if parts else ""
        try:
            self.query_one("#status", Static).update(status)
        except Exception:
            pass

    def _build_system_msg_base(self) -> str:
        context = self.scanner.get_system_prompt_context()
        return (
            "You are CodeHydra, an autonomous coding agent. "
            "To modify files, use the following format:\n"
            "File: path/to/file\n"
            "<<<<<<< SEARCH\n"
            "[exact code to find]\n"
            "=======\n"
            "[new code to replace with]\n"
            ">>>>>>> REPLACE\n"
            f"{context}"
        )

    def _refresh_system_msg(self) -> None:
        """Rebuild history[0] with current memory file content prepended."""
        try:
            memory = MEMORY_FILE.read_text().strip() if MEMORY_FILE.exists() else ""
        except Exception:
            memory = ""
        if memory:
            full = f"## Project Memory\n{memory}\n\n---\n\n{self.system_msg}"
        else:
            full = self.system_msg
        self.history[0] = {"role": Role.SYSTEM, "content": full}

    def _add_message(self, renderable) -> Widget:
        widget = renderable if isinstance(renderable, Widget) else Static(renderable)
        history = self._history_scroll
        history.mount(widget)
        history.scroll_end(animate=False)
        return widget

    def _add_response_turn(self):
        """Mount a Markdown widget for streaming response. Returns it.
        Thinking widget is NOT mounted here; it's lazily inserted above this
        widget only if the model actually produces thinking output."""
        history = self._history_scroll
        md = Markdown("", classes="response-turn")
        history.mount(md)
        history.scroll_end(animate=False)
        return md

    def _mount_thinking_widget(self, before: "Widget") -> "Static":
        """Lazily insert a thinking indicator above `before`. Called from worker."""
        w = Static("⟳ Thinking…", classes="thinking-active")
        self._history_scroll.mount(w, before=before)
        self._history_scroll.scroll_end(animate=False)
        return w

    def _render_history(self) -> None:
        history = self._history_scroll
        history.remove_children()
        for msg in self.history:
            if msg["role"] == Role.USER:
                history.mount(Static(f"  {msg['content']}", classes="user-msg"))
            elif msg["role"] == Role.ASSISTANT:
                history.mount(Markdown(msg["content"], classes="response-turn"))
        history.scroll_end(animate=False)

    def _save_session(self) -> None:
        self.sessions.save(self.session_id, {
            "history": self.history,
            "effort_tier": self.effort_tier,
            "cli_override": self.cli_override,
            "model_override": self.model_override,
            "mode": self.mode,
            "usage_log": self.usage_log,
        })

    def _login(self, cli_name: str) -> None:
        if cli_name == CLI.OLLAMA:
            self.gateway.refresh_auth()
            available = OllamaProvider.list_models()
            if available:
                lines = [
                    Text(f"Ollama ready. Models: {', '.join(available)}", style="green"),
                    Text(f"Use /cli ollama  /model {available[0]}  to start chatting.", style="dim"),
                ]
            else:
                lines = [
                    Text("Ollama not reachable.", style="yellow"),
                    Text("  Local:  install from https://ollama.com/download, then ollama pull llama3.2", style="dim"),
                    Text("  Cloud:  export OLLAMA_HOST=https://your-ollama-host", style="dim"),
                    Text("  Run /login ollama again after setup.", style="dim"),
                ]
            self._add_message(Group(*lines))
            return
        cmd = Gateway.LOGIN_COMMANDS.get(cli_name)
        if not cmd:
            self._add_message(Text(f"Unknown CLI: {cli_name}. Choose from: {', '.join(Gateway.LOGIN_COMMANDS)}, ollama", style="red"))
            return

        self._add_message(Text(f"Launching '{' '.join(cmd)}'... complete the login, then return here.", style="yellow"))
        with self.suspend():
            try:
                subprocess.run(cmd, check=False)
            except FileNotFoundError:
                self._add_message(Text(f"'{cli_name}' CLI not found on PATH.", style="red"))
                return

        self.gateway.refresh_auth()
        self._add_message(Text(f"Refreshed credentials for '{cli_name}'.", style="green"))

    # -- history meta-answers ------------------------------------------

    def _try_answer_locally(self, text: str) -> bool:
        """Answer meta-questions about conversation history from local state.
        Returns True if answered locally (caller should skip CLI routing).
        Adds the exchange to history so future turns retain the context."""
        if not _HISTORY_META_RE.search(text):
            return False
        user_turns = [m for m in self.history if m["role"] == Role.USER]
        if not user_turns:
            answer = "No previous messages in this session."
        else:
            last = user_turns[-1]["content"]
            answer = f"Your last message:\n\n```\n{last}\n```"
        self._add_message(Markdown(answer))
        # Record in history so subsequent turns have the context.
        self.history.append({"role": Role.USER, "content": text})
        self.history.append({"role": Role.ASSISTANT, "content": answer})
        return True

    # -- input handling ------------------------------------------------

    _AT_FILE_RE = re.compile(r"@(https?://[^\s]+|[\w./\-]+)")

    def _expand_file_refs(self, text: str) -> tuple[str, list[str], list[Path]]:
        """Replaces @path / @url tokens with content inline.
        Media files (images/PDFs/videos) are stripped from the text and
        returned separately as Path objects for per-CLI handling in the gateway.
        Returns (expanded_text, injected_labels, media_files)."""
        injected: list[str] = []
        media_files: list[Path] = []

        cwd = Path.cwd().resolve()

        def replace(m: re.Match) -> str:
            token = m.group(1)
            if token.startswith("http://") or token.startswith("https://"):
                token = token.rstrip(".,;:!?)>]<")
                try:
                    content = fetch_url(token)
                    injected.append(token)
                    return (
                        f"\n\n<untrusted-external-content source=\"{token}\">\n"
                        f"{content}\n"
                        f"</untrusted-external-content>\n"
                    )
                except Exception:
                    return m.group(0)
            path = (cwd / token).resolve()
            if not path.is_relative_to(cwd):
                return m.group(0)
            if not path.is_file():
                return m.group(0)
            if path.suffix.lower() in MEDIA_EXTS:
                media_files.append(path)
                injected.append(token)
                return ""  # removed from text; gateway injects per-CLI
            try:
                content = path.read_text(errors="replace")
                injected.append(token)
                return (
                    f"\n\n<untrusted-file-content path=\"{token}\">\n"
                    f"```\n{content}\n```\n"
                    f"</untrusted-file-content>\n"
                )
            except Exception:
                return m.group(0)

        expanded = self._AT_FILE_RE.sub(replace, text)
        return expanded, injected, media_files

    def action_submit_input(self) -> None:
        """Submit the current input."""
        text_area = self.query_one("#input-area", TextArea)
        text = text_area.text.strip()
        text_area.text = ""
        if not text:
            return

        if text.lower() in ("exit", "quit"):
            self.exit()
            return

        if text.startswith("/"):
            self._handle_command(text)
            return

        # Answer meta-questions (e.g. "what was my last message") from local
        # history without routing to a CLI that has no awareness of prior turns.
        if self._try_answer_locally(text):
            self._prompt_history.append(text)
            self._history_index = len(self._prompt_history)
            return

        expanded, injected, media_files = self._expand_file_refs(text)
        self._add_message(Static(f"  {text}", classes="user-msg"))
        if injected:
            self._add_message(Text(f"  @{' @'.join(injected)}", style="dim cyan"))
        self._prompt_history.append(text)
        self._history_index = len(self._prompt_history)
        self._run_prompt(expanded, media_files)

    def _handle_command(self, user_input: str) -> None:
        cmd = user_input[1:].lower()

        if cmd == "exit":
            self.exit()
        elif cmd == "clear":
            self.history = [{"role": Role.SYSTEM, "content": self.system_msg}]
            self._history_scroll.remove_children()
        elif cmd == "compact":
            self._run_compact()
        elif cmd.startswith("login"):
            parts = cmd.split(" ")
            valid_display = [_CLI_DISPLAY.get(c, c) for c in list(Gateway.LOGIN_COMMANDS) + [CLI.OLLAMA]]
            if len(parts) != 2:
                self._add_message(Text(f"Usage: /login <{'|'.join(valid_display)}>", style="red"))
            else:
                cli_name = _LOGIN_ALIASES.get(parts[1], parts[1])
                self._login(cli_name)
        elif cmd.startswith("effort "):
            tier = cmd.split(" ")[1]
            if tier in ("low", "medium", "high"):
                self.effort_tier = tier
                self._add_message(Text(f"Effort tier set to {tier}", style="yellow"))
            else:
                self._add_message(Text(f"Invalid tier: {tier}", style="red"))
            self._update_status()
        elif cmd.startswith("cli"):
            parts = cmd.split(" ")
            valid = ["auto", *Gateway.CLI_DEFAULT_MODELS]
            if len(parts) != 2 or parts[1] not in valid:
                self._add_message(Text(f"Usage: /cli <{'|'.join(valid)}>", style="red"))
            else:
                self.cli_override = None if parts[1] == "auto" else parts[1]
                self._add_message(Text(f"CLI backend set to {parts[1]}", style="yellow"))
            self._update_status()
        elif cmd.startswith("model"):
            parts = user_input.split(" ", 1)  # preserve model name case
            if len(parts) != 2:
                self._add_message(Text("Usage: /model <name|auto>", style="red"))
            else:
                self.model_override = None if parts[1].lower() == "auto" else parts[1]
                self._add_message(Text(f"Model override set to {parts[1]}", style="yellow"))
            self._update_status()
        elif cmd.startswith("mode"):
            parts = cmd.split(" ")
            if len(parts) != 2 or parts[1] not in (Mode.PLAN, Mode.YOLO):
                self._add_message(Text("Usage: /mode <plan|yolo>", style="red"))
            else:
                self.mode = parts[1]
                desc = (
                    "read-only - no file edits or commands" if self.mode == "plan"
                    else "auto-approve edits and commands"
                )
                self._add_message(Text(f"Mode set to {self.mode} ({desc})", style="yellow"))
            self._update_status()
        elif cmd == "sessions":
            saved = self.sessions.list_sessions()
            if not saved:
                self._add_message(Text("No saved sessions.", style="dim"))
            else:
                lines = []
                for s in saved:
                    marker = " (current)" if s["id"] == self.session_id else ""
                    lines.append(Text(f"{s['id']}{marker} - {s['preview']}"))
                self._add_message(Group(*lines))
        elif cmd.startswith("resume"):
            parts = cmd.split(" ")
            target = parts[1] if len(parts) == 2 else self.sessions.latest_session_id(exclude=self.session_id)
            data = self.sessions.load(target) if target else None
            if not data:
                self._add_message(Text(f"No session found: {target or '(none saved)'}", style="red"))
            else:
                self.session_id = target
                self.history = data.get("history", self.history)
                self.effort_tier = data.get("effort_tier")
                self.cli_override = data.get("cli_override")
                self.model_override = data.get("model_override")
                self.mode = data.get("mode", Mode.YOLO)
                self.usage_log = data.get("usage_log", [])
                self.system_msg = self._build_system_msg_base()
                self._refresh_system_msg()
                self._render_history()
                self._add_message(Text(f"Resumed session {self.session_id}", style="yellow"))
                self._update_status()
        elif cmd == "cost":
            if not self.usage_log:
                self._add_message(Text("No usage recorded yet this session.", style="dim"))
            else:
                table = Table(title="Session Usage")
                table.add_column("#", justify="right")
                table.add_column("CLI")
                table.add_column("Model")
                table.add_column("Tier")
                table.add_column("Tokens", justify="right")
                for i, entry in enumerate(self.usage_log, 1):
                    tokens = entry.get("tokens")
                    table.add_row(
                        str(i),
                        entry.get("cli", "?"),
                        entry.get("model", "?"),
                        entry.get("tier", "?"),
                        str(tokens) if tokens is not None else "n/a",
                    )
                total_tokens = sum(entry.get("tokens") or 0 for entry in self.usage_log)
                self._add_message(Group(
                    table,
                    Text(f"Total tokens (where reported): {total_tokens}", style="dim"),
                    Text("Note: BYOS subscriptions are flat-rate; token counts are usage indicators, not billed cost.", style="dim"),
                ))
        elif cmd.startswith("sdd"):
            task = user_input[len("/sdd"):].strip()
            if not task:
                self._add_message(Text('Usage: /sdd <task description>', style="red"))
            else:
                self._run_sdd(task)
        elif cmd.startswith("parallel"):
            try:
                prompts = shlex.split(user_input[len("/parallel"):].strip())
            except ValueError as e:
                self._add_message(Text(f"Could not parse prompts: {e}", style="red"))
                return
            if len(prompts) < 2:
                self._add_message(Text('Usage: /parallel "task one" "task two" ...', style="red"))
                return
            self._add_message(Text(
                f"Running {len(prompts)} tasks in parallel "
                f"(each spawns its own CLI process; agentic edits to the same "
                f"files may conflict)...",
                style="yellow",
            ))
            self._run_parallel(prompts)
        elif cmd.startswith("compare"):
            prompt = user_input[len("/compare"):].strip()
            if not prompt:
                self._add_message(Text('Usage: /compare <prompt>', style="red"))
                return
            active = [cli for cli, ok in self.gateway.cli_auth_status.items() if ok]
            if len(active) < 2:
                self._add_message(Text("Need at least 2 active CLIs to compare.", style="red"))
                return
            self._add_message(Text(
                f"Comparing across {len(active)} CLIs: {', '.join(active)}...",
                style="yellow",
            ))
            self._run_compare(prompt, active)
        elif cmd == "skills":
            names = _skill_names()
            if not names:
                self._add_message(Text(
                    f"No skills found. Create .md files in {SKILLS_DIR}/",
                    style="dim",
                ))
            else:
                self._add_message(Group(
                    Text(f"Available skills ({SKILLS_DIR}):", style="bold"),
                    *[Text(f"  /skill {n}", style="cyan") for n in names],
                ))
        elif cmd.startswith("skill"):
            parts = user_input.split(None, 2)  # /skill <name> [prompt]
            if len(parts) < 2:
                self._add_message(Text("Usage: /skill <name> [prompt]", style="red"))
                return
            skill_name = parts[1]
            skill_prompt = parts[2].strip() if len(parts) > 2 else ""
            skill_file = SKILLS_DIR / f"{skill_name}.md"
            try:
                skill_file = skill_file.resolve()
                if not skill_file.is_relative_to(SKILLS_DIR.resolve()):
                    self._add_message(Text("Invalid skill name.", style="red"))
                    return
            except Exception:
                self._add_message(Text("Invalid skill name.", style="red"))
                return
            if not skill_file.exists():
                names = _skill_names()
                hint = f"  Available: {', '.join(names)}" if names else f"  No skills in {SKILLS_DIR}/"
                self._add_message(Text(f"Skill '{skill_name}' not found.{hint}", style="red"))
                return
            skill_content = skill_file.read_text().strip()
            prompt = f"{skill_content}\n\n{skill_prompt}".strip() if skill_prompt else skill_content
            self._add_message(Text(f"> /skill {skill_name}" + (f" {skill_prompt}" if skill_prompt else ""), style="dim"))
            self._add_message(Text(f"  skill: {skill_name}", style="dim cyan"))
            self._run_prompt(prompt)
        elif cmd.startswith("search ") or cmd == "search":
            query = user_input[len("/search"):].strip()
            if not query:
                self._add_message(Text("Usage: /search <query>", style="red"))
            else:
                self._run_search(query)
        elif cmd == "memory":
            if not MEMORY_FILE.exists() or not MEMORY_FILE.read_text().strip():
                self._add_message(Text("No project memory yet. Use /remember <fact>", style="dim"))
            else:
                self._add_message(Markdown(MEMORY_FILE.read_text()))
        elif cmd.startswith("remember "):
            fact = user_input[len("/remember "):].strip()
            if fact:
                MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
                with MEMORY_FILE.open("a") as f:
                    f.write(f"- {fact}\n")
                self._refresh_system_msg()
                self._add_message(Text(f"Remembered: {fact}", style="green"))
        elif cmd.startswith("forget "):
            pattern = user_input[len("/forget "):].strip().lower()
            if MEMORY_FILE.exists():
                lines = MEMORY_FILE.read_text().splitlines(keepends=True)
                kept = [l for l in lines if pattern not in l.lower()]
                removed = len(lines) - len(kept)
                MEMORY_FILE.write_text("".join(kept))
                self._refresh_system_msg()
                self._add_message(Text(
                    f"Removed {removed} line(s) matching '{pattern}'" if removed else f"No lines matched '{pattern}'",
                    style="yellow",
                ))
        elif cmd == "help":
            self._add_message(Markdown(HELP_TEXT))
        elif cmd == "doctor":
            self._run_doctor()
        elif cmd == "tasks":
            self._show_tasks()
        elif cmd.startswith("loop"):
            rest = user_input[len("/loop"):].strip()
            parts = rest.split(None, 1)
            if not rest:
                self._add_message(Text("Usage: /loop [N] <prompt>  (N=count, omit for infinite)", style="red"))
            elif parts and parts[0].isdigit():
                count = int(parts[0])
                prompt = parts[1].strip() if len(parts) > 1 else ""
                if not prompt:
                    self._add_message(Text("Usage: /loop [N] <prompt>", style="red"))
                else:
                    self._run_loop(prompt, count)
            else:
                self._run_loop(rest, -1)
        elif cmd.startswith("commit"):
            msg_override = user_input[len("/commit"):].strip()
            self._run_commit(msg_override)
        elif cmd.startswith("pr"):
            title_override = user_input[len("/pr"):].strip()
            self._run_pr(title_override)
        else:
            self._add_message(Text(f"Unknown command: {cmd}", style="red"))

    def action_history_up(self) -> None:
        if self._prompt_history:
            if self._history_index > 0:
                self._history_index -= 1
            self.query_one("#input-area", TextArea).text = self._prompt_history[self._history_index]

    def action_history_down(self) -> None:
        if self._prompt_history:
            if self._history_index < len(self._prompt_history) - 1:
                self._history_index += 1
                self.query_one("#input-area", TextArea).text = self._prompt_history[self._history_index]
            elif self._history_index == len(self._prompt_history) - 1:
                self._history_index = len(self._prompt_history)
                self.query_one("#input-area", TextArea).text = ""

    def _run_doctor(self) -> None:
        """Show health status for all CLIs, MCP server, and project structure."""
        rows = []
        auth = self.gateway.cli_auth_status

        cli_bin_map = {
            CLI.CLAUDE: "claude",
            CLI.AGY:    "agy",
            CLI.CODEX:  "codex",
            CLI.OLLAMA: "ollama",
        }
        for cli, bin_name in cli_bin_map.items():
            on_path = shutil.which(bin_name) is not None
            authenticated = auth.get(cli, False)
            path_icon = "✓" if on_path else "✗"
            auth_icon = "✓" if authenticated else ("–" if not on_path else "✗")
            rows.append((
                _CLI_DISPLAY.get(cli, cli),
                Text(f"{path_icon} on PATH", style="green" if on_path else "red"),
                Text(f"{auth_icon} auth", style="green" if authenticated else ("dim" if not on_path else "red")),
            ))

        table = Table(title="CLI Health", show_lines=False)
        table.add_column("CLI")
        table.add_column("Binary")
        table.add_column("Auth")
        for name, path_txt, auth_txt in rows:
            table.add_row(name, path_txt, auth_txt)

        # MCP server
        from codehydra.routing.gateway import _SHARED_MCP
        mcp_port = _SHARED_MCP.get("port")
        mcp_line = Text(
            f"✓ MCP server running on port {mcp_port}" if mcp_port else "✗ MCP server not started",
            style="green" if mcp_port else "yellow",
        )

        # Project files
        mem_line = Text(
            f"✓ Memory: {MEMORY_FILE}" if MEMORY_FILE.exists() else "– No memory file (.codehydra/memory.md)",
            style="green" if MEMORY_FILE.exists() else "dim",
        )
        skills_count = len(_skill_names())
        skill_line = Text(
            f"✓ Skills: {skills_count} in {SKILLS_DIR}" if skills_count else f"– No skills ({SKILLS_DIR})",
            style="green" if skills_count else "dim",
        )
        session_line = Text(f"✓ Session: {self.session_id}", style="dim")

        self._add_message(Group(table, Text(""), mcp_line, mem_line, skill_line, session_line))

    _AT_PARTIAL_RE = re.compile(r"@([\w./\-]*)$")

    def _complete_at_ref(self) -> None:
        """Tab-complete: @path file tokens and /slash commands."""
        area = self.query_one("#input-area", TextArea)
        text = area.text
        row, col = area.cursor_location
        lines = text.split("\n")
        line = lines[row] if row < len(lines) else ""
        before = line[:col]

        # /command completion
        if before.startswith("/") and " " not in before:
            lower = before.lower()
            candidates = SLASH_COMMANDS + [f"/skill {n}" for n in _skill_names()]
            match = next((c for c in candidates if c.startswith(lower) and c != lower), None)
            if match:
                lines[row] = match + line[col:]
                area.text = "\n".join(lines)
                area.move_cursor((row, len(match)))
            return

        # @path completion
        m = self._AT_PARTIAL_RE.search(before)
        if not m:
            area.insert("    ")
            return
        partial = m.group(1)
        cwd = Path.cwd()
        try:
            if not partial or partial.endswith("/"):
                candidates = sorted((cwd / partial).glob("*") if partial else cwd.glob("*"))[:8]
            else:
                parent = Path(partial).parent
                stem = Path(partial).name
                candidates = sorted((cwd / parent).glob(f"{stem}*"))[:8]
        except Exception:
            return
        if not candidates:
            return
        best = candidates[0]
        try:
            rel = str(best.relative_to(cwd))
        except ValueError:
            rel = str(best)
        if best.is_dir():
            rel += "/"
        new_before = before[: m.start()] + f"@{rel}"
        lines[row] = new_before + line[col:]
        area.text = "\n".join(lines)
        area.move_cursor((row, len(new_before)))

    def _open_in_editor(self) -> None:
        """Open current prompt in $EDITOR (Ctrl+E). Suspends TUI, restores text after."""
        import os as _os
        import tempfile
        editor = _os.environ.get("EDITOR") or _os.environ.get("VISUAL") or "vi"
        editor_parts = shlex.split(editor)
        area = self.query_one("#input-area", TextArea)
        current = area.text
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".md", mode="w", delete=False) as f:
                f.write(current)
                tmp_path = Path(f.name)
            with self.suspend():
                subprocess.run(editor_parts + [str(tmp_path)], check=False)
            area.text = tmp_path.read_text()
            area.move_cursor_to_end()
        except Exception as e:
            self._add_message(Text(f"Editor error: {e}", style="red"))
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

    @work(thread=True, exclusive=True, group="prompt")
    def _run_prompt(self, prompt: str, media_files: list[Path] | None = None) -> None:
        self._cancel_event.clear()
        self.call_from_thread(self._set_request_active, True)
        try:
            self._run_prompt_inner(prompt, media_files)
        finally:
            self.call_from_thread(self._set_request_active, False)

    @work(thread=True, exclusive=True, group="prompt")
    def _run_loop(self, prompt: str, count: int) -> None:
        """Repeat prompt count times (count=-1 = infinite). Ctrl+C stops loop."""
        self._loop_stop.clear()
        iteration = 0
        while not self._loop_stop.is_set():
            if count != -1 and iteration >= count:
                break
            iteration += 1
            label = f"♻ Loop iteration {iteration}" + (f"/{count}" if count != -1 else "")
            self.call_from_thread(self._add_message, Text(label, style="dim cyan"))
            self._cancel_event.clear()
            self.call_from_thread(self._set_request_active, True)
            try:
                self._run_prompt_inner(prompt)
            finally:
                self.call_from_thread(self._set_request_active, False)
            if self._cancel_event.is_set():
                break
        self.call_from_thread(self._add_message, Text(f"♻ Loop done ({iteration} iteration{'s' if iteration != 1 else ''})", style="dim cyan"))

    def _set_request_active(self, active: bool) -> None:
        self._request_active = active
        self._update_status()

    def _run_prompt_inner(self, prompt: str, media_files: list[Path] | None = None) -> None:
        self.call_from_thread(self._refresh_system_msg)
        turns = [m for m in self.history if m["role"] != Role.SYSTEM]
        history_chars = sum(len(m["content"]) for m in turns)
        if history_chars > self._AUTO_COMPACT_CHARS:
            self.call_from_thread(self._add_message, Text("↩ Auto-compacting history…", style="dim yellow"))
            try:
                msg = self._do_compact()
                if msg:
                    self.call_from_thread(self._add_message, Text(f"↩ {msg}", style="dim yellow"))
            except Exception as e:
                self.call_from_thread(self._add_message, Text(f"Auto-compact failed: {e}", style="red"))
        # Throttle Markdown re-renders: only push UI update every 100ms to avoid
        # re-parsing the full markdown tree on every streamed token.
        _MD_INTERVAL = 0.1  # seconds
        _THINK_FRAMES = ("⟳", "◐", "◓", "◑", "◒")
        _MAX_BUILD_RETRIES = 3

        for _build_attempt in range(_MAX_BUILD_RETRIES):
            if self._cancel_event.is_set():
                return
            md_w = self.call_from_thread(self._add_response_turn)
            thinking_w = None
            thinking_start = None
            thinking_chars = 0
            response = ""
            in_thinking = False
            last_md_push = 0.0
            try:
                for chunk in self.gateway.request_stream(
                    prompt,
                    tier=self.effort_tier,
                    history=self.history,
                    cli_override=self.cli_override,
                    model_override=self.model_override,
                    mode=self.mode,
                    media_files=media_files or [],
                ):
                    if self._cancel_event.is_set():
                        if thinking_w is not None:
                            self.call_from_thread(thinking_w.remove)
                        self.call_from_thread(md_w.update, f"{response}\n\n*[cancelled]*")
                        return
                    if chunk == THINKING_START:
                        in_thinking = True
                        thinking_start = time.time()
                        thinking_w = self.call_from_thread(self._mount_thinking_widget, md_w)
                        continue
                    if chunk == THINKING_END:
                        in_thinking = False
                        if thinking_w is not None and thinking_start is not None:
                            secs = int(time.time() - thinking_start)
                            label = f"↓ Thought for {secs}s" if secs > 0 else "↓ Thought"
                            self.call_from_thread(thinking_w.set_classes, "thinking-done")
                            self.call_from_thread(thinking_w.update, label)
                        continue
                    if in_thinking:
                        thinking_chars += len(chunk)
                        # Animate thinking indicator every 500 chars of thought.
                        if thinking_w is not None and thinking_chars % 500 < len(chunk):
                            frame = _THINK_FRAMES[(thinking_chars // 500) % len(_THINK_FRAMES)]
                            self.call_from_thread(thinking_w.update, f"{frame} Thinking… ({thinking_chars:,} chars)")
                    else:
                        response += chunk
                        now = time.monotonic()
                        if now - last_md_push >= _MD_INTERVAL:
                            self.call_from_thread(md_w.update, response)
                            self.call_from_thread(self._history_scroll.scroll_end, animate=False)
                            last_md_push = now
            except Exception as e:
                if thinking_w is not None:
                    # Collapse the thinking indicator rather than vanishing it,
                    # so the user sees the model was mid-thought when it failed.
                    self.call_from_thread(thinking_w.set_classes, "thinking-done")
                    self.call_from_thread(thinking_w.update, "↓ Thinking interrupted")
                self.call_from_thread(md_w.update, f"**Error:** {e}")
                return

            # Final flush — ensure last partial chunk is displayed.
            if response:
                self.call_from_thread(md_w.update, response)
                self.call_from_thread(self._history_scroll.scroll_end, animate=False)
            else:
                # Model produced no text (thinking-only or empty output).
                # Remove the blank Markdown widget to avoid empty whitespace.
                self.call_from_thread(md_w.remove)

            usage = self.gateway.last_usage
            if usage:
                display_cli = _CLI_DISPLAY.get(usage["cli"], usage["cli"])
                tag = f"[{display_cli}/{usage['model'].split('/')[-1]}]"
                self.call_from_thread(self._add_message, Text(tag, style="dim"))
            self.call_from_thread(self._update_status)

            # Always record the user turn. Only record the assistant turn if
            # there is actual content — an empty string confuses LLMs on the
            # next turn and causes API validation errors on some providers.
            self.history.append({"role": Role.USER, "content": prompt})
            if response:
                self.history.append({"role": Role.ASSISTANT, "content": response})
            if usage:
                self.usage_log.append(usage)
            self._save_session()

            patched_files = self.patcher.apply_all_patches(response)
            if not patched_files:
                return

            self.call_from_thread(self._add_message, Text(f"Applied patches to: {', '.join(patched_files)}", style="green"))
            exit_code, output = self.compiler.run_build()
            if exit_code == 0:
                self.call_from_thread(self._add_message, Text("✅ Build successful!", style="bold green"))
                return

            attempt_num = _build_attempt + 1
            if attempt_num >= _MAX_BUILD_RETRIES:
                self.call_from_thread(self._add_message, Text(f"❌ Build failed after {attempt_num} attempts. Stopping.", style="bold red"))
                return
            self.call_from_thread(self._add_message, Text(f"❌ Build failed (exit {exit_code}). Feeding back to Hydra… (attempt {attempt_num}/{_MAX_BUILD_RETRIES})", style="bold red"))
            prompt = f"The build failed with the following error:\n```\n{output}\n```\nPlease fix the code."

    # Auto-compact when non-system history exceeds this many characters.
    _AUTO_COMPACT_CHARS = 60_000

    def _do_compact(self) -> str | None:
        """Compact history in-place. Returns a status string, or None if nothing to compact.
        Must be called from a worker thread (calls gateway.request which blocks)."""
        turns = [m for m in self.history if m["role"] != Role.SYSTEM]
        if len(turns) < 6:
            return None
        to_summarize = turns[:-4]
        recent = turns[-4:]
        conv_text = "\n\n".join(
            f"{m['role'].upper()}: {m['content'][:1000]}" for m in to_summarize
        )
        summary_prompt = (
            "Summarize the conversation below in 5-8 concise bullet points. "
            "Capture: key decisions, files changed, bugs fixed, commands run, "
            "and any unresolved issues. Be specific — mention filenames and function names.\n\n"
            f"{conv_text}"
        )
        auth = self.gateway.cli_auth_status
        best_cli = next(
            (c for c in (CLI.CLAUDE, CLI.AGY, CLI.CODEX) if auth.get(c)),
            self.cli_override,
        )
        summary = self.gateway.request(
            summary_prompt,
            tier="low",
            history=[],
            cli_override=best_cli,
            mode=Mode.PLAN,
        )
        system = [m for m in self.history if m["role"] == Role.SYSTEM]
        self.history = system + [
            {"role": Role.ASSISTANT, "content": f"[Compacted history — {len(to_summarize)//2} earlier turns]\n{summary.strip()}"}
        ] + recent
        self._save_session()
        return f"Compacted {len(to_summarize)//2} turns → summary + {len(recent)//2} recent turns kept"

    @work(thread=True, exclusive=True, group="prompt")
    def _run_compact(self) -> None:
        self.call_from_thread(self._inc_workers)
        try:
            turns = [m for m in self.history if m["role"] != Role.SYSTEM]
            if len(turns) < 6:
                self.call_from_thread(self._add_message, Text("Not enough history to compact.", style="dim"))
                return
            self.call_from_thread(self._add_message, Text("Compacting history…", style="dim yellow"))
            try:
                msg = self._do_compact()
            except Exception as e:
                self.call_from_thread(self._add_message, Text(f"Compact failed: {e}", style="red"))
                return
            self.call_from_thread(self._add_message, Text(msg or "Nothing to compact.", style="green"))
        finally:
            self.call_from_thread(self._dec_workers)

    @work(thread=True, group="search")
    def _run_search(self, query: str) -> None:
        self.call_from_thread(self._inc_workers)
        try:
            self.call_from_thread(self._add_message, Text(f"Searching: {query}…", style="dim yellow"))
            try:
                results = search(query)
                formatted = format_results(results)
            except Exception as e:
                self.call_from_thread(self._add_message, Text(f"Search failed: {e}", style="red"))
                return
            self.call_from_thread(self._add_message, Markdown(formatted))
            content = f"[Web search results for: {query}]\n{formatted}"
            self.history.append({"role": Role.USER,      "content": f"/search {query}"})
            self.history.append({"role": Role.ASSISTANT, "content": content})
        finally:
            self.call_from_thread(self._dec_workers)

    @work(thread=True, group="parallel")
    def _run_sdd(self, task: str) -> None:
        """Spec-Driven Development: spec agent → parse tasks → parallel implementation agents."""
        self.call_from_thread(self._inc_workers)
        try:
            self._run_sdd_inner(task)
        finally:
            self.call_from_thread(self._dec_workers)

    def _run_sdd_inner(self, task: str) -> None:
        import re as _re

        # Phase 1 — spec generation with a high-tier model in plan mode.
        self.call_from_thread(self._add_message, Text(
            "SDD phase 1: generating spec…", style="dim yellow"
        ))
        spec_prompt = (
            "You are a software architect. Create a concise spec for this task.\n\n"
            f"TASK: {task}\n\n"
            "Respond in this exact format:\n"
            "## Spec\n"
            "<one-paragraph description>\n\n"
            "## Requirements\n"
            "- <requirement>\n\n"
            "## Tasks\n"
            "TASK 1: <fully self-contained implementation prompt — include all context an "
            "agent needs with zero knowledge of the overall project>\n"
            "TASK 2: <same>\n"
            "TASK 3: <same>\n\n"
            "Rules: 3–5 tasks, each fully independent (no task depends on another's output), "
            "each reads as a standalone coding prompt."
        )
        auth = self.gateway.cli_auth_status
        best_cli = next((c for c in (CLI.CLAUDE, CLI.AGY, CLI.CODEX) if auth.get(c)), None)
        if not best_cli:
            self.call_from_thread(self._add_message, Text("No CLI available.", style="red"))
            return
        try:
            spec = self.gateway.request(
                spec_prompt,
                tier=Tier.HIGH,
                history=[],
                cli_override=best_cli,
                mode=Mode.PLAN,
            ).strip()
        except Exception as e:
            self.call_from_thread(self._add_message, Text(f"Spec generation failed: {e}", style="red"))
            return

        self.call_from_thread(self._add_message, Markdown(spec))

        # Phase 2 — parse TASK lines from spec.
        task_lines = _re.findall(r"^TASK \d+:\s*(.+)$", spec, _re.MULTILINE)
        if not task_lines:
            self.call_from_thread(self._add_message, Text(
                "No TASK lines found in spec output. Try rephrasing or use /parallel.", style="yellow"
            ))
            return

        self.call_from_thread(self._add_message, Text(
            f"SDD phase 2: running {len(task_lines)} parallel agents…", style="dim yellow"
        ))

        # Phase 3 — parallel implementation (each task gets its own Gateway instance).
        parent_auth = self.gateway.cli_auth_status
        history_snapshot = list(self.history)

        def run_task(t: str):
            gw = Gateway()
            gw.cli_auth_status = parent_auth
            result = gw.request(
                t,
                tier=self.effort_tier or Tier.MEDIUM,
                history=history_snapshot,
                cli_override=self.cli_override,
                mode=self.mode,
            )
            return result, gw.last_usage

        with ThreadPoolExecutor(max_workers=min(8, len(task_lines))) as executor:
            futures = {executor.submit(run_task, t): t for t in task_lines}
            for future in as_completed(futures):
                t = futures[future]
                try:
                    result, usage = future.result()
                except Exception as e:
                    self.call_from_thread(self._add_message, Text(f"❌ {t[:60]}: {e}", style="red"))
                    continue
                tag = f"[{usage['cli']}/{usage['model'].split('/')[-1]}]" if usage else ""
                self.call_from_thread(self._add_message, Static(f"  {t[:80]}", classes="user-msg"))
                self.call_from_thread(self._add_message, Markdown(result))
                if tag:
                    self.call_from_thread(self._add_message, Text(tag, style="dim"))
                if usage:
                    self.usage_log.append(usage)

        combined = f"[SDD spec]\n{spec}\n\n[Tasks]\n" + "\n".join(f"- {t}" for t in task_lines)
        self.history.append({"role": Role.USER, "content": f"/sdd {task}"})
        self.history.append({"role": Role.ASSISTANT, "content": combined})
        self._save_session()

    @work(thread=True, group="parallel")
    def _run_parallel(self, prompts: list[str]) -> None:
        self.call_from_thread(self._inc_workers)
        try:
            self._run_parallel_inner(prompts)
        finally:
            self.call_from_thread(self._dec_workers)

    def _run_parallel_inner(self, prompts: list[str]) -> None:
        parent_auth = self.gateway.cli_auth_status
        history_snapshot = list(self.history)

        def run_task(prompt: str):
            gw = Gateway()
            gw.cli_auth_status = parent_auth  # skip redundant CLI auth subprocesses
            result = gw.request(
                prompt,
                tier=self.effort_tier,
                history=history_snapshot,
                cli_override=self.cli_override,
                model_override=self.model_override,
                mode=self.mode,
            )
            return result, gw.last_usage

        with ThreadPoolExecutor(max_workers=max(1, len(prompts))) as executor:
            futures = {executor.submit(run_task, p): p for p in prompts}
            for future in as_completed(futures):
                prompt = futures[future]
                try:
                    result, usage = future.result()
                except Exception as e:
                    self.call_from_thread(self._add_message, Text(f"❌ {prompt[:60]}: {e}", style="red"))
                    continue

                tag = f"[{usage['cli']}/{usage['model'].split('/')[-1]}]" if usage else ""
                self.call_from_thread(self._add_message, Static(f"  {prompt[:60]}", classes="user-msg"))
                self.call_from_thread(self._add_message, Markdown(result))
                if tag:
                    self.call_from_thread(self._add_message, Text(tag, style="dim"))
                self.history.append({"role": Role.USER, "content": prompt})
                self.history.append({"role": Role.ASSISTANT, "content": result})
                if usage:
                    self.usage_log.append(usage)

        self._save_session()

    @work(thread=True, group="compare")
    def _run_compare(self, prompt: str, cli_names: list[str]) -> None:
        self.call_from_thread(self._inc_workers)
        try:
            self._run_compare_inner(prompt, cli_names)
        finally:
            self.call_from_thread(self._dec_workers)

    def _run_compare_inner(self, prompt: str, cli_names: list[str]) -> None:
        """Sends the same prompt to every active CLI in parallel and renders
        each response as a labelled block so the user can compare answers."""
        parent_auth = self.gateway.cli_auth_status
        history_snapshot = list(self.history)

        def run_one(cli: str):
            gw = Gateway()
            gw.cli_auth_status = parent_auth
            t0 = time.monotonic()
            result = gw.request(
                prompt,
                tier=self.effort_tier,
                history=history_snapshot,
                cli_override=cli,
                mode=self.mode,
            )
            elapsed = time.monotonic() - t0
            return result, gw.last_usage, elapsed

        results_by_cli: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=max(1, len(cli_names))) as executor:
            futures = {executor.submit(run_one, cli): cli for cli in cli_names}
            for future in as_completed(futures):
                cli = futures[future]
                try:
                    result, usage, elapsed = future.result()
                except Exception as e:
                    self.call_from_thread(
                        self._add_message,
                        Text(f"❌ {cli}: {e}", style="red"),
                    )
                    continue

                results_by_cli[cli] = result
                model_tag = usage["model"].split("/")[-1] if usage else cli
                header = Text(f"── {cli} / {model_tag}  ({elapsed:.1f}s) ──", style="bold cyan")
                self.call_from_thread(self._add_message, header)
                self.call_from_thread(self._add_message, Markdown(result))
                if usage:
                    self.usage_log.append(usage)

        if results_by_cli:
            combined = "\n\n---\n\n".join(
                f"**{cli}:**\n{results_by_cli[cli]}"
                for cli in cli_names
                if cli in results_by_cli
            )
            self.history.append({"role": Role.USER, "content": prompt})
            self.history.append({"role": Role.ASSISTANT, "content": f"[Compare]\n{combined}"})
        self._save_session()


    def _show_tasks(self) -> None:
        """Show background tasks spawned via the run_in_background MCP tool."""
        from codehydra.mcp.server import BG_DIR
        import json as _json
        import os as _os
        if not BG_DIR.exists():
            self._add_message(Text("No background tasks yet.", style="dim"))
            return
        task_dirs = sorted(d for d in BG_DIR.iterdir() if d.is_dir())
        if not task_dirs:
            self._add_message(Text("No background tasks yet.", style="dim"))
            return
        table = Table(title="Background Tasks", show_lines=False)
        table.add_column("ID")
        table.add_column("Status")
        table.add_column("Command")
        table.add_column("Last output")
        for task_dir in task_dirs:
            meta_path = task_dir / "meta.json"
            if not meta_path.exists():
                continue
            try:
                meta = _json.loads(meta_path.read_text())
            except Exception:
                continue
            log_path = task_dir / "output.log"
            tail = ""
            if log_path.exists():
                lines = log_path.read_text().splitlines()
                tail = lines[-1][:60] if lines else ""
            try:
                _os.kill(meta["pid"], 0)
                status = Text("running", style="green")
            except OSError:
                status = Text("done", style="dim")
            table.add_row(task_dir.name, status, meta["command"][:50], tail)
        self._add_message(table)

    @work(thread=True, exclusive=True, group="git")
    def _run_commit(self, message_override: str = "") -> None:
        self.call_from_thread(self._inc_workers)
        try:
            self._run_commit_inner(message_override)
        finally:
            self.call_from_thread(self._dec_workers)

    def _run_commit_inner(self, message_override: str = "") -> None:
        check = subprocess.run(["git", "rev-parse", "--git-dir"], capture_output=True, text=True)
        if check.returncode != 0:
            self.call_from_thread(self._add_message, Text("Not a git repository.", style="red"))
            return

        diff = subprocess.run(["git", "diff", "--cached"], capture_output=True, text=True).stdout
        if not diff.strip():
            self.call_from_thread(self._add_message, Text("Staging all changes…", style="dim yellow"))
            subprocess.run(["git", "add", "-A"], capture_output=True)
            diff = subprocess.run(["git", "diff", "--cached"], capture_output=True, text=True).stdout
            if not diff.strip():
                self.call_from_thread(self._add_message, Text("Nothing to commit.", style="dim"))
                return

        if message_override:
            commit_msg = message_override
        else:
            self.call_from_thread(self._add_message, Text("Generating commit message…", style="dim yellow"))
            prompt = (
                "Write a conventional commit message for this diff.\n"
                "Format: type(scope): short description\n"
                "Types: feat, fix, refactor, docs, test, chore\n"
                "Rules: imperative mood, max 72 chars, no period, no quotes.\n"
                "Reply with ONLY the commit message, nothing else.\n\n"
                f"```diff\n{diff[:4000]}\n```"
            )
            auth = self.gateway.cli_auth_status
            best_cli = next((c for c in (CLI.CLAUDE, CLI.AGY, CLI.CODEX) if auth.get(c)), None)
            if not best_cli:
                self.call_from_thread(self._add_message, Text("No CLI available to generate message.", style="red"))
                return
            try:
                commit_msg = self.gateway.request(
                    prompt, tier="low", history=[], cli_override=best_cli, mode=Mode.PLAN,
                ).strip().strip('"').strip("'")
            except Exception as e:
                self.call_from_thread(self._add_message, Text(f"Message generation failed: {e}", style="red"))
                return

        result = subprocess.run(["git", "commit", "-m", commit_msg], capture_output=True, text=True)
        if result.returncode == 0:
            self.call_from_thread(self._add_message, Text(f"✓ {commit_msg}", style="green"))
        else:
            self.call_from_thread(self._add_message, Text(f"✗ {result.stderr.strip()}", style="red"))

    @work(thread=True, exclusive=True, group="git")
    def _run_pr(self, title_override: str = "") -> None:
        self.call_from_thread(self._inc_workers)
        try:
            self._run_pr_inner(title_override)
        finally:
            self.call_from_thread(self._dec_workers)

    def _run_pr_inner(self, title_override: str = "") -> None:
        if not shutil.which("gh"):
            self.call_from_thread(self._add_message, Text("'gh' not found. Install: https://cli.github.com/", style="red"))
            return

        branch = subprocess.run(
            ["git", "branch", "--show-current"], capture_output=True, text=True,
        ).stdout.strip()
        if not branch or branch in ("main", "master"):
            self.call_from_thread(self._add_message, Text(f"Cannot create PR from branch: {branch or '(detached)'}", style="red"))
            return

        log = subprocess.run(
            ["git", "log", "main..HEAD", "--oneline"], capture_output=True, text=True,
        ).stdout.strip()
        if not log:
            self.call_from_thread(self._add_message, Text("No commits ahead of main.", style="dim"))
            return

        diff_stat = subprocess.run(
            ["git", "diff", "main...HEAD", "--stat"], capture_output=True, text=True,
        ).stdout.strip()

        self.call_from_thread(self._add_message, Text("Generating PR description…", style="dim yellow"))
        prompt = (
            "Write a GitHub pull request title and description.\n\n"
            f"Branch: {branch}\nCommits:\n{log}\nFiles changed:\n{diff_stat}\n\n"
            "Reply in this exact format:\n"
            "TITLE: <title under 70 chars>\n\n"
            "BODY:\n<markdown with ## Summary and ## Test plan sections>"
        )
        auth = self.gateway.cli_auth_status
        best_cli = next((c for c in (CLI.CLAUDE, CLI.AGY, CLI.CODEX) if auth.get(c)), None)
        if not best_cli:
            self.call_from_thread(self._add_message, Text("No CLI available to generate description.", style="red"))
            return
        try:
            raw = self.gateway.request(
                prompt, tier="low", history=[], cli_override=best_cli, mode=Mode.PLAN,
            ).strip()
        except Exception as e:
            self.call_from_thread(self._add_message, Text(f"Description generation failed: {e}", style="red"))
            return

        title = title_override
        body = raw
        if not title_override:
            for line in raw.splitlines():
                if line.startswith("TITLE:"):
                    title = line[6:].strip()
                    break
            body_start = raw.find("BODY:")
            if body_start >= 0:
                body = raw[body_start + 5:].strip()
        if not title:
            title = branch.replace("-", " ").replace("_", " ")

        self.call_from_thread(self._add_message, Text(f"Pushing {branch}…", style="dim yellow"))
        push = subprocess.run(["git", "push", "-u", "origin", branch], capture_output=True, text=True)
        if push.returncode != 0:
            self.call_from_thread(self._add_message, Text(f"Push failed: {push.stderr.strip()}", style="red"))
            return

        result = subprocess.run(
            ["gh", "pr", "create", "--title", title, "--body", body],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            self.call_from_thread(self._add_message, Text(f"✓ {result.stdout.strip()}", style="green"))
        else:
            self.call_from_thread(self._add_message, Text(f"✗ {result.stderr.strip()}", style="red"))


if __name__ == "__main__":
    HydraApp().run()
