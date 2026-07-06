#!/usr/bin/env python3
"""PTY driver for the scripted CodeHydra demo.

Spawns `codehydra` in a fresh throwaway git repo with the DemoGateway
script active, feeds a choreographed keystroke sequence, and mirrors the
TUI's output to stdout — so wrapping this in `asciinema rec --command`
records a clean, deterministic demo with no live API calls.

Usage (normally via record_demo.sh):
    python demo_driver.py
"""

import fcntl
import os
import pty
import select
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent
SCRIPT = REPO / "assets" / "demo_script.json"
CODEHYDRA = shutil.which("codehydra") or str(REPO / ".venv" / "bin" / "codehydra")
COLS, ROWS = 100, 32

ENTER = "\r"
TAB = "\t"
DOWN = "\x1b[B"


def _type(fd: int, text: str, delay: float = 0.05) -> None:
    for ch in text:
        os.write(fd, ch.encode())
        time.sleep(delay)


def _keys(fd: int, seq: str, delay: float = 0.35) -> None:
    os.write(fd, seq.encode())
    time.sleep(delay)


def choreography(fd: int, demo_dir: Path, child_pid: int) -> None:
    time.sleep(4)  # header + status bar settle

    # 1. Slash autocomplete: /eff → popup → Down ×2 → Tab → "/effort high"
    _type(fd, "/eff", delay=0.14)
    time.sleep(1.4)
    _keys(fd, DOWN, 0.5)
    _keys(fd, DOWN, 0.5)
    _keys(fd, TAB, 0.8)
    _keys(fd, ENTER, 1.5)

    # 2. First prompt: thinking spinner → stream → diff view (new fib.py)
    _type(fd, "Add a memoized fibonacci function to fib.py with a small CLI.", delay=0.028)
    time.sleep(0.6)
    _keys(fd, ENTER, 0.3)
    time.sleep(14)  # canned thinking + stream + diff render

    # Track fib.py so the next turn's edit renders as a modification diff.
    subprocess.run(["git", "add", "-A"], cwd=demo_dir, check=False,
                   capture_output=True)
    subprocess.run(["git", "commit", "-qm", "add fib.py"], cwd=demo_dir,
                   check=False, capture_output=True)

    # 3. Second prompt: mid-stream fallback notice + reset → codex finishes
    _type(fd, "Handle negative inputs gracefully.", delay=0.03)
    time.sleep(0.5)
    _keys(fd, ENTER, 0.3)
    time.sleep(14)

    # 4. /cost usage table
    _type(fd, "/cost", delay=0.1)
    time.sleep(0.4)
    _keys(fd, ENTER, 0.3)
    time.sleep(5)

    # 5. Hold on the status bar, then exit
    _type(fd, "/exit", delay=0.1)
    time.sleep(0.4)
    _keys(fd, ENTER, 0.3)
    time.sleep(2)


def main() -> None:
    demo_dir = Path(tempfile.mkdtemp(prefix="codehydra_demo_"))
    subprocess.run(["git", "init", "-q"], cwd=demo_dir, check=True)
    (demo_dir / ".gitignore").write_text(".codehydra/\n")
    subprocess.run(["git", "-C", str(demo_dir), "add", ".gitignore"],
                   check=False, capture_output=True)
    subprocess.run(["git", "-C", str(demo_dir), "commit", "-qm", "init"],
                   check=False, capture_output=True)

    env = dict(os.environ)
    env["CODEHYDRA_DEMO_SCRIPT"] = str(SCRIPT)
    env["CODEHYDRA_ENABLE_SUBAGENTS"] = "0"
    env.setdefault("TERM", "xterm-256color")

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
    proc = subprocess.Popen(
        [CODEHYDRA],
        stdin=slave, stdout=slave, stderr=slave,
        cwd=demo_dir, env=env, close_fds=True, start_new_session=True,
    )
    os.close(slave)

    driver = threading.Thread(
        target=choreography, args=(master, demo_dir, proc.pid), daemon=True
    )
    driver.start()

    # Mirror TUI output to our stdout (which asciinema records).
    try:
        while proc.poll() is None:
            r, _, _ = select.select([master], [], [], 0.2)
            if master in r:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    break
                if not data:
                    break
                os.write(sys.stdout.fileno(), data)
    finally:
        proc.wait(timeout=10)
        os.close(master)
        shutil.rmtree(demo_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
