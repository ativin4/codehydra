"""Smoke tests for the CODEHYDRA_DEMO_SCRIPT canned-playback mode."""

import json
import subprocess

import pytest

import codehydra.tui as tui_mod
from codehydra.routing.demo_gateway import DemoGateway
from codehydra.routing.gateway import STREAM_RESET
from codehydra.routing.claude_session import THINKING_END, THINKING_START


SCRIPT = {
    "turns": [
        {
            "thinking": "short reasoning",
            "response": "answer one",
            "cli": "claude",
            "model": "anthropic/claude-sonnet-4-5",
            "tier": "medium",
            "tokens": 100,
            "write_file": {"path": "fib.py", "content": "print('hi')\n"},
        },
        {
            "response": "answer two after fallback",
            "cli": "codex",
            "model": "codex/gpt-5-codex",
            "fail_midway": {"after_chars": 6, "notice": "claude failed mid-response"},
        },
    ]
}


@pytest.fixture
def script_path(tmp_path):
    p = tmp_path / "script.json"
    p.write_text(json.dumps(SCRIPT))
    return p


def test_demo_stream_turn_with_thinking_and_file_write(tmp_path, script_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    gw = DemoGateway(script_path)
    gw.CHUNK_DELAY = 0
    gw.THINKING_DELAY = 0

    chunks = list(gw.request_stream("prompt one"))
    assert chunks[0] == THINKING_START
    assert THINKING_END in chunks
    text = "".join(c for c in chunks if c not in (THINKING_START, THINKING_END))
    assert "answer one" in text
    assert (tmp_path / "fib.py").exists()
    assert gw.last_usage["cli"] == "claude"


def test_demo_stream_fail_midway_emits_reset_and_fallback(tmp_path, script_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    gw = DemoGateway(script_path)
    gw.CHUNK_DELAY = 0
    gw.THINKING_DELAY = 0
    notices = []
    gw.on_fallback = notices.append

    list(gw.request_stream("first"))
    chunks = list(gw.request_stream("second"))
    assert chunks.count(STREAM_RESET) == 1
    reset_idx = chunks.index(STREAM_RESET)
    assert "".join(chunks[reset_idx + 1:]).strip() == "answer two after fallback"
    assert any("failed mid-response" in n for n in notices)
    assert gw.last_usage["cli"] == "codex"


async def test_hydra_app_uses_demo_gateway_when_env_set(tmp_path, script_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    monkeypatch.setenv("CODEHYDRA_DEMO_SCRIPT", str(script_path))
    app = tui_mod.HydraApp()
    assert isinstance(app.gateway, DemoGateway)
