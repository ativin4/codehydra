#!/usr/bin/env python3
"""
Automated CodeHydra demo recorder.

Records a screen video of the TUI running in Chrome (via textual-serve),
drives the interaction with CDP, then post-processes with ffmpeg.

Usage:
    textual-serve codehydra.tui:HydraApp &   # if not already running on :8900
    python make_demo.py
"""
import asyncio
import base64
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request

try:
    import websockets
except ImportError:
    sys.exit("pip install websockets")

DEMO_PROMPT = (
    "Create a Python CLI todo list app in todo.py. "
    "Commands: add <task>, list, done <id>, remove <id>. "
    "Persist to todos.json."
)
OUT_MOV = "assets/demo_raw.mov"
OUT_MP4 = "assets/demo.mp4"
TUI_URL = "http://localhost:8900"
CDP_URL = "http://localhost:9222"


def chrome_window_bounds() -> tuple[int, int, int, int]:
    """Return (x, y, width, height) of the front Chrome window."""
    raw = subprocess.run(
        ["osascript", "-e", 'tell application "Google Chrome" to get bounds of front window'],
        capture_output=True, text=True
    ).stdout.strip()
    x1, y1, x2, y2 = [int(v.strip()) for v in raw.split(",")]
    return x1, y1, x2 - x1, y2 - y1


async def run():
    os.makedirs("assets", exist_ok=True)

    # --- find first page ---
    pages = json.loads(urllib.request.urlopen(f"{CDP_URL}/json").read())
    page = next(
        (p for p in pages if p.get("type") == "page" and "localhost:8900" in p.get("url", "")),
        pages[0]
    )
    ws_url = page["webSocketDebuggerUrl"]
    print(f"Using page: {page.get('url', '?')[:60]}")

    # --- get Chrome window position ---
    x, y, w, h = chrome_window_bounds()
    print(f"Chrome window: {w}×{h} at ({x},{y})")

    # --- start screencapture ---
    print(f"Starting screencapture → {OUT_MOV}")
    rec = subprocess.Popen(
        ["screencapture", "-v", "-R", f"{x},{y},{w},{h}", OUT_MOV],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    await asyncio.sleep(2.5)  # recording warm-up

    async with websockets.connect(ws_url, max_size=20 * 1024 * 1024) as ws:
        _id = 0

        async def send(method, params={}):
            nonlocal _id
            _id += 1
            payload = json.dumps({"id": _id, "method": method, "params": params})
            await ws.send(payload)
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), 30))
                if msg.get("id") == _id:
                    return msg

        # Navigate to fresh TUI session
        await send("Page.navigate", {"url": TUI_URL})
        print("Waiting for TUI to load…")
        await asyncio.sleep(6)

        # --- click into the text input ---
        # Input box is at the bottom of the terminal (~755px from top for 820px tall Chrome)
        input_y = int(h * 0.9)
        await send("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": w // 2, "y": input_y,
            "button": "left", "clickCount": 1
        })
        await send("Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": w // 2, "y": input_y, "button": "left"
        })
        await asyncio.sleep(0.8)

        # --- type prompt at human speed ---
        print(f"Typing: {DEMO_PROMPT[:50]}…")
        for ch in DEMO_PROMPT:
            await send("Input.dispatchKeyEvent", {"type": "char", "text": ch})
            await asyncio.sleep(0.035)  # ~28 chars/sec

        await asyncio.sleep(1.2)

        # --- submit ---
        await send("Input.dispatchKeyEvent", {
            "type": "keyDown", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13
        })
        await send("Input.dispatchKeyEvent", {
            "type": "keyUp", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13
        })
        print("Prompt submitted — waiting for response…")

        # --- poll until thinking disappears ---
        for i in range(40):
            await asyncio.sleep(3)
            r = await send("Runtime.evaluate", {"expression": "document.body.innerText"})
            text = str(r.get("result", {}).get("value", ""))
            still_thinking = "thinking" in text.lower()
            print(f"  t={i*3+3}s  thinking={still_thinking}")
            if not still_thinking and i >= 3:
                break

        await asyncio.sleep(4)  # hold on streamed response

        # --- type /status ---
        await send("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": w // 2, "y": input_y,
            "button": "left", "clickCount": 1
        })
        await send("Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": w // 2, "y": input_y, "button": "left"
        })
        await asyncio.sleep(0.3)
        for ch in "/status":
            await send("Input.dispatchKeyEvent", {"type": "char", "text": ch})
            await asyncio.sleep(0.08)
        await asyncio.sleep(0.5)
        await send("Input.dispatchKeyEvent", {
            "type": "keyDown", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13
        })
        await send("Input.dispatchKeyEvent", {
            "type": "keyUp", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13
        })
        await asyncio.sleep(4)

    # --- stop recording ---
    print("Stopping screencapture…")
    rec.send_signal(signal.SIGINT)
    await asyncio.sleep(3)

    if not os.path.exists(OUT_MOV):
        print(f"ERROR: {OUT_MOV} not created. Check screencapture permissions.")
        return

    # --- ffmpeg post-process ---
    print(f"Processing with ffmpeg → {OUT_MP4}")
    subprocess.run([
        "ffmpeg", "-y",
        "-i", OUT_MOV,
        # Scale to 1280 wide (keep aspect), sharpen, boost contrast slightly
        "-vf", "scale=1280:-2:flags=lanczos,unsharp=3:3:0.5:3:3:0",
        "-c:v", "libx264",
        "-preset", "slow",
        "-crf", "18",
        "-movflags", "+faststart",
        "-pix_fmt", "yuv420p",
        OUT_MP4
    ], check=True)

    size_mb = os.path.getsize(OUT_MP4) / 1024 / 1024
    print(f"\n✓ Done: {OUT_MP4} ({size_mb:.1f} MB)")
    print("  Upload directly to LinkedIn post for autoplay (no audio needed).")


if __name__ == "__main__":
    asyncio.run(run())
