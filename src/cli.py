import subprocess
import shlex
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import typer
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.markdown import Markdown
from rich.table import Table
from src.routing.gateway import Gateway
from src.routing.session import SessionManager
from src.tools.scanner import Scanner
from src.tools.patcher import Patcher
from src.tools.compiler import Compiler

app = typer.Typer()
console = Console()
gateway = Gateway()
scanner = Scanner()
patcher = Patcher()
compiler = Compiler()
sessions = SessionManager()

# How to launch the interactive login/auth flow for each underlying CLI.
LOGIN_COMMANDS = {
    "claude": ["claude", "auth", "login"],
    "codex": ["codex", "login"],
    "gemini": ["gemini"],  # first interactive launch walks through OAuth
}


def _login(cli_name: str) -> None:
    """Launches a CLI's interactive auth flow, inheriting the current terminal."""
    cmd = LOGIN_COMMANDS.get(cli_name)
    if not cmd:
        console.print(f"[red]Unknown CLI: {cli_name}. Choose from: {', '.join(LOGIN_COMMANDS)}[/red]")
        return

    console.print(f"[yellow]Launching '{' '.join(cmd)}'... complete the login, then exit back here.[/yellow]")
    try:
        subprocess.run(cmd, check=False)
    except FileNotFoundError:
        console.print(f"[red]'{cli_name}' CLI not found on PATH.[/red]")
        return

    # Refresh credential state now that login may have changed it.
    gateway.active_providers = gateway.scavenger.get_active_providers()
    gateway.headers = gateway.scavenger.get_all_headers()
    console.print(f"[green]Refreshed credentials for '{cli_name}'.[/green]")

