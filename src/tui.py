import re
import shlex
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.suggester import Suggester
from textual.widgets import Footer, Input, Markdown, Static

from src.routing.claude_session import THINKING_END, THINKING_START
from src.routing.gateway import Gateway
from src.routing.oss_provider import OllamaProvider
from src.routing.session import SessionManager
from src.tools.compiler import Compiler
from src.tools.media import MEDIA_EXTS, image_to_base64, pdf_to_text
from src.tools.patcher import Patcher
from src.tools.scanner import Scanner
from src.tools.websearch import fetch_url, format_results, search


SKILLS_DIR = Path(".codehydra/skills")
MEMORY_FILE = Path(".codehydra/memory.md")

SLASH_COMMANDS = [
    "/effort low", "/effort medium", "/effort high",
    "/cli auto", "/cli claude", "/cli gemini", "/cli codex", "/cli ollama",
    "/model auto",
    "/mode plan", "/mode yolo",
    "/login claude", "/login gemini", "/login codex", "/login ollama",
    "/parallel",
    "/compare",
    "/skills",
    "/sessions", "/resume",
    "/cost", "/clear", "/compact", "/exit",
    "/memory", "/remember", "/forget",
    "/search",
]


def _skill_names() -> list[str]:
    if not SKILLS_DIR.is_dir():
        return []
    return [p.stem for p in SKILLS_DIR.glob("*.md")]


class SlashSuggester(Suggester):
    """Suggests slash commands and installed skill names when input starts with '/'."""

    async def get_suggestion(self, value: str) -> str | None:
        if not value.startswith("/"):
            return None
        lower = value.lower()
        candidates = SLASH_COMMANDS + [f"/skill {n}" for n in _skill_names()]
        for cmd in candidates:
            if cmd.startswith(lower) and cmd != lower:
                return cmd
        return None


