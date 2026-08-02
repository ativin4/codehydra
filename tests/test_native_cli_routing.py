"""Contract tests and opt-in live evaluations for native backend routing.

These tests deliberately inspect argv instead of mocking a model response: the
underlying CLIs are the contract CodeHydra must preserve.
"""

import os
import subprocess
from unittest.mock import patch

import pytest

from codehydra.routing.constants import CLI, Mode, Tier
from codehydra.routing.gateway import Gateway


def _gateway() -> Gateway:
    gateway = Gateway.__new__(Gateway)
    gateway._CONTEXT_WINDOW = 30
    gateway.mcp_servers = {}
    gateway.claude_mcp_config_path = None
    gateway.priority = None
    gateway.cli_auth_status = {}
    gateway.cli_default_models = {
        cli: dict(models) for cli, models in Gateway.CLI_DEFAULT_MODELS.items()
    }
    return gateway


def _build(cli: CLI, model: str, *, tier: Tier, mode: Mode = Mode.YOLO) -> list[str]:
    gateway = _gateway()
    with patch("shutil.which", return_value=f"/usr/bin/{cli}"):
        cmd, _ = gateway._build_cmd(
            cli,
            [{"role": "user", "content": "Reply exactly: OK"}],
            model,
            mode=mode,
            tier=tier,
        )
    return cmd


class TestNativeBackendArguments:
    def test_claude_forwards_model_effort_and_plan_mode(self):
        cmd = _build(
            CLI.CLAUDE, "anthropic/sonnet", tier=Tier.HIGH, mode=Mode.PLAN,
        )

        assert cmd[:2] == ["/usr/bin/claude", "--permission-mode"]
        assert "plan" in cmd
        assert cmd[cmd.index("--model") + 1] == "sonnet"
        assert cmd[cmd.index("--effort") + 1] == Tier.HIGH

    def test_agy_forwards_model_effort_and_plan_mode(self):
        cmd = _build(
            CLI.AGY, "agy/gemini-3.1-pro-high", tier=Tier.HIGH, mode=Mode.PLAN,
        )

        assert cmd[:3] == ["/usr/bin/agy", "--mode", "plan"]
        assert cmd[cmd.index("--model") + 1] == "gemini-3.1-pro-high"
        assert cmd[cmd.index("--effort") + 1] == Tier.HIGH

    def test_codex_forwards_explicit_model_effort_and_plan_mode(self):
        cmd = _build(
            CLI.CODEX, "codex/gpt-5.4", tier=Tier.HIGH, mode=Mode.PLAN,
        )

        assert cmd[:4] == ["/usr/bin/codex", "exec", "-s", "read-only"]
        assert cmd[cmd.index("--model") + 1] == "gpt-5.4"
        effort_config = cmd[cmd.index("-c") + 1]
        assert effort_config == 'model_reasoning_effort="high"'

    def test_codex_default_uses_account_model_but_still_forwards_effort(self):
        cmd = _build(CLI.CODEX, "codex/default", tier=Tier.LOW)

        assert "--model" not in cmd
        assert cmd[cmd.index("-c") + 1] == 'model_reasoning_effort="low"'

    def test_persistent_claude_session_receives_effort(self):
        gateway = _gateway()
        gateway._claude_session = None
        gateway._claude_history_len = 0
        gateway._cancel_event = None
        captured = {}

        class FakeClaudeSession:
            last_result_text = ""

            def __init__(self, *args, **kwargs):
                captured["args"] = args
                captured["kwargs"] = kwargs

            def alive(self):
                return True

            def send(self, prompt, cancel_event=None):
                yield "OK", None

            def close(self):
                pass

        with patch("codehydra.routing.gateway.ClaudeSession", FakeClaudeSession):
            assert list(gateway._run_claude_persistent_stream(
                [{"role": "user", "content": "Reply exactly: OK"}],
                "anthropic/sonnet",
                Tier.HIGH,
                Mode.PLAN,
            )) == ["OK"]

        assert captured["args"][:2] == ("sonnet", Gateway.MODE_FLAGS[Mode.PLAN][CLI.CLAUDE])
        assert captured["kwargs"]["effort"] == Tier.HIGH


