import typer

from src.tui import HydraApp

app = typer.Typer()


@app.command()
def chat():
    """Starts the interactive CodeHydra TUI."""
    HydraApp().run()


if __name__ == "__main__":
    app()
