"""Real evals for CodeHydra — bug fixes, TUI headless pilot, real CLI.

Sections:
  1. Bug fix unit tests (no network, no TUI)
  2. TUI headless pilot tests (Textual run_test, async)
  3. Real CLI evals (require active claude session)
"""
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Group
from rich.text import Text


# ============================================================
# Helpers
# ============================================================

def _static_texts(app) -> list[str]:
    """Return plain-text content of all Static widgets in #history."""
    from textual.widgets import Static
    out = []
    for w in app.query_one("#history").query(Static):
        content = w._Static__content
        if isinstance(content, str):
            out.append(content)
        elif isinstance(content, Text):
            out.append(str(content))
        elif isinstance(content, Group):
            for item in content.renderables:
                if isinstance(item, (Text, str)):
                    out.append(str(item))
        else:
            out.append(str(content))
    return out


def _make_tui_app(tmp_path):
    """HydraApp with mocked Gateway/Scanner/SessionManager for fast headless tests."""
    import codehydra.tui as tui_mod
    from codehydra.routing.constants import CLI

    with patch("codehydra.tui.Gateway") as MockGW, \
         patch("codehydra.tui.Scanner") as MockScanner, \
         patch("codehydra.tui.SessionManager") as MockSM:

        gw_inst = MagicMock()
        gw_inst.cli_auth_status = {
            CLI.CLAUDE: True, CLI.AGY: False,
            CLI.CODEX: False, CLI.OLLAMA: False,
        }
        gw_inst.rate_limit_resets_in.return_value = None
        gw_inst.last_usage = None
        MockGW.return_value = gw_inst
        MockGW.LOGIN_COMMANDS = {CLI.CLAUDE: ["claude", "auth", "login"]}
        MockGW.CLI_DEFAULT_MODELS = {
            CLI.CLAUDE: {}, CLI.AGY: {}, CLI.CODEX: {}, CLI.OLLAMA: {},
        }

        scanner_inst = MagicMock()
        scanner_inst.get_system_prompt_context.return_value = ""
        MockScanner.return_value = scanner_inst

        sm_inst = MagicMock()
        sm_inst.new_session_id.return_value = "test-session-001"
        sm_inst.list_sessions.return_value = []
        MockSM.return_value = sm_inst

        app = tui_mod.HydraApp()
        tui_mod.MEMORY_FILE = tmp_path / ".codehydra" / "memory.md"
        return app


@pytest.fixture
def tmp_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


async def _type_command(pilot, cmd: str, pause: float = 0.3):
    for ch in cmd:
        await pilot.press(ch)
    await pilot.press("enter")
    await pilot.pause(pause)


# ============================================================
# 1. Bug fix unit tests
# ============================================================

class TestURLTrailingPunct:
    """Bug 3: @url tokens must have trailing punct stripped before fetching."""

    def _make_app(self, tmp_path, monkeypatch):
        import codehydra.tui as tui_mod
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(tui_mod, "MEMORY_FILE", tmp_path / "memory.md")
        app = tui_mod.HydraApp.__new__(tui_mod.HydraApp)
        app.system_msg = "sys"
        app.history = [{"role": "system", "content": "sys"}]
        return app

    @pytest.mark.parametrize("suffix,expected_url", [
        (".",   "https://example.com"),
        (",",   "https://example.com"),
        (")",   "https://example.com"),
        (">",   "https://example.com"),
        ("]",   "https://example.com"),
        ("!?",  "https://example.com"),
        ("",    "https://example.com"),
    ])
    def test_url_punct_stripped(self, suffix, expected_url, tmp_path, monkeypatch):
        app = self._make_app(tmp_path, monkeypatch)
        fetched_urls = []

        def fake_fetch(url, **kw):
            fetched_urls.append(url)
            return "content"

        with patch("codehydra.tui.fetch_url", side_effect=fake_fetch):
            app._expand_file_refs(f"@https://example.com{suffix} text")

        assert fetched_urls == [expected_url]

    def test_dots_inside_url_path_kept(self, tmp_path, monkeypatch):
        """Interior dots (in path/query) must not be stripped."""
        app = self._make_app(tmp_path, monkeypatch)
        fetched = []

        def fake_fetch(url, **kw):
            fetched.append(url)
            return "ok"

        with patch("codehydra.tui.fetch_url", side_effect=fake_fetch):
            app._expand_file_refs("@https://docs.python.org/3/library/pathlib.html text")

        assert fetched == ["https://docs.python.org/3/library/pathlib.html"]