@app.command()
def chat():
    """Starts the interactive CodeHydra REPL."""
    active = gateway.scavenger.get_active_providers()
    sub_info = f"Active Subscriptions: {', '.join(active)}" if active else "No active subscriptions found. Using environment API keys."

    auth_status = gateway.scavenger.get_cli_auth_status()
    unauthenticated = [cli for cli, ok in auth_status.items() if not ok]
    panel_body = f"[bold green]CodeHydra[/bold green] - BYOS Agent Active\n[dim]{sub_info}[/dim]"
    if unauthenticated:
        panel_body += f"\n[yellow]Not logged in: {', '.join(unauthenticated)} - run /login <cli> to authenticate[/yellow]"

    console.print(Panel(panel_body, subtitle="Type /exit to quit"))
    
    # Initialize with workspace context
    context = scanner.get_system_prompt_context()
    system_msg = (
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
    history = [{"role": "system", "content": system_msg}]
    effort_tier = None
    cli_override = None
    model_override = None
    usage_log = []
    session_id = sessions.new_session_id()

    while True:
        try:
            user_input = console.input("[bold blue]👤 You:[/bold blue] ").strip()
            # ... (commands logic same)
            if not user_input: continue
            if user_input.startswith("/"):
                # Handle commands (exit, clear, effort)
                cmd = user_input[1:].lower()
                if cmd == "exit": break
                elif cmd == "clear":
                    history = [{"role": "system", "content": system_msg}]
                    console.clear()
                    continue
                elif cmd.startswith("login"):
                    parts = cmd.split(" ")
                    if len(parts) != 2:
                        console.print(f"[red]Usage: /login <{'|'.join(LOGIN_COMMANDS)}>[/red]")
                    else:
                        _login(parts[1])
                    continue
                elif cmd.startswith("effort "):
                    tier = cmd.split(" ")[1]
                    if tier in ["low", "medium", "high"]:
                        effort_tier = tier
                        console.print(f"[yellow]Effort tier set to {effort_tier}[/yellow]")
                    else:
                        console.print(f"[red]Invalid tier: {tier}[/red]")
                    continue
                elif cmd.startswith("cli"):
                    parts = cmd.split(" ")
                    valid = ["auto", *Gateway.CLI_DEFAULT_MODELS]
                    if len(parts) != 2 or parts[1] not in valid:
                        console.print(f"[red]Usage: /cli <{'|'.join(valid)}>[/red]")
                    else:
                        cli_override = None if parts[1] == "auto" else parts[1]
                        console.print(f"[yellow]CLI backend set to {parts[1]}[/yellow]")
                    continue
                elif cmd.startswith("model"):
                    parts = user_input.split(" ", 1)  # preserve model name case
                    if len(parts) != 2:
                        console.print("[red]Usage: /model <name|auto>[/red]")
                    else:
                        model_override = None if parts[1].lower() == "auto" else parts[1]
                        console.print(f"[yellow]Model override set to {parts[1]}[/yellow]")
                    continue
                elif cmd == "sessions":
                    saved = sessions.list_sessions()
                    if not saved:
                        console.print("[dim]No saved sessions.[/dim]")
                    for s in saved:
                        marker = " (current)" if s["id"] == session_id else ""
                        console.print(f"  [cyan]{s['id']}[/cyan]{marker} - {s['preview']}")
                    continue
                elif cmd.startswith("resume"):
                    parts = cmd.split(" ")
                    target = parts[1] if len(parts) == 2 else sessions.latest_session_id(exclude=session_id)
                    data = sessions.load(target) if target else None
                    if not data:
                        console.print(f"[red]No session found: {target or '(none saved)'}[/red]")
                    else:
                        session_id = target
                        history = data.get("history", history)
                        effort_tier = data.get("effort_tier")
                        cli_override = data.get("cli_override")
                        model_override = data.get("model_override")
                        usage_log = data.get("usage_log", [])
                        console.print(f"[yellow]Resumed session {session_id}[/yellow]")
                    continue
                elif cmd == "cost":
                    if not usage_log:
                        console.print("[dim]No usage recorded yet this session.[/dim]")
                        continue
                    table = Table(title="Session Usage")
                    table.add_column("#", justify="right")
                    table.add_column("CLI")
                    table.add_column("Model")
                    table.add_column("Tier")
                    table.add_column("Tokens", justify="right")
                    for i, entry in enumerate(usage_log, 1):
                        tokens = entry.get("tokens")
                        table.add_row(
                            str(i),
                            entry.get("cli", "?"),
                            entry.get("model", "?"),
                            entry.get("tier", "?"),
                            str(tokens) if tokens is not None else "n/a",
                        )
                    console.print(table)
                    cli_counts = Counter(entry["cli"] for entry in usage_log)
                    breakdown = ", ".join(f"{cli}: {count}" for cli, count in cli_counts.items())
                    total_tokens = sum(entry.get("tokens") or 0 for entry in usage_log)
                    console.print(f"[dim]Turns by CLI: {breakdown}[/dim]")
                    console.print(f"[dim]Total tokens (where reported): {total_tokens}[/dim]")
                    console.print("[dim]Note: BYOS subscriptions are flat-rate; token counts are usage indicators, not billed cost.[/dim]")
                    continue
                elif cmd.startswith("parallel"):
                    try:
                        prompts = shlex.split(user_input[len("/parallel"):].strip())
                    except ValueError as e:
                        console.print(f"[red]Could not parse prompts: {e}[/red]")
                        continue
                    if len(prompts) < 2:
                        console.print('[red]Usage: /parallel "task one" "task two" ...[/red]')
                        continue

                    console.print(
                        f"[yellow]Running {len(prompts)} tasks in parallel "
                        f"(each spawns its own CLI process; agentic edits to the same "
                        f"files may conflict)...[/yellow]"
                    )

                    def run_task(prompt):
                        gw = Gateway()
                        result = gw.request(
                            prompt,
                            tier=effort_tier,
                            history=history,
                            cli_override=cli_override,
                            model_override=model_override,
                        )
                        return result, gw.last_usage

                    with ThreadPoolExecutor(max_workers=len(prompts)) as executor:
                        futures = {executor.submit(run_task, p): p for p in prompts}
                        for future in as_completed(futures):
                            prompt = futures[future]
                            try:
                                result, usage = future.result()
                            except Exception as e:
                                console.print(Panel(f"[red]{e}[/red]", title=f"❌ {prompt[:60]}"))
                                continue
                            console.print(Panel(Markdown(result), title=f"\U0001f916 {prompt[:60]}"))
                            history.append({"role": "user", "content": prompt})
                            history.append({"role": "assistant", "content": result})
                            if usage:
                                usage_log.append(usage)

                    sessions.save(session_id, {
                        "history": history,
                        "effort_tier": effort_tier,
                        "cli_override": cli_override,
                        "model_override": model_override,
                        "usage_log": usage_log,
                    })
                    continue
                else:
                    console.print(f"[red]Unknown command: {cmd}[/red]")
                    continue

            # Standard chat interaction
            current_prompt = user_input
            while True: # Self-healing loop
                try:
                    console.print("\n[bold magenta]🤖 Hydra:[/bold magenta]")
                    response = ""
                    with Live(Markdown(""), console=console, refresh_per_second=10) as live:
                        for chunk in gateway.request_stream(
                            current_prompt,
                            tier=effort_tier,
                            history=history,
                            cli_override=cli_override,
                            model_override=model_override,
                        ):
                            response += chunk
                            live.update(Markdown(response))
                    response = response.strip()

                    history.append({"role": "user", "content": current_prompt})
                    history.append({"role": "assistant", "content": response})
                    if gateway.last_usage:
                        usage_log.append(gateway.last_usage)
                    sessions.save(session_id, {
                        "history": history,
                        "effort_tier": effort_tier,
                        "cli_override": cli_override,
                        "model_override": model_override,
                        "usage_log": usage_log,
                    })

                    # Check for patches
                    patched_files = patcher.apply_all_patches(response)
                    if patched_files:
                        console.print(f"[green]Applied patches to: {', '.join(patched_files)}[/green]")

                        # Run build
                        exit_code, output = compiler.run_build()
                        if exit_code == 0:
                            console.print("[bold green]✅ Build successful![/bold green]")
                            break # Done with this prompt
                        else:
                            console.print(f"[bold red]❌ Build failed (Exit {exit_code}). Feed back to Hydra...[/bold red]")
                            current_prompt = f"The build failed with the following error:\n```\n{output}\n```\nPlease fix the code."
                            continue # Iterate
                    else:
                        break # No patches, just a normal chat
                except Exception as e:
                    console.print(f"[red]Error: {e}[/red]")
                    break

        except KeyboardInterrupt: break
        except EOFError: break

    console.print("[yellow]Goodbye![/yellow]")

if __name__ == "__main__":
    app()