class TestExplicitModelResolution:
    def test_qualified_model_pins_its_own_backend(self):
        gateway = _gateway()

        assert gateway._resolve_cli_and_model(None, "codex/gpt-5.4", Tier.MEDIUM) == (
            CLI.CODEX,
            "codex/gpt-5.4",
        )

    def test_bare_model_requires_a_pinned_backend(self):
        with pytest.raises(Exception, match="bare /model needs a pinned /cli"):
            _gateway()._resolve_cli_and_model(None, "gpt-5.4", Tier.MEDIUM)

    def test_qualified_model_cannot_disagree_with_pinned_backend(self):
        with pytest.raises(Exception, match="cannot be used with /cli claude"):
            _gateway()._resolve_cli_and_model(CLI.CLAUDE, "codex/gpt-5.4", Tier.MEDIUM)


class TestRoutingMaps:
    def test_default_agy_models_are_in_the_current_native_catalog(self):
        defaults = Gateway.CLI_DEFAULT_MODELS[CLI.AGY]
        assert defaults == {
            Tier.LOW: "gemini-3.6-flash-low",
            Tier.MEDIUM: "gemini-3.6-flash-medium",
            Tier.HIGH: "gemini-3.1-pro-high",
        }

    def test_priority_accepts_cli_names_from_user_config(self):
        gateway = _gateway()
        gateway.priority = [
            Gateway.PRIORITY_ALIASES["codex"],
            Gateway.PRIORITY_ALIASES["claude"],
            Gateway.PRIORITY_ALIASES["agy"],
        ]

        assert gateway._prioritize_models([
            "anthropic/sonnet",
            "agy/gemini-3.6-flash-medium",
            "codex/default",
        ]) == [
            "codex/default",
            "anthropic/sonnet",
            "agy/gemini-3.6-flash-medium",
        ]


@pytest.mark.live
class TestLiveCodexAgenticEval:
    """Opt-in end-to-end check for CodeHydra's native Codex pathway.

    Run with CODEHYDRA_LIVE_EVALS=1. This deliberately exercises the same
    streaming + yolo path the TUI uses, but confines agent edits to pytest's
    disposable git repository.
    """

    @pytest.fixture(autouse=True)
    def require_live_codex(self):
        from codehydra.auth.scavenger import Scavenger

        if os.environ.get("CODEHYDRA_LIVE_EVALS") != "1":
            pytest.skip("set CODEHYDRA_LIVE_EVALS=1 to run cloud CLI evaluations")
        if not Scavenger().get_cli_auth_status().get(CLI.CODEX):
            pytest.skip("requires an authenticated codex CLI session")

    def test_high_effort_stream_can_edit_workspace(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("CODEHYDRA_ENABLE_SUBAGENTS", "0")
        subprocess.run(["git", "init", "-q"], check=True)

        model_override = os.environ.get("CODEHYDRA_LIVE_EVAL_CODEX_MODEL")
        gateway = Gateway()
        response = "".join(gateway.request_stream(
            "Create native_codex_eval.txt in the current repository containing "
            "exactly CODEHYDRA_NATIVE_CODEX_EVAL followed by one newline. "
            "Use your tools to make the edit. Do not modify any other file. "
            "When finished, reply exactly DONE.",
            tier=Tier.HIGH,
            cli_override=CLI.CODEX,
            model_override=model_override,
            mode=Mode.YOLO,
        ))
        gateway.close()

        assert (tmp_path / "native_codex_eval.txt").read_text() == "CODEHYDRA_NATIVE_CODEX_EVAL\n"
        assert "DONE" in response
        assert gateway.last_usage is not None
        assert gateway.last_usage["cli"] == CLI.CODEX
        assert gateway.last_usage["model"] == f"codex/{model_override or 'default'}"
        assert gateway.last_usage["tier"] == Tier.HIGH
