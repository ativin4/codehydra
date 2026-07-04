import sys
import typer
from typing import Optional

from codehydra.tui import HydraApp

app = typer.Typer()


def _pick_session() -> Optional[str]:
    """Interactive arrow-key session picker. Returns session ID or None."""
    from codehydra.routing.session import SessionManager
    sessions = SessionManager().list_sessions()
    if not sessions:
        print("No saved sessions.", file=sys.stderr)
        return None

    import curses

    def _ui(stdscr):
        curses.curs_set(0)
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN, -1)   # selected
        curses.init_pair(2, curses.COLOR_WHITE, -1)  # normal
        curses.init_pair(3, curses.COLOR_BLACK, -1)  # dim

        idx = [0]
        while True:
            stdscr.clear()
            h, w = stdscr.getmaxyx()
            stdscr.addstr(0, 0, "Select a session (↑↓ to move, Enter to confirm, q to cancel):", curses.A_BOLD)
            for i, s in enumerate(sessions[:h - 3]):
                marker = "▶ " if i == idx[0] else "  "
                label = f"{marker}{s['id']}  {s['preview'][:w - 30]}"
                if i == idx[0]:
                    stdscr.addstr(i + 2, 0, label[:w - 1], curses.color_pair(1) | curses.A_BOLD)
                else:
                    stdscr.addstr(i + 2, 0, label[:w - 1], curses.color_pair(2))
            stdscr.refresh()

            key = stdscr.getch()
            if key in (curses.KEY_UP, ord('k')) and idx[0] > 0:
                idx[0] -= 1
            elif key in (curses.KEY_DOWN, ord('j')) and idx[0] < len(sessions) - 1:
                idx[0] += 1
            elif key in (ord('\n'), ord('\r'), curses.KEY_ENTER):
                return sessions[idx[0]]["id"]
            elif key in (ord('q'), 27):  # q or Esc
                return None

    return curses.wrapper(_ui)


@app.command()
def chat(
    resume: Optional[str] = typer.Option(
        None,
        "--resume",
        "-r",
        metavar="SESSION_ID",
        help="Resume a session. Pass an ID to resume directly, or omit the value to pick interactively.",
        is_flag=False,
        flag_value="",
    ),
):
    """Starts the interactive CodeHydra TUI."""
    # --resume with no value → show interactive picker
    if resume == "":
        resume = _pick_session()
        if resume is None:
            raise typer.Exit()
    HydraApp(resume=resume).run()


if __name__ == "__main__":
    app()
