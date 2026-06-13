import shlex
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

from rich.console import Group
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Footer, Input, Static

from src.routing.claude_session import THINKING_END, THINKING_START
from src.routing.gateway import Gateway
from src.routing.session import SessionManager
from src.tools.compiler import Compiler
from src.tools.patcher import Patcher
from src.tools.scanner import Scanner

# How to launch the interactive login/auth flow for each underlying CLI.
LOGIN_COMMANDS = {
    "claude": ["claude", "auth", "login"],
    "codex": ["codex", "login"],
    "gemini": ["gemini"],  # first interactive launch walks through OAuth
}


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
    """

    BINDINGS = [("ctrl+q", "quit", "Quit")]

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
        self.effort_tier = None
        self.cli_override = None
        self.model_override = None
        self.mode = "yolo"
        self.usage_log = []
        self.session_id = self.sessions.new_session_id()

    def on_unmount(self) -> None:
        self.gateway.close()

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="history")
        yield Static(id="status")
        yield Input(placeholder="Type a message or /command...")
        yield Footer()

    def on_mount(self) -> None:
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
        self.query_one("#status", Static).update(
            f"cli={cli}  model={model}  effort={tier}  mode={self.mode}  session={self.session_id}"
        )

    def _add_message(self, renderable) -> Static:
        widget = Static(renderable)
        history = self.query_one("#history", VerticalScroll)
        history.mount(widget)
        history.scroll_end(animate=False)
        return widget

    def _render_history(self) -> None:
        history = self.query_one("#history", VerticalScroll)
        history.remove_children()
        for msg in self.history:
            if msg["role"] == "user":
                history.mount(Static(Text(f"\U0001f464 You: {msg['content']}")))
            elif msg["role"] == "assistant":
                history.mount(Static(Markdown(msg["content"])))
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
        cmd = LOGIN_COMMANDS.get(cli_name)
        if not cmd:
            self._add_message(Text(f"Unknown CLI: {cli_name}. Choose from: {', '.join(LOGIN_COMMANDS)}", style="red"))
            return

        self._add_message(Text(f"Launching '{' '.join(cmd)}'... complete the login, then return here.", style="yellow"))
        with self.suspend():
            try:
                subprocess.run(cmd, check=False)
            except FileNotFoundError:
                self._add_message(Text(f"'{cli_name}' CLI not found on PATH.", style="red"))
                return

        self.gateway.active_providers = self.gateway.scavenger.get_active_providers()
        self.gateway.cli_auth_status = self.gateway.scavenger.get_cli_auth_status()
        self.gateway.headers = self.gateway.scavenger.get_all_headers()
        self._add_message(Text(f"Refreshed credentials for '{cli_name}'.", style="green"))

    # -- input handling ------------------------------------------------

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
            
        self._add_message(Text(f"\U0001f464 You: {text}"))
        self._run_prompt(text)

    def _handle_command(self, user_input: str) -> None:
        cmd = user_input[1:].lower()

        if cmd == "exit":
            self.exit()
        elif cmd == "clear":
            self.history = [{"role": "system", "content": self.system_msg}]
            self.query_one("#history", VerticalScroll).remove_children()
        elif cmd.startswith("login"):
            parts = cmd.split(" ")
            if len(parts) != 2:
                self._add_message(Text(f"Usage: /login <{'|'.join(LOGIN_COMMANDS)}>", style="red"))
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
        else:
            self._add_message(Text(f"Unknown command: {cmd}", style="red"))

    # -- workers ---------------------------------------------------------

    def _render_streaming(self, label: str, thinking: str, response: str):
        """Builds the renderable for a streaming update.

        Thinking text (model reasoning) is shown dim/italic above the answer
        so the user can watch the model reason in real-time without it being
        confused with the final reply.
        """
        parts = [Text(label, style="bold magenta")]
        if thinking:
            parts.append(Text(thinking, style="dim italic"))
        if response:
            parts.append(Markdown(response))
        return Group(*parts)

    @work(thread=True, exclusive=True, group="prompt")
    def _run_prompt(self, prompt: str) -> None:
        current_prompt = prompt
        while True:
            widget = self.call_from_thread(self._add_message, Text("\U0001f916 Hydra:"))
            thinking = ""
            response = ""
            in_thinking = False
            try:
                for chunk in self.gateway.request_stream(
                    current_prompt,
                    tier=self.effort_tier,
                    history=self.history,
                    cli_override=self.cli_override,
                    model_override=self.model_override,
                    mode=self.mode,
                ):
                    if chunk == THINKING_START:
                        in_thinking = True
                        continue
                    if chunk == THINKING_END:
                        in_thinking = False
                        continue
                    if in_thinking:
                        thinking += chunk
                    else:
                        response += chunk
                    self.call_from_thread(
                        widget.update,
                        self._render_streaming("\U0001f916 Hydra:", thinking, response),
                    )
                    self.call_from_thread(self.query_one("#history", VerticalScroll).scroll_end, animate=False)
            except Exception as e:
                self.call_from_thread(widget.update, Text(f"Error: {e}", style="red"))
                return

            response = response.strip()
            usage = self.gateway.last_usage
            label = f"\U0001f916 Hydra ({usage['cli']}/{usage['model']}):" if usage else "\U0001f916 Hydra:"
            self.call_from_thread(widget.update, Group(Text(label, style="bold magenta"), Markdown(response)))

            self.history.append({"role": "user", "content": current_prompt})
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
            current_prompt = f"The build failed with the following error:\n```\n{output}\n```\nPlease fix the code."

    @work(thread=True, group="parallel")
    def _run_parallel(self, prompts: list[str]) -> None:
        def run_task(prompt: str):
            gw = Gateway()
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

                self.call_from_thread(
                    self._add_message,
                    Group(Text(f"\U0001f916 {prompt[:60]}", style="bold magenta"), Markdown(result)),
                )
                self.history.append({"role": "user", "content": prompt})
                self.history.append({"role": "assistant", "content": result})
                if usage:
                    self.usage_log.append(usage)

        self._save_session()


if __name__ == "__main__":
    HydraApp().run()
