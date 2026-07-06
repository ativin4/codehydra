"""Pilot tests for launch-polish TUI behavior: autocomplete key routing,
/clear reset, and the collapsible per-file diff view."""

import pytest
from textual.widgets import Collapsible, ListView, TextArea

import codehydra.tui as tui_mod
from codehydra.tui import _count_changes, _split_diff_by_file, _style_diff_lines


class FakeGateway:
    LOGIN_COMMANDS = {}

    def __init__(self):
        self.cli_auth_status = {"claude": True, "agy": True, "codex": True}
        self.last_usage = None
        self.on_fallback = None
        self._cancel_event = None
        self.reset_called = False

    def rate_limit_resets_in(self, name):
        return None

    def reset_session(self):
        self.reset_called = True

    def refresh_auth(self):
        pass

    def close(self, stop_mcp_server=False):
        pass


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tui_mod, "Gateway", FakeGateway)
    return tui_mod.HydraApp()


async def test_arrow_keys_drive_autocomplete_popup(app):
    async with app.run_test() as pilot:
        await pilot.press("slash", "c")
        await pilot.pause()
        assert app._autocomplete_visible()
        lv = app.query_one("#autocomplete", ListView)
        start = lv.index
        await pilot.press("down")
        assert lv.index == start + 1
        await pilot.press("up")
        assert lv.index == start
        await pilot.press("tab")
        area = app.query_one("#input-area", TextArea)
        assert area.text.startswith("/c")
        assert not app._autocomplete_visible()


async def test_enter_accepts_popup_after_arrow_navigation(app):
    async with app.run_test() as pilot:
        await pilot.press("slash", "c", "l")
        await pilot.pause()
        assert app._autocomplete_visible()
        await pilot.press("down", "enter")
        area = app.query_one("#input-area", TextArea)
        # Enter accepted the arrow-selected completion; nothing was submitted.
        assert area.text.startswith("/cl")
        assert not app._autocomplete_visible()


async def test_enter_submits_typed_text_without_navigation(app):
    async with app.run_test() as pilot:
        await pilot.press(*"/login")
        await pilot.pause()
        assert app._autocomplete_visible()
        await pilot.press("enter")
        area = app.query_one("#input-area", TextArea)
        # No arrow navigation: Enter submitted "/login" (usage message shown)
        # instead of accepting "/login claude".
        assert area.text == ""
        from textual.widgets import Static
        texts = [str(w._Static__content) for w in app.query_one("#history").query(Static)]
        assert any("usage" in t.lower() for t in texts if t)


async def test_arrow_keys_navigate_history_when_popup_hidden(app):
    async with app.run_test() as pilot:
        app._prompt_history = ["earlier prompt"]
        app._history_index = 1
        await pilot.press("up")
        area = app.query_one("#input-area", TextArea)
        assert area.text == "earlier prompt"


async def test_clear_resets_usage_log_and_session(app):
    async with app.run_test() as pilot:
        app.usage_log = [{"cli": "claude", "model": "anthropic/sonnet", "tier": "medium"}]
        app._handle_command("/clear")
        await pilot.pause()
        assert app.usage_log == []
        assert app.gateway.reset_called
        assert len(app.history) == 1


def test_split_diff_by_file():
    diff = (
        "diff --git a/foo.py b/foo.py\n"
        "index 111..222 100644\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-old\n"
        "+new\n"
        "diff --git a/bar.py b/bar.py\n"
        "--- a/bar.py\n"
        "+++ b/bar.py\n"
        "@@ -0,0 +1,1 @@\n"
        "+added\n"
    )
    files = _split_diff_by_file(diff)
    assert [p for p, _ in files] == ["foo.py", "bar.py"]
    assert "-old" in files[0][1]
    assert "+added" in files[1][1]


def test_count_changes_skips_file_headers():
    file_diff = "--- a/foo.py\n+++ b/foo.py\n@@ -1,2 +1,2 @@\n-old\n+new\n+more\n"
    assert _count_changes(file_diff) == (2, 1)


def test_style_diff_lines_truncates():
    file_diff = "\n".join(f"+line{i}" for i in range(50))
    body = _style_diff_lines(file_diff, max_lines=10)
    assert "more lines (truncated)" in body.plain
    assert "line9" in body.plain
    assert "line11" not in body.plain


async def test_mount_diff_view_creates_collapsible_panels(app):
    async with app.run_test() as pilot:
        app._mount_diff_view([
            ("foo.py", "@@ -1,2 +1,2 @@\n-old\n+new"),
            ("bar.py", "@@ -0,0 +1,2 @@\n+a\n+b"),
        ])
        await pilot.pause()
        panels = app.query(".diff-file")
        assert len(panels) == 2
        titles = [p.title for p in panels]
        assert "foo.py  +1 −1" in titles
        assert "bar.py  +2 −0" in titles
        # First few files start expanded when the set is small.
        assert not panels[0].collapsed
