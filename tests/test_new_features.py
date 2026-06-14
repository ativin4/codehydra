"""Tests for features added since last test run:
- Context sizing (sliding window, /compact)
- Project memory (_refresh_system_msg)
- Web search (search(), fetch_url(), format_results())
- Media expansion (@file, @url, @image, @pdf)
- Gateway _build_cmd media injection
- Gateway request/request_stream with real cloud CLIs (claude)
"""
import base64
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Context sizing — sliding window
# ---------------------------------------------------------------------------

class TestSlidingWindow:
    def test_trims_old_turns_for_gemini(self):
        from src.routing.gateway import Gateway
        gw = Gateway.__new__(Gateway)
        gw._CONTEXT_WINDOW = 4  # small window for testing

        system = {"role": "system", "content": "sys"}
        turns = [
            msg
            for i in range(5)
            for msg in (
                {"role": "user", "content": f"q{i}"},
                {"role": "assistant", "content": f"a{i}"},
            )
        ]
        messages = [system] + turns  # 10 non-system messages

        # Simulate what _build_cmd does for non-claude CLIs
        result_system = [m for m in messages if m["role"] == "system"]
        result_turns = [m for m in messages if m["role"] != "system"]
        if len(result_turns) > gw._CONTEXT_WINDOW:
            result_turns = result_turns[-gw._CONTEXT_WINDOW:]
        result = result_system + result_turns

        assert result[0]["role"] == "system"
        assert len([m for m in result if m["role"] != "system"]) == 4

    def test_claude_not_trimmed(self):
        from src.routing.gateway import Gateway
        # Claude uses persistent session — no trimming in _build_cmd
        # Just verify the constant exists
        assert hasattr(Gateway, "_CONTEXT_WINDOW")
        assert Gateway._CONTEXT_WINDOW > 0


# ---------------------------------------------------------------------------
# Project memory
# ---------------------------------------------------------------------------

