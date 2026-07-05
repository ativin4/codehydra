#!/usr/bin/env python3
"""
Automated CodeHydra demo recorder (v4).

Uses the EXISTING TUI tab (no navigation = no xterm.js reconnect race).
Captures frames via CDP at ~10 fps, assembles with ffmpeg.

Usage:
    # 1. Open Chrome with --remote-debugging-port=9222
    # 2. Navigate a tab to http://localhost:8900 (textual-serve)
    # 3. python make_demo.py
"""
import asyncio, base64, json, os, subprocess, sys, time, urllib.request
try:
    import websockets
except ImportError:
    sys.exit("pip install websockets")

# ── config ──────────────────────────────────────────────────────────────────
CDP_URL      = "http://localhost:9222"
FRAMES       = "/tmp/codehydra_demo_frames"
OUT_MP4      = "assets/demo.mp4"
FPS          = 10
VIEWPORT_W   = 1400
VIEWPORT_H   = 860
INPUT_Y      = 787   # CSS px — center of textarea in a 860px-tall viewport
INPUT_X      = 700
CLAUDE_WAIT  = 55    # fixed seconds to wait for claude (no flaky detection)
CODEX_WAIT   = 35
# ────────────────────────────────────────────────────────────────────────────

_gid     = 0
_pending: dict[int, asyncio.Future] = {}
_stop    = asyncio.Event()
frame_n  = 0


async def recv_loop(ws):
    async for raw in ws:
        msg = json.loads(raw)
        fid = msg.get("id")
        if fid is not None:
            fut = _pending.pop(fid, None)
            if fut and not fut.done():
                fut.set_result(msg)


async def cdp(ws, method, params={}, timeout=20):
    global _gid
    _gid += 1
    _id = _gid
    fut = asyncio.get_running_loop().create_future()
    _pending[_id] = fut
    await ws.send(json.dumps({"id": _id, "method": method, "params": params}))
    return await asyncio.wait_for(fut, timeout)


async def recorder(ws):
    global frame_n
    os.makedirs(FRAMES, exist_ok=True)
    for f in os.scandir(FRAMES):
        os.unlink(f.path)
    while not _stop.is_set():
        t0 = time.monotonic()
        try:
            r    = await cdp(ws, "Page.captureScreenshot", {"format": "jpeg", "quality": 90}, timeout=4)
            data = base64.b64decode(r["result"]["data"])
            with open(f"{FRAMES}/f{frame_n:06d}.jpg", "wb") as fh:
                fh.write(data)
            frame_n += 1
        except Exception:
            pass
        await asyncio.sleep(max(0.02, 1/FPS - (time.monotonic()-t0)))
    print(f"  [rec] {frame_n} frames")


async def driver(ws):
    async def click():
        for ev in ("mousePressed", "mouseReleased"):
            await cdp(ws, "Input.dispatchMouseEvent", {
                "type": ev, "x": INPUT_X, "y": INPUT_Y,
                "button": "left", **({"clickCount": 1} if ev == "mousePressed" else {})
            })
        await asyncio.sleep(0.4)

    async def key(ch):
        await cdp(ws, "Input.dispatchKeyEvent", {"type": "char", "text": ch})

    async def enter():
        for ev in ("keyDown", "keyUp"):
            await cdp(ws, "Input.dispatchKeyEvent", {
                "type": ev, "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13
            })

    async def send_cmd(text, speed=0.06):
        await click()
        for ch in text:
            await key(ch)
            await asyncio.sleep(speed)
        await asyncio.sleep(0.5)
        await enter()
        await asyncio.sleep(1.2)

    # ── brief pause so recorder captures a clean "before" state ──
    await asyncio.sleep(3)

    # ── clear any prior history ──
    print("[drv] /clear")
    await send_cmd("/clear", speed=0.08)
    await asyncio.sleep(1.5)

    # ── show effort + cli commands ──
    print("[drv] /effort high")
    await send_cmd("/effort high", speed=0.08)
    await asyncio.sleep(1)

    print("[drv] /cli claude")
    await send_cmd("/cli claude", speed=0.08)
    await asyncio.sleep(1.5)

    # ── main build prompt ──
    prompt = (
        "Create todo.py — a Python CLI todo app. "
        "Commands: add <task>, list, done <id>, remove <id>. "
        "Persist tasks to todos.json."
    )
    print(f"[drv] prompt → claude")
    await send_cmd(prompt, speed=0.032)
    await asyncio.sleep(3)  # let thinking indicator appear

    print(f"[drv] waiting {CLAUDE_WAIT}s for claude…")
    await asyncio.sleep(CLAUDE_WAIT)
    await asyncio.sleep(5)  # hold on response + diff

    # ── switch CLI ──
    print("[drv] /cli codex")
    await send_cmd("/cli codex", speed=0.08)
    await asyncio.sleep(1.5)

    # ── follow-up ──
    followup = "Add type hints to all functions in todo.py."
    print(f"[drv] follow-up → codex")
    await send_cmd(followup, speed=0.045)
    await asyncio.sleep(3)

    print(f"[drv] waiting {CODEX_WAIT}s for codex…")
    await asyncio.sleep(CODEX_WAIT)
    await asyncio.sleep(4)

    # ── /status ──
    print("[drv] /status")
    await send_cmd("/status", speed=0.09)
    await asyncio.sleep(6)

    _stop.set()


async def main():
    pages   = json.loads(urllib.request.urlopen(f"{CDP_URL}/json").read())
    page    = next(
        (p for p in pages if p.get("type") == "page" and "8900" in p.get("url", "")),
        next(p for p in pages if p.get("type") == "page")
    )
    ws_url  = page["webSocketDebuggerUrl"]
    print(f"[main] target: {page.get('url','?')[:70]}")
    os.makedirs("assets", exist_ok=True)

    async with websockets.connect(ws_url, max_size=20*1024*1024) as ws:
        # Ensure consistent viewport before recording starts
        _id0 = 0
        async def _send0(m, p={}):
            nonlocal _id0; _id0 += 1
            await ws.send(json.dumps({"id": _id0, "method": m, "params": p}))
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), 10))
                if msg.get("id") == _id0: return msg
        await _send0("Emulation.setDeviceMetricsOverride", {
            "width": VIEWPORT_W, "height": VIEWPORT_H,
            "deviceScaleFactor": 2, "mobile": False
        })
        print(f"[main] viewport locked to {VIEWPORT_W}×{VIEWPORT_H}")
        await asyncio.sleep(1)
        await asyncio.gather(recv_loop(ws), recorder(ws), driver(ws))

    n = len([f for f in os.listdir(FRAMES) if f.endswith(".jpg")])
    dur = n / FPS
    print(f"[main] assembling {n} frames ({dur:.0f}s) → {OUT_MP4}")
    subprocess.run([
        "ffmpeg", "-y",
        "-framerate", str(FPS),
        "-pattern_type", "glob", "-i", f"{FRAMES}/*.jpg",
        "-vf", "scale=1280:-2:flags=lanczos",
        "-c:v", "libx264", "-preset", "slow", "-crf", "16",
        "-movflags", "+faststart", "-pix_fmt", "yuv420p",
        OUT_MP4
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    mb = os.path.getsize(OUT_MP4) / 1024 / 1024
    print(f"\n✓  {OUT_MP4}  ({mb:.1f} MB)")


if __name__ == "__main__":
    asyncio.run(main())
