import typer
from typing import Optional

from codehydra.tui import HydraApp

app = typer.Typer()


@app.command()
def chat(
    resume: Optional[str] = typer.Option(
        None,
        "--resume",
        "-r",
        metavar="SESSION_ID",
        help="Resume a previous session by ID, or omit the value to resume the most recent one.",
        is_flag=False,
        flag_value="",
    ),
):
    """Starts the interactive CodeHydra TUI."""
    HydraApp(resume=resume).run()


if __name__ == "__main__":
    app()