class HydraApp(App):
    """Claude Code-style TUI: scrollable history with a pinned input box."""

    CSS = """
    #history {
        height: 1fr;
        padding: 0 1;
    }
    #status {
        height: 1;
        background: $boost;
        color: $text-muted;
        padding: 0 1;
    }
    .thinking {
        color: $text-muted;
        text-style: italic;
        padding: 0 1;
    }
    Markdown {
        padding: 0 1;
    }
    """

    BINDINGS = [("ctrl+c", "quit", "Quit")]

    def __init__(self):
        super().__init__()
        self.gateway = Gateway()
        self.scanner = Scanner()
        self.patcher = Patcher()
        self.compiler = Compiler()
        self.sessions = SessionManager()

        context = self.scanner.get_system_prompt_context()
        self.system_msg = (
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
        self.history = [{"role": "system", "content": self.system_msg}]
        self._refresh_system_msg()
        self.effort_tier = None
        self.cli_override = None
        self.model_override = None
        self.mode = "yolo"
        self.usage_log = []
        self.session_id = self.sessions.new_session_id()

    def on_unmount(self) -> None:
        self._save_session()
        self.gateway.close()

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="history")
        yield Static(id="status")
        yield Input(placeholder="Type a message or /command...", suggester=SlashSuggester())
        yield Footer()

    def on_mount(self) -> None:
        self._history_scroll = self.query_one("#history", VerticalScroll)
        auth_status = self.gateway.cli_auth_status
        ready = [cli for cli, ok in auth_status.items() if ok]
        sub_info = (
            f"Ready CLIs: {', '.join(ready)}"
            if ready else "No CLIs are logged in. Run /login <cli> to authenticate."
        )
        unauthenticated = [cli for cli, ok in auth_status.items() if not ok]
        lines = [Text("CodeHydra - BYOS Agent Active", style="bold green"), Text(sub_info)]
        if unauthenticated:
            lines.append(Text(
                f"Not logged in: {', '.join(unauthenticated)} - run /login <cli> to authenticate",
                style="yellow",
            ))
        self._add_message(Group(*lines))
        self._update_status()
        self.query_one(Input).focus()

    # -- helpers -----------------------------------------------------

    def _update_status(self) -> None:
        cli = self.cli_override or "auto"
        model = self.model_override or "auto"
        tier = self.effort_tier or "auto"
        rl_parts = []
        for name in ("claude", "gemini", "codex", "ollama"):
            secs = self.gateway.rate_limit_resets_in(name)
            if secs is not None:
                rl_parts.append(f"{name}⏳{secs}s")
        rl_str = f"  rate-limited: {' '.join(rl_parts)}" if rl_parts else ""
        self.query_one("#status", Static).update(
            f"cli={cli}  model={model}  effort={tier}  mode={self.mode}  session={self.session_id}{rl_str}"
        )

    def _refresh_system_msg(self) -> None:
        """Rebuild history[0] with current memory file content prepended."""
        memory = MEMORY_FILE.read_text().strip() if MEMORY_FILE.exists() else ""
        if memory:
            full = f"## Project Memory\n{memory}\n\n---\n\n{self.system_msg}"
        else:
            full = self.system_msg
        self.history[0] = {"role": "system", "content": full}

    def _add_message(self, renderable) -> Static:
        widget = Static(renderable)
        history = self._history_scroll
        history.mount(widget)
        history.scroll_end(animate=False)
        return widget

    def _add_response_turn(self):
        """Mounts (thinking_static, markdown_widget) for a streaming response.
        Both are returned so the worker thread can update them incrementally.
        Markdown widget supports mouse selection; thinking stays as Static (dim).
        """
        history = self._history_scroll
        thinking = Static("", classes="thinking")
        md = Markdown("")
        history.mount(thinking)
        history.mount(md)
        history.scroll_end(animate=False)
        return thinking, md

    def _render_history(self) -> None:
        history = self._history_scroll
        history.remove_children()
        for msg in self.history:
            if msg["role"] == "user":
                history.mount(Static(Text(f"> {msg['content']}", style="dim")))
            elif msg["role"] == "assistant":
                history.mount(Markdown(msg["content"]))
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
        if cli_name == "ollama":
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

    # -- input handling ------------------------------------------------

    _AT_FILE_RE = re.compile(r"@(https?://[^\s]+|[\w./\-]+)")

    def _expand_file_refs(self, text: str) -> tuple[str, list[str], list[Path]]:
        """Replaces @path / @url tokens with content inline.
        Media files (images/PDFs/videos) are stripped from the text and
        returned separately as Path objects for per-CLI handling in the gateway.
        Returns (expanded_text, injected_labels, media_files)."""
        injected: list[str] = []
        media_files: list[Path] = []

        def replace(m: re.Match) -> str:
            token = m.group(1)
            if token.startswith("http://") or token.startswith("https://"):
                try:
                    content = fetch_url(token)
                    injected.append(token)
                    return f"\n\n[URL: {token}]\n{content}\n"
                except Exception:
                    return m.group(0)
            path = Path(token)
            if not path.exists():
                path = Path.cwd() / token
            if not path.is_file():
                return m.group(0)
            if path.suffix.lower() in MEDIA_EXTS:
                media_files.append(path)
                injected.append(token)
                return ""  # removed from text; gateway injects per-CLI
            try:
                content = path.read_text(errors="replace")
                injected.append(token)
                return f"\n\n[File: {token}]\n```\n{content}\n```\n"
            except Exception:
                return m.group(0)

        expanded = self._AT_FILE_RE.sub(replace, text)
        return expanded, injected, media_files

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        self.query_one(Input).value = ""
        if not text:
            return

        if text.lower() in ("exit", "quit"):
            self.exit()
            return

        if text.startswith("/"):
            self._handle_command(text)
            return

        expanded, injected, media_files = self._expand_file_refs(text)
        self._add_message(Text(f"> {text}", style="dim"))
        if injected:
            self._add_message(Text(f"  @{' @'.join(injected)}", style="dim cyan"))
        self._run_prompt(expanded, media_files)

    def _handle_command(self, user_input: str) -> None:
        cmd = user_input[1:].lower()

        if cmd == "exit":
            self.exit()
        elif cmd == "clear":
            self.history = [{"role": "system", "content": self.system_msg}]
            self._history_scroll.remove_children()
        elif cmd == "compact":
            self._run_compact()
        elif cmd.startswith("login"):
            parts = cmd.split(" ")
            valid_logins = list(Gateway.LOGIN_COMMANDS) + ["ollama"]
            if len(parts) != 2:
                self._add_message(Text(f"Usage: /login <{'|'.join(valid_logins)}>", style="red"))
            else:
                self._login(parts[1])
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
            if len(parts) != 2 or parts[1] not in ("plan", "yolo"):
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
                self.mode = data.get("mode", "yolo")
                self.usage_log = data.get("usage_log", [])
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
        else:
            self._add_message(Text(f"Unknown command: {cmd}", style="red"))

    # -- workers ---------------------------------------------------------

    @work(thread=True, exclusive=True, group="prompt")
    def _run_prompt(self, prompt: str, media_files: list[Path] | None = None) -> None:
        self.call_from_thread(self._refresh_system_msg)
        turns = [m for m in self.history if m["role"] != "system"]
        history_chars = sum(len(m["content"]) for m in turns)
        if history_chars > self._AUTO_COMPACT_CHARS:
            self.call_from_thread(self._add_message, Text("↩ Auto-compacting history…", style="dim yellow"))
            try:
                msg = self._do_compact()
                if msg:
                    self.call_from_thread(self._add_message, Text(f"↩ {msg}", style="dim yellow"))
            except Exception as e:
                self.call_from_thread(self._add_message, Text(f"Auto-compact failed: {e}", style="red"))
        while True:
            thinking_w, md_w = self.call_from_thread(self._add_response_turn)
            thinking = ""
            response = ""
            in_thinking = False
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
                    if chunk == THINKING_START:
                        in_thinking = True
                        continue
                    if chunk == THINKING_END:
                        in_thinking = False
                        continue
                    if in_thinking:
                        thinking += chunk
                        self.call_from_thread(thinking_w.update, thinking)
                    else:
                        response += chunk
                        self.call_from_thread(md_w.update, response)
                    self.call_from_thread(self._history_scroll.scroll_end, animate=False)
            except Exception as e:
                self.call_from_thread(thinking_w.remove)
                self.call_from_thread(md_w.update, f"**Error:** {e}")
                return

            response = response.strip()
            usage = self.gateway.last_usage
            if usage:
                tag = f"[{usage['cli']}/{usage['model'].split('/')[-1]}]"
                self.call_from_thread(self._add_message, Text(tag, style="dim"))
            self.call_from_thread(self._update_status)

            self.history.append({"role": "user", "content": prompt})
            self.history.append({"role": "assistant", "content": response})
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

            self.call_from_thread(self._add_message, Text(f"❌ Build failed (exit {exit_code}). Feeding back to Hydra...", style="bold red"))
            prompt = f"The build failed with the following error:\n```\n{output}\n```\nPlease fix the code."

    # Auto-compact when non-system history exceeds this many characters.
    _AUTO_COMPACT_CHARS = 60_000

    def _do_compact(self) -> str | None:
        """Compact history in-place. Returns a status string, or None if nothing to compact.
        Must be called from a worker thread (calls gateway.request which blocks)."""
        turns = [m for m in self.history if m["role"] != "system"]
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
        summary = self.gateway.request(
            summary_prompt,
            tier="low",
            history=[],
            cli_override=self.cli_override,
            mode="plan",
        )
        system = [m for m in self.history if m["role"] == "system"]
        self.history = system + [
            {"role": "assistant", "content": f"[Compacted history — {len(to_summarize)//2} earlier turns]\n{summary.strip()}"}
        ] + recent
        self._save_session()
        return f"Compacted {len(to_summarize)//2} turns → summary + {len(recent)//2} recent turns kept"

    @work(thread=True, exclusive=True, group="prompt")
    def _run_compact(self) -> None:
        turns = [m for m in self.history if m["role"] != "system"]
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

    @work(thread=True, group="search")
    def _run_search(self, query: str) -> None:
        self.call_from_thread(self._add_message, Text(f"Searching: {query}…", style="dim yellow"))
        try:
            results = search(query)
            formatted = format_results(results)
        except Exception as e:
            self.call_from_thread(self._add_message, Text(f"Search failed: {e}", style="red"))
            return
        self.call_from_thread(self._history_scroll.mount, Markdown(formatted))
        self.call_from_thread(self._history_scroll.scroll_end, animate=False)
        # Inject results as context so the next prompt can reference them.
        self.history.append({
            "role": "assistant",
            "content": f"[Web search results for: {query}]\n{formatted}",
        })

    @work(thread=True, group="parallel")
    def _run_parallel(self, prompts: list[str]) -> None:
        parent_auth = self.gateway.cli_auth_status

        def run_task(prompt: str):
            gw = Gateway()
            gw.cli_auth_status = parent_auth  # skip redundant CLI auth subprocesses
            result = gw.request(
                prompt,
                tier=self.effort_tier,
                history=self.history,
                cli_override=self.cli_override,
                model_override=self.model_override,
                mode=self.mode,
            )
            return result, gw.last_usage

        with ThreadPoolExecutor(max_workers=len(prompts)) as executor:
            futures = {executor.submit(run_task, p): p for p in prompts}
            for future in as_completed(futures):
                prompt = futures[future]
                try:
                    result, usage = future.result()
                except Exception as e:
                    self.call_from_thread(self._add_message, Text(f"❌ {prompt[:60]}: {e}", style="red"))
                    continue

                tag = f"[{usage['cli']}/{usage['model'].split('/')[-1]}]" if usage else ""
                self.call_from_thread(self._add_message, Text(f"> {prompt[:60]}", style="dim"))
                md = Markdown(result)
                self.call_from_thread(self._history_scroll.mount, md)
                if tag:
                    self.call_from_thread(self._add_message, Text(tag, style="dim"))
                self.history.append({"role": "user", "content": prompt})
                self.history.append({"role": "assistant", "content": result})
                if usage:
                    self.usage_log.append(usage)

        self._save_session()

    @work(thread=True, group="compare")
    def _run_compare(self, prompt: str, cli_names: list[str]) -> None:
        """Sends the same prompt to every active CLI in parallel and renders
        each response as a labelled block so the user can compare answers."""
        parent_auth = self.gateway.cli_auth_status

        def run_one(cli: str):
            gw = Gateway()
            gw.cli_auth_status = parent_auth
            t0 = time.monotonic()
            result = gw.request(
                prompt,
                tier=self.effort_tier,
                history=self.history,
                cli_override=cli,
                mode=self.mode,
            )
            elapsed = time.monotonic() - t0
            return result, gw.last_usage, elapsed

        with ThreadPoolExecutor(max_workers=len(cli_names)) as executor:
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

                model_tag = usage["model"].split("/")[-1] if usage else cli
                header = Text(f"── {cli} / {model_tag}  ({elapsed:.1f}s) ──", style="bold cyan")
                self.call_from_thread(self._add_message, header)
                md = Markdown(result)
                self.call_from_thread(self._history_scroll.mount, md)
                if usage:
                    self.usage_log.append(usage)

        self._save_session()


if __name__ == "__main__":
    HydraApp().run()