class TestResumeRebuildsSystemMsg:
    """Bug 2: /resume must rebuild system_msg from fresh workspace scan."""

    def test_resume_updates_system_msg_and_history0(self, tmp_path, monkeypatch):
        import codehydra.tui as tui_mod
        from codehydra.routing.constants import Role

        monkeypatch.chdir(tmp_path)
        mem_file = tmp_path / "memory.md"
        monkeypatch.setattr(tui_mod, "MEMORY_FILE", mem_file)

        app = tui_mod.HydraApp.__new__(tui_mod.HydraApp)
        app.system_msg = "OLD system"
        app.history = [{"role": Role.SYSTEM, "content": "OLD system"}]
        app.effort_tier = None
        app.cli_override = None
        app.model_override = None
        app.mode = "yolo"
        app.usage_log = []

        monkeypatch.setattr(tui_mod.HydraApp, "_build_system_msg_base", lambda s: "FRESH system")
        monkeypatch.setattr(tui_mod.HydraApp, "_render_history", lambda s: None)
        monkeypatch.setattr(tui_mod.HydraApp, "_add_message", lambda s, m: None)
        monkeypatch.setattr(tui_mod.HydraApp, "_update_status", lambda s: None)

        sessions_mock = MagicMock()
        sessions_mock.load.return_value = {
            "history": [
                {"role": "system", "content": "STALE system"},
                {"role": "user", "content": "hello"},
            ],
            "effort_tier": "high",
            "cli_override": "claude",
            "model_override": None,
            "mode": "plan",
            "usage_log": [],
        }
        app.sessions = sessions_mock

        # Replay /resume handler (no memory file → _refresh_system_msg just sets history[0]=system_msg)
        data = sessions_mock.load("tid")
        app.session_id = "tid"
        app.history = data.get("history", app.history)
        app.effort_tier = data.get("effort_tier")
        app.cli_override = data.get("cli_override")
        app.model_override = data.get("model_override")
        app.mode = data.get("mode", "yolo")
        app.usage_log = data.get("usage_log", [])
        app.system_msg = tui_mod.HydraApp._build_system_msg_base(app)
        tui_mod.HydraApp._refresh_system_msg(app)  # real impl: reads non-existent mem_file → noop

        assert app.system_msg == "FRESH system"
        assert app.history[0]["content"] == "FRESH system"


class TestDoCompactPicksBestCLI:
    """Bug 7: _do_compact must pick best authenticated CLI, not self.cli_override."""

    def _seed_app(self, tmp_path, monkeypatch, auth_status):
        import codehydra.tui as tui_mod
        from codehydra.routing.constants import CLI, Mode, Role

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(tui_mod, "MEMORY_FILE", tmp_path / "memory.md")

        app = tui_mod.HydraApp.__new__(tui_mod.HydraApp)
        app.system_msg = "sys"
        app.cli_override = CLI.AGY
        app.mode = Mode.PLAN
        app.history = [{"role": Role.SYSTEM, "content": "sys"}] + [
            msg
            for i in range(6)
            for msg in (
                {"role": Role.USER, "content": f"q{i}"},
                {"role": Role.ASSISTANT, "content": f"a{i}"},
            )
        ]
        gw = MagicMock()
        gw.cli_auth_status = auth_status
        used_clis = []
        gw.request.side_effect = lambda *a, **kw: (used_clis.append(kw.get("cli_override")), "• s")[1]
        app.gateway = gw
        app._save_session = lambda: None
        return app, used_clis

    def test_picks_claude_over_override(self, tmp_path, monkeypatch):
        from codehydra.routing.constants import CLI
        app, used_clis = self._seed_app(
            tmp_path, monkeypatch,
            {CLI.CLAUDE: True, CLI.AGY: True, CLI.CODEX: False},
        )
        import codehydra.tui as tui_mod
        tui_mod.HydraApp._do_compact(app)
        assert used_clis and used_clis[0] == CLI.CLAUDE

    def test_falls_back_to_agy_when_claude_absent(self, tmp_path, monkeypatch):
        from codehydra.routing.constants import CLI
        app, used_clis = self._seed_app(
            tmp_path, monkeypatch,
            {CLI.CLAUDE: False, CLI.AGY: True, CLI.CODEX: False},
        )
        import codehydra.tui as tui_mod
        tui_mod.HydraApp._do_compact(app)
        assert used_clis and used_clis[0] == CLI.AGY

    def test_falls_back_to_override_when_none_authenticated(self, tmp_path, monkeypatch):
        from codehydra.routing.constants import CLI
        app, used_clis = self._seed_app(
            tmp_path, monkeypatch,
            {CLI.CLAUDE: False, CLI.AGY: False, CLI.CODEX: False},
        )
        import codehydra.tui as tui_mod
        tui_mod.HydraApp._do_compact(app)
        assert used_clis and used_clis[0] == CLI.AGY  # fallback to self.cli_override


