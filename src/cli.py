import typer
from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown
from src.routing.gateway import Gateway
from src.tools.scanner import Scanner
from src.tools.patcher import Patcher
from src.tools.compiler import Compiler

app = typer.Typer()
console = Console()
gateway = Gateway()
scanner = Scanner()
patcher = Patcher()
compiler = Compiler()

@app.command()
def chat():
    """Starts the interactive CodeHydra REPL."""
    active = gateway.scavenger.get_active_providers()
    sub_info = f"Active Subscriptions: {', '.join(active)}" if active else "No active subscriptions found. Using environment API keys."
    console.print(Panel(
        f"[bold green]CodeHydra[/bold green] - BYOS Agent Active\n[dim]{sub_info}[/dim]", 
        subtitle="Type /exit to quit"
    ))
    
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
                elif cmd.startswith("effort "):
                    tier = cmd.split(" ")[1]
                    if tier in ["low", "medium", "high"]:
                        effort_tier = tier
                        console.print(f"[yellow]Effort tier set to {effort_tier}[/yellow]")
                    else:
                        console.print(f"[red]Invalid tier: {tier}[/red]")
                    continue
                else:
                    console.print(f"[red]Unknown command: {cmd}[/red]")
                    continue

            # Standard chat interaction
            current_prompt = user_input
            while True: # Self-healing loop
                with console.status("[bold green]Hydra is working...[/bold green]"):
                    try:
                        response = gateway.request(current_prompt, tier=effort_tier, history=history)
                        history.append({"role": "user", "content": current_prompt})
                        history.append({"role": "assistant", "content": response})
                        
                        console.print("\n[bold magenta]🤖 Hydra:[/bold magenta]")
                        console.print(Markdown(response))
                        
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
