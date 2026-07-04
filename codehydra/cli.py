import argparse
import sys

from codehydra.tui import HydraApp


def _pick_session():
    """Arrow-key session picker. Returns session ID or None."""
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
        curses.init_pair(1, curses.COLOR_CYAN, -1)
        curses.init_pair(2, curses.COLOR_WHITE, -1)
        idx = [0]
        while True:
            stdscr.clear()
            h, w = stdscr.getmaxyx()
            stdscr.addstr(0, 0, "Select session  (↑↓/jk move  Enter confirm  q cancel):", curses.A_BOLD)
            for i, s in enumerate(sessions[:h - 3]):
                marker = "▶ " if i == idx[0] else "  "
                label = f"{marker}{s['id']}  {s['preview'][:w - 34]}"
                attr = (curses.color_pair(1) | curses.A_BOLD) if i == idx[0] else curses.color_pair(2)
                stdscr.addstr(i + 2, 0, label[:w - 1], attr)
            stdscr.refresh()
            key = stdscr.getch()
            if key in (curses.KEY_UP, ord('k')) and idx[0] > 0:
                idx[0] -= 1
            elif key in (curses.KEY_DOWN, ord('j')) and idx[0] < len(sessions) - 1:
                idx[0] += 1
            elif key in (ord('\n'), ord('\r'), curses.KEY_ENTER):
                return sessions[idx[0]]["id"]
            elif key in (ord('q'), 27):
                return None

    return curses.wrapper(_ui)


def main():
    parser = argparse.ArgumentParser(
        prog="codehydra",
        description="CodeHydra — multi-CLI autonomous coding agent TUI",
    )
    parser.add_argument(
        "--resume", "-r",
        nargs="?",          # 0 or 1 args: bare flag → const; --resume id → id
        const="",           # value when flag appears without an argument
        default=None,       # value when flag is absent entirely
        metavar="SESSION_ID",
        help="Resume a session. Omit SESSION_ID to pick interactively.",
    )
    args = parser.parse_args()

    resume = args.resume
    if resume == "":          # bare --resume / -r → show picker
        resume = _pick_session()
        if resume is None:
            return

    HydraApp(resume=resume).run()


if __name__ == "__main__":
    main()