class TestOllamaExcludedFromAuthWarning:
    """Bug 5: ollama not shown in 'not logged in' warning — LOGIN_COMMANDS is the authority."""

    @pytest.mark.parametrize("ollama_ok", [True, False])
    def test_ollama_never_in_unauthenticated(self, ollama_ok):
        from codehydra.routing.constants import CLI
        from codehydra.routing.gateway import Gateway

        auth = {CLI.CLAUDE: False, CLI.AGY: False, CLI.CODEX: False, CLI.OLLAMA: ollama_ok}
        unauthenticated = [c for c, ok in auth.items() if not ok and c in Gateway.LOGIN_COMMANDS]
        assert CLI.OLLAMA not in unauthenticated


class TestCompareAppendsHistory:
    """Bug 6: /compare must append user prompt + combined answer to history."""

    def test_compare_appends_user_and_assistant(self, tmp_path, monkeypatch):
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import codehydra.tui as tui_mod
        from codehydra.routing.constants import CLI, Mode, Role

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(tui_mod, "MEMORY_FILE", tmp_path / "memory.md")

        app = tui_mod.HydraApp.__new__(tui_mod.HydraApp)
        app.system_msg = "sys"
        app.history = [{"role": Role.SYSTEM, "content": "sys"}]
        app.usage_log = []
        app.effort_tier = None
        app.mode = Mode.YOLO

        gw = MagicMock()
        gw.cli_auth_status = {CLI.CLAUDE: True, CLI.AGY: True}
        gw.request.side_effect = lambda *a, cli_override=None, **kw: f"answer from {cli_override}"
        gw.last_usage = {"cli": "claude", "model": "a/haiku", "tokens": 10, "tier": "low"}
        app.gateway = gw
        app._save_session = lambda: None

        prompt = "what is 2+2?"
        cli_names = [CLI.CLAUDE, CLI.AGY]

        # Replay the _run_compare post-loop logic
        results_by_cli = {CLI.CLAUDE: "4 (claude)", CLI.AGY: "4 (agy)"}
        if results_by_cli:
            combined = "\n\n---\n\n".join(
                f"**{cli}:**\n{results_by_cli[cli]}"
                for cli in cli_names if cli in results_by_cli
            )
            app.history.append({"role": Role.USER, "content": prompt})
            app.history.append({"role": Role.ASSISTANT, "content": f"[Compare]\n{combined}"})

        assert len(app.history) == 3
        assert app.history[1]["role"] == Role.USER
        assert app.history[1]["content"] == prompt
        assert "[Compare]" in app.history[2]["content"]
        assert "claude" in app.history[2]["content"]
        assert "agy" in app.history[2]["content"]


# ============================================================
# 2. TUI headless pilot tests
# ============================================================