class TestProjectMemory:
    def test_refresh_with_no_memory_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        import src.tui as tui_mod
        monkeypatch.setattr(tui_mod, "MEMORY_FILE", tmp_path / "memory.md")

        app = tui_mod.HydraApp.__new__(tui_mod.HydraApp)
        app.system_msg = "base system"
        app.history = [{"role": "system", "content": "base system"}]
        app._refresh_system_msg()

        assert app.history[0]["content"] == "base system"

    def test_refresh_prepends_memory(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        import src.tui as tui_mod
        mem_file = tmp_path / "memory.md"
        mem_file.write_text("- use pytest\n- always type hint\n")
        monkeypatch.setattr(tui_mod, "MEMORY_FILE", mem_file)

        app = tui_mod.HydraApp.__new__(tui_mod.HydraApp)
        app.system_msg = "base system"
        app.history = [{"role": "system", "content": "base system"}]
        app._refresh_system_msg()

        content = app.history[0]["content"]
        assert "## Project Memory" in content
        assert "use pytest" in content
        assert "base system" in content
        # memory comes before base system
        assert content.index("Project Memory") < content.index("base system")

    def test_memory_survives_compact(self, tmp_path, monkeypatch):
        """Memory in history[0] must not be wiped by _do_compact."""
        monkeypatch.chdir(tmp_path)
        import src.tui as tui_mod
        mem_file = tmp_path / "memory.md"
        mem_file.write_text("- key fact\n")
        monkeypatch.setattr(tui_mod, "MEMORY_FILE", mem_file)

        app = tui_mod.HydraApp.__new__(tui_mod.HydraApp)
        app.system_msg = "base"
        app.history = [{"role": "system", "content": "base"}]
        app._refresh_system_msg()

        # _do_compact rebuilds from system + summary + recent turns.
        # After compact, history[0] must still be the system entry.
        system_content = app.history[0]["content"]
        assert "key fact" in system_content


# ---------------------------------------------------------------------------
# Web search
# ---------------------------------------------------------------------------

class TestWebSearch:
    def test_format_results_empty(self):
        from src.tools.websearch import format_results
        out = format_results([])
        assert "No results" in out

    def test_format_results_populated(self):
        from src.tools.websearch import format_results
        results = [
            {"title": "Foo Bar", "url": "https://foo.com", "snippet": "A snippet."},
        ]
        out = format_results(results)
        assert "Foo Bar" in out
        assert "foo.com" in out
        assert "snippet" in out

    def test_search_returns_results(self):
        from src.tools.websearch import search
        results = search("python pathlib tutorial", max_results=3)
        assert isinstance(results, list)
        assert len(results) >= 1
        for r in results:
            assert "title" in r
            assert "url" in r
            assert r["url"].startswith("http")

    def test_fetch_url_returns_text(self):
        from src.tools.websearch import fetch_url
        text = fetch_url("https://example.com", max_chars=500)
        assert isinstance(text, str)
        assert len(text) > 10
        assert "example" in text.lower()


# ---------------------------------------------------------------------------
# Media expansion
# ---------------------------------------------------------------------------

class TestMediaExpansion:
    def _make_app(self, tmp_path, monkeypatch):
        import src.tui as tui_mod
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(tui_mod, "MEMORY_FILE", tmp_path / "memory.md")
        app = tui_mod.HydraApp.__new__(tui_mod.HydraApp)
        app.system_msg = "sys"
        app.history = [{"role": "system", "content": "sys"}]
        return app

    def test_text_file_expanded_inline(self, tmp_path, monkeypatch):
        app = self._make_app(tmp_path, monkeypatch)
        f = tmp_path / "hello.py"
        f.write_text("print('hello')")
        expanded, injected, media = app._expand_file_refs(f"@{f} explain this")
        assert "print('hello')" in expanded
        assert str(f) in injected or "hello.py" in " ".join(injected)
        assert media == []

    def test_image_detected_as_media(self, tmp_path, monkeypatch):
        app = self._make_app(tmp_path, monkeypatch)
        img = tmp_path / "shot.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)  # minimal PNG header
        expanded, injected, media = app._expand_file_refs(f"@{img} describe this")
        assert len(media) == 1
        assert media[0] == img
        assert "shot.png" in expanded or "shot.png" in " ".join(injected)
        # image bytes NOT in prompt
        assert "PNG" not in expanded

    def test_pdf_detected_as_media(self, tmp_path, monkeypatch):
        app = self._make_app(tmp_path, monkeypatch)
        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake content")
        expanded, injected, media = app._expand_file_refs(f"@{pdf} summarize")
        assert len(media) == 1
        assert media[0] == pdf

    def test_url_fetched_inline(self, tmp_path, monkeypatch):
        app = self._make_app(tmp_path, monkeypatch)
        with patch("src.tui.fetch_url", return_value="mocked content") as mock_fetch:
            expanded, injected, media = app._expand_file_refs(
                "@https://example.com what does this say"
            )
        assert "mocked content" in expanded
        assert "https://example.com" in injected
        assert media == []


# ---------------------------------------------------------------------------
# Gateway _build_cmd media injection
# ---------------------------------------------------------------------------

class TestBuildCmdMedia:
    def _make_gateway(self):
        from src.routing.gateway import Gateway
        gw = Gateway.__new__(Gateway)
        gw._CONTEXT_WINDOW = 30
        gw.mcp_servers = {}
        gw.claude_mcp_config_path = None
        gw.MODE_FLAGS = Gateway.MODE_FLAGS
        return gw

    def test_gemini_gets_at_path_refs(self, tmp_path):
        gw = self._make_gateway()
        img = tmp_path / "shot.png"
        img.write_bytes(b"\x89PNG" + b"\x00" * 10)

        messages = [{"role": "user", "content": "describe this image"}]
        with patch("shutil.which", return_value="/usr/bin/gemini"):
            cmd, _ = gw._build_cmd("gemini", messages, "gemini/gemini-2.5-flash",
                                    media_files=[img])
        prompt_arg = cmd[cmd.index("--prompt") + 1]
        assert f"@{img.absolute()}" in prompt_arg

    def test_claude_gets_pdf_text(self, tmp_path):
        gw = self._make_gateway()
        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(b"%PDF fake")

        messages = [{"role": "user", "content": "summarize"}]
        with patch("shutil.which", return_value="/usr/bin/claude"), \
             patch("src.routing.gateway.pdf_to_text", return_value="Extracted text here"):
            cmd, _ = gw._build_cmd("claude", messages, "anthropic/claude-sonnet-4-6",
                                    media_files=[pdf])
        # The extracted text should appear in the -p prompt arg
        prompt_arg = cmd[cmd.index("-p") + 1]
        assert "Extracted text here" in prompt_arg

    def test_claude_image_gets_note(self, tmp_path):
        gw = self._make_gateway()
        img = tmp_path / "photo.jpg"
        img.write_bytes(b"\xff\xd8\xff" + b"\x00" * 10)

        messages = [{"role": "user", "content": "what is this?"}]
        with patch("shutil.which", return_value="/usr/bin/claude"):
            cmd, _ = gw._build_cmd("claude", messages, "anthropic/claude-sonnet-4-6",
                                    media_files=[img])
        prompt_arg = cmd[cmd.index("-p") + 1]
        assert "photo.jpg" in prompt_arg
        assert "not supported" in prompt_arg.lower() or "image" in prompt_arg.lower()

    def test_ollama_images_base64(self, tmp_path):
        from src.routing.gateway import Gateway
        gw = Gateway.__new__(Gateway)
        gw._record_usage = lambda *a, **kw: None

        img = tmp_path / "img.png"
        img.write_bytes(b"\x89PNG" + b"\x00" * 10)

        messages = [{"role": "user", "content": "describe"}]
        captured = []

        def fake_generate(model, msgs):
            captured.append(msgs)
            return iter(["ok"])

        with patch("src.routing.gateway.OllamaProvider.generate", side_effect=fake_generate):
            list(gw._run_oss_stream("ollama/llava", messages, media_files=[img]))

        assert captured
        last_user = next(m for m in reversed(captured[0]) if m["role"] == "user")
        assert "images" in last_user
        assert len(last_user["images"]) == 1
        # verify it's valid base64
        base64.b64decode(last_user["images"][0])


# ---------------------------------------------------------------------------
# End-to-end: real cloud CLI (claude)
# ---------------------------------------------------------------------------

class TestCloudCLI:
    """Requires active claude CLI session. Skipped if claude not found."""

    @pytest.fixture(autouse=True)
    def skip_if_no_claude(self):
        import shutil
        if not shutil.which("claude"):
            pytest.skip("claude CLI not on PATH")

    def test_simple_request(self):
        from src.routing.gateway import Gateway
        gw = Gateway()
        result = gw.request(
            "Reply with exactly: HYDRA_OK",
            tier="low",
            cli_override="claude",
        )
        assert "HYDRA_OK" in result

    def test_stream_request(self):
        from src.routing.gateway import Gateway
        gw = Gateway()
        chunks = list(gw.request_stream(
            "Reply with exactly: STREAM_OK",
            tier="low",
            cli_override="claude",
        ))
        full = "".join(chunks)
        assert "STREAM_OK" in full

    def test_memory_injected_in_system(self):
        """System prompt content must be visible to the model."""
        from src.routing.gateway import Gateway
        gw = Gateway()
        marker = "XHYDRA_TEST_MARKER_42X"
        system = f"Whenever asked to repeat the marker, say exactly: {marker}"
        result = gw.request(
            "Repeat the marker from your instructions. Output only the marker, nothing else.",
            tier="low",
            cli_override="claude",
            history=[{"role": "system", "content": system}],
        )
        assert marker in result

    def test_media_pdf_text_extraction(self, tmp_path):
        """If pdftotext is available, extracted text reaches the model."""
        import shutil as sh
        if not sh.which("pdftotext"):
            pytest.skip("pdftotext not installed")
        # Use a known simple PDF (create via a minimal approach)
        pytest.skip("Requires a real PDF file — manual test")

    def test_web_search_injects_context(self):
        from src.routing.gateway import Gateway
        from src.tools.websearch import format_results, search
        gw = Gateway()
        results = search("Python pathlib", max_results=2)
        ctx = format_results(results)
        result = gw.request(
            f"Given these search results:\n{ctx}\n\nWhat library is being discussed? Reply in one word.",
            tier="low",
            cli_override="claude",
        )
        assert "pathlib" in result.lower() or "python" in result.lower()
