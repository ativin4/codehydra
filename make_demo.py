#!/usr/bin/env python3
"""
CDP-driven CodeHydra demo recorder (v5) — deterministic, no live API calls.

Records the scripted DemoGateway session served by textual-serve in a real
browser tab, capturing frames over the Chrome DevTools Protocol.

Setup:
    1. Chrome running with --remote-debugging-port=9222
    2. python -c "from textual_serve.server import Server; \
         Server('bash demo_serve.sh', port=8901).serve()" &
    3. .venv/bin/python make_demo.py

Output: assets/demo_v2.mp4
"""
import asyncio, base64, json, os, subprocess, sys, time, urllib.request
try:
    import websockets
except ImportError:
    sys.exit("pip install websockets")

# ── config ──────────────────────────────────────────────────────────────────
CDP_URL    = os.environ.get("CODEHYDRA_CDP_URL", "http://localhost:9222")
PAGE_URL   = "http://localhost:8901"
DEMO_DIR   = os.environ.get("CODEHYDRA_DEMO_DIR", "/tmp/codehydra_demo_repo")
FRAMES     = "/tmp/codehydra_demo_frames"
OUT_MP4    = "assets/demo_v2.mp4"
FPS        = 10
VIEWPORT_W = 1200
VIEWPORT_H = 800
# ────────────────────────────────────────────────────────────────────────────

_gid     = 0
_pending: dict[int, asyncio.Future] = {}
_stop    = asyncio.Event()
frame_n  = 0

VK = {"Enter": 13, "Tab": 9, "ArrowDown": 40, "ArrowUp": 38}


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
    async def click_center():
        for ev in ("mousePressed", "mouseReleased"):
            await cdp(ws, "Input.dispatchMouseEvent", {
                "type": ev, "x": VIEWPORT_W // 2, "y": VIEWPORT_H - 60,
                "button": "left", **({"clickCount": 1} if ev == "mousePressed" else {})
            })
        await asyncio.sleep(0.4)

    async def type_text(text, speed=0.05):
        for ch in text:
            await cdp(ws, "Input.dispatchKeyEvent", {"type": "char", "text": ch})
            await asyncio.sleep(speed)

    async def key(name, pause=0.35):
        for ev in ("rawKeyDown", "keyUp"):
            await cdp(ws, "Input.dispatchKeyEvent", {
                "type": ev, "key": name, "code": name,
                "windowsVirtualKeyCode": VK[name],
            })
        await asyncio.sleep(pause)

    await asyncio.sleep(4)   # header + status bar settle
    await click_center()     # focus the terminal

    # 1. Slash autocomplete: /eff → popup → Down ×2 → Tab → "/effort high"
    print("[drv] autocomplete")
    await type_text("/eff", speed=0.14)
    await asyncio.sleep(1.4)
    await key("ArrowDown", 0.5)
    await key("ArrowDown", 0.5)
    await key("Tab", 0.8)
    await key("Enter", 1.5)

    # 2. First prompt: thinking spinner → stream → new-file diff panel
    print("[drv] prompt 1")
    await type_text("Add a memoized fibonacci function to fib.py with a small CLI.", speed=0.028)
    await asyncio.sleep(0.6)
    await key("Enter", 0.3)
    await asyncio.sleep(14)

    # Track fib.py so turn 2 renders as a modification diff.
    subprocess.run(["git", "-C", DEMO_DIR, "add", "-A"], capture_output=True)
    subprocess.run(["git", "-C", DEMO_DIR, "commit", "-qm", "add fib.py"], capture_output=True)

    # 3. Second prompt: mid-stream fallback → reset → codex finishes
    print("[drv] prompt 2 (fallback)")
    await type_text("Handle negative inputs gracefully.", speed=0.03)
    await asyncio.sleep(0.5)
    await key("Enter", 0.3)
    await asyncio.sleep(14)

    # 4. /cost usage table, hold on status bar
    print("[drv] /cost")
    await type_text("/cost", speed=0.1)
    await asyncio.sleep(0.4)
    await key("Enter", 0.3)
    await asyncio.sleep(6)

    _stop.set()


async def main():
    pages = json.loads(urllib.request.urlopen(f"{CDP_URL}/json").read())
    page  = next((p for p in pages if p.get("type") == "page" and "8901" in p.get("url", "")), None)
    if page is None:
        sys.exit(f"No tab on {PAGE_URL} — open one first (textual-serve must be running).")
    ws_url = page["webSocketDebuggerUrl"]
    print(f"[main] target: {page.get('url','?')[:70]}")
    os.makedirs("assets", exist_ok=True)

    async with websockets.connect(ws_url, max_size=20*1024*1024) as ws:
        _id0 = 0
        async def _send0(m, p={}):
            nonlocal _id0
            _id0 += 1
            await ws.send(json.dumps({"id": _id0, "method": m, "params": p}))
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), 10))
                if msg.get("id") == _id0:
                    return msg
        await _send0("Emulation.setDeviceMetricsOverride", {
            "width": VIEWPORT_W, "height": VIEWPORT_H,
            "deviceScaleFactor": 2, "mobile": False
        })
        # NOTE: do not Page.reload here — xterm.js reconnect races the CDP ws.
        # The tab must be freshly opened (demo_serve.sh resets the repo per session).
        print(f"[main] viewport {VIEWPORT_W}×{VIEWPORT_H}")
        await asyncio.sleep(3)
        # recv_loop never returns on its own — cancel it once driver+recorder finish.
        recv_task = asyncio.create_task(recv_loop(ws))
        await asyncio.gather(recorder(ws), driver(ws))
        recv_task.cancel()

    n = len([f for f in os.listdir(FRAMES) if f.endswith(".jpg")])
    print(f"[main] assembling {n} frames ({n/FPS:.0f}s) → {OUT_MP4}")
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-framerate", str(FPS),
        "-pattern_type", "glob", "-i", f"{FRAMES}/*.jpg",
        "-vf", "scale=1280:-2:flags=lanczos",
        "-c:v", "libx264", "-preset", "slow", "-crf", "17",
        "-movflags", "+faststart", "-pix_fmt", "yuv420p",
        OUT_MP4
    ], check=True)
    print(f"✓ {OUT_MP4} ({os.path.getsize(OUT_MP4)/1024/1024:.1f} MB)")


if __name__ == "__main__":
    asyncio.run(main())