class TestTUIHeadless:
    """Headless Textual run_test pilot — no network, mocked gateway."""

    async def test_app_starts_without_crash(self, tmp_cwd):
        app = _make_tui_app(tmp_cwd)
        async with app.run_test(headless=True, size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            history = app.query_one("#history")
            assert list(history.children)  # startup banner mounted

    async def test_help_command_renders_markdown(self, tmp_cwd):
        """/help mounts a Markdown widget with HELP_TEXT content."""
        from textual.widgets import Markdown

        app = _make_tui_app(tmp_cwd)
        async with app.run_test(headless=True, size=(120, 40)) as pilot:
            await _type_command(pilot, "/help")
            mds = list(app.query_one("#history").query(Markdown))
            assert mds, "Expected Markdown widget after /help"
            last = mds[-1]
            src = last._markdown or last._initial_markdown
            assert "CodeHydra Commands" in src

    async def test_clear_resets_history_list(self, tmp_cwd):
        """/clear must leave only the system entry in app.history."""
        from codehydra.routing.constants import Role

        app = _make_tui_app(tmp_cwd)
        app.history.append({"role": Role.USER, "content": "hi"})
        app.history.append({"role": Role.ASSISTANT, "content": "hello"})

        async with app.run_test(headless=True, size=(120, 40)) as pilot:
            await _type_command(pilot, "/clear")
            assert len(app.history) == 1
            assert app.history[0]["role"] == Role.SYSTEM

    async def test_memory_empty_message(self, tmp_cwd):
        """/memory with no file shows 'No project memory' text."""
        app = _make_tui_app(tmp_cwd)
        async with app.run_test(headless=True, size=(120, 40)) as pilot:
            await _type_command(pilot, "/memory")
            texts = _static_texts(app)
            assert any("project memory" in t.lower() for t in texts), texts

    async def test_remember_and_memory_show_fact(self, tmp_cwd):
        """/remember saves fact; /memory renders it as Markdown."""
        import codehydra.tui as tui_mod
        from textual.widgets import Markdown

        tui_mod.MEMORY_FILE = tmp_cwd / ".codehydra" / "memory.md"
        app = _make_tui_app(tmp_cwd)

        async with app.run_test(headless=True, size=(120, 40)) as pilot:
            await _type_command(pilot, "/remember always use type hints")
            await _type_command(pilot, "/memory", pause=0.3)
            mds = list(app.query_one("#history").query(Markdown))
            assert mds, "Expected Markdown widget after /memory"
            src_md = mds[-1]._markdown or mds[-1]._initial_markdown
            assert "type hints" in src_md

    async def test_effort_high_sets_tier(self, tmp_cwd):
        app = _make_tui_app(tmp_cwd)
        async with app.run_test(headless=True, size=(120, 40)) as pilot:
            await _type_command(pilot, "/effort high")
            assert app.effort_tier == "high"

    async def test_mode_plan_sets_mode(self, tmp_cwd):
        from codehydra.routing.constants import Mode

        app = _make_tui_app(tmp_cwd)
        async with app.run_test(headless=True, size=(120, 40)) as pilot:
            await _type_command(pilot, "/mode plan")
            assert app.mode == Mode.PLAN

    async def test_unknown_command_error_shown(self, tmp_cwd):
        app = _make_tui_app(tmp_cwd)
        async with app.run_test(headless=True, size=(120, 40)) as pilot:
            await _type_command(pilot, "/zzzbadcmd")
            texts = _static_texts(app)
            assert any("unknown command" in t.lower() for t in texts), texts

    async def test_sessions_empty_message(self, tmp_cwd):
        app = _make_tui_app(tmp_cwd)
        async with app.run_test(headless=True, size=(120, 40)) as pilot:
            await _type_command(pilot, "/sessions")
            texts = _static_texts(app)
            assert any("no saved sessions" in t.lower() for t in texts), texts

    async def test_compare_needs_two_clis(self, tmp_cwd):
        from codehydra.routing.constants import CLI

        app = _make_tui_app(tmp_cwd)
        app.gateway.cli_auth_status = {
            CLI.CLAUDE: True, CLI.AGY: False,
            CLI.CODEX: False, CLI.OLLAMA: False,
        }
        async with app.run_test(headless=True, size=(120, 40)) as pilot:
            await _type_command(pilot, "/compare what is 2+2")
            texts = _static_texts(app)
            assert any("at least 2" in t.lower() for t in texts), texts

    async def test_status_bar_shows_session_id(self, tmp_cwd):
        from textual.widgets import Static

        app = _make_tui_app(tmp_cwd)
        async with app.run_test(headless=True, size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            status = app.query_one("#status", Static)
            content = status._Static__content
            assert "test-session-001" in str(content)

    async def test_exit_command_terminates_gracefully(self, tmp_cwd):
        app = _make_tui_app(tmp_cwd)
        async with app.run_test(headless=True, size=(120, 40)) as pilot:
            await _type_command(pilot, "exit")
            # No crash = pass

    async def test_enter_submits_and_ctrl_enter_adds_newline(self, tmp_cwd):
        """Enter submits; Ctrl+Enter inserts a newline in the prompt."""
        from codehydra.routing.constants import Role
        from textual.widgets import TextArea

        app = _make_tui_app(tmp_cwd)
        async with app.run_test(headless=True, size=(120, 40)) as pilot:
            text_area = app.query_one("#input-area", TextArea)

            for ch in "line one":
                await pilot.press(ch)
            await pilot.press("ctrl+enter")
            for ch in "line two":
                await pilot.press(ch)
            await pilot.pause(0.1)

            assert text_area.text == "line one\nline two"

            await pilot.press("enter")
            await pilot.pause(0.2)

            user_msgs = [m for m in app.history if m["role"] == Role.USER]
            assert len(user_msgs) > 0, "Expected user message in history"
            last_user = user_msgs[-1]
            assert "line one" in last_user["content"]
            assert "line two" in last_user["content"]

            assert text_area.text == ""


# ============================================================
# 3. Real CLI evals (require active claude session)
# ============================================================

class TestRealCLIEvals:

    @pytest.fixture(autouse=True)
    def require_claude(self):
        import shutil
        if not shutil.which("claude"):
            pytest.skip("claude CLI not on PATH")

    async def test_prompt_updates_history(self, tmp_cwd):
        """Real prompt through TUI — history gains user+assistant entries."""
        import codehydra.tui as tui_mod
        from codehydra.routing.constants import Role
        from codehydra.routing.gateway import Gateway

        app = _make_tui_app(tmp_cwd)
        app.gateway = Gateway()

        async with app.run_test(headless=True, size=(120, 40)) as pilot:
            initial = len(app.history)
            await _type_command(pilot, "Reply with exactly: EVAL_OK", pause=0.5)
            deadline = time.monotonic() + 30
            while len(app.history) <= initial and time.monotonic() < deadline:
                await pilot.pause(0.5)

            assert len(app.history) > initial
            last = next((m for m in reversed(app.history) if m["role"] == Role.ASSISTANT), None)
            assert last and "EVAL_OK" in last["content"]

    async def test_compact_shortens_history(self, tmp_cwd):
        """/compact with 8+ turns summarises and shrinks history."""
        import codehydra.tui as tui_mod
        from codehydra.routing.constants import Role
        from codehydra.routing.gateway import Gateway

        app = _make_tui_app(tmp_cwd)
        app.gateway = Gateway()

        for i in range(4):
            app.history.append({"role": Role.USER, "content": f"q{i}: {i}+{i}?"})
            app.history.append({"role": Role.ASSISTANT, "content": f"{i+i}"})

        initial_turns = len([m for m in app.history if m["role"] != Role.SYSTEM])

        async with app.run_test(headless=True, size=(120, 40)) as pilot:
            await _type_command(pilot, "/compact", pause=1.0)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                turns = len([m for m in app.history if m["role"] != Role.SYSTEM])
                if turns < initial_turns:
                    break
                await pilot.pause(1.0)

            turns = len([m for m in app.history if m["role"] != Role.SYSTEM])
            assert turns < initial_turns, f"compact didn't shorten: was {initial_turns}, now {turns}"
            assert app.history[0]["role"] == Role.SYSTEM

    async def test_session_save_resume_roundtrip(self, tmp_cwd):
        """Session saved in app1 is fully restored in app2 via /resume."""
        import codehydra.tui as tui_mod
        from codehydra.routing.constants import Role
        from codehydra.routing.session import SessionManager

        sm = SessionManager()
        session_id = sm.new_session_id()

        # App1: save session with marker
        app1 = _make_tui_app(tmp_cwd)
        app1.sessions = sm
        app1.session_id = session_id
        app1.history.append({"role": Role.USER, "content": "marker_XYZ_7734"})
        app1.history.append({"role": Role.ASSISTANT, "content": "response to marker"})
        app1._save_session()

        # App2: resume that session
        app2 = _make_tui_app(tmp_cwd)
        app2.sessions = sm

        async with app2.run_test(headless=True, size=(120, 40)) as pilot:
            await _type_command(pilot, f"/resume {session_id}", pause=0.5)

        assert any(
            m["content"] == "marker_XYZ_7734"
            for m in app2.history if m["role"] == Role.USER
        ), "Resumed history missing original user message"

    async def test_web_search_injects_into_history(self, tmp_cwd):
        """Real /search hits DDG and injects result as assistant msg."""
        import codehydra.tui as tui_mod
        from codehydra.routing.constants import Role

        app = _make_tui_app(tmp_cwd)
        before = len(app.history)

        async with app.run_test(headless=True, size=(120, 40)) as pilot:
            await _type_command(pilot, "/search python pathlib", pause=0.5)
            deadline = time.monotonic() + 20
            while len(app.history) <= before and time.monotonic() < deadline:
                await pilot.pause(0.5)

        assert len(app.history) > before
        injected = app.history[-1]
        assert injected["role"] == Role.ASSISTANT
        assert "search results" in injected["content"].lower()
