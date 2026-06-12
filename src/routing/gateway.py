import subprocess
import shutil
import os
from typing import Generator, List, Dict, Optional
from src.auth.scavenger import Scavenger
from src.routing.classifier import Classifier

class Gateway:
    MODEL_MAP = {
        "low": ["gemini/gemini-2.5-flash", "anthropic/haiku"],
        "medium": ["anthropic/sonnet", "gemini/gemini-2.5-pro", "codex/default"],
        "high": ["anthropic/opus", "gemini/gemini-3-pro-preview", "codex/default"],
    }

    # Default model used for each CLI/tier when /cli pins a backend without /model.
    CLI_DEFAULT_MODELS = {
        "claude": {"low": "haiku", "medium": "sonnet", "high": "opus"},
        "gemini": {"low": "gemini-2.5-flash", "medium": "gemini-2.5-pro", "high": "gemini-3-pro-preview"},
        "codex": {"low": "default", "medium": "default", "high": "default"},
    }

    # Flags that put each CLI in non-interactive, auto-approve-edits agentic mode.
    AGENTIC_FLAGS = {
        "claude": ["--permission-mode", "acceptEdits"],
        # --approval-mode auto_edit hangs headless on the workspace-trust
        # prompt; --yolo + --skip-trust runs non-interactively.
        "gemini": ["--yolo", "--skip-trust"],
        "codex": ["-s", "workspace-write"],
    }

    def __init__(self):
        self.scavenger = Scavenger()
        self.scavenger.apply_to_env()
        self.headers = self.scavenger.get_all_headers()
        self.active_providers = self.scavenger.get_active_providers()
        self.classifier = Classifier()

    def _build_cmd(self, cli_name: str, messages: List[Dict[str, str]], model: str):
        """Builds the subprocess argv + env for invoking a CLI with a prompt."""
        cli_path = shutil.which(cli_name)
        if not cli_path:
            raise Exception(f"'{cli_name}' CLI not found on PATH. Install it and ensure it's accessible.")

        full_prompt = ""
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            full_prompt += f"{role.upper()}:\n{content}\n\n"

        model_name = model.split("/")[-1]

        if cli_name == "gemini":
            cmd = [cli_path, "--prompt", full_prompt, "--model", model_name]
        elif cli_name == "codex":
            # codex exec uses the account's default model; explicit model
            # aliases (e.g. "default") are not valid -m values.
            cmd = [cli_path, "exec", full_prompt]
        elif cli_name == "claude":
            cmd = [cli_path, "-p", full_prompt, "--model", model_name]
        else:
            cmd = [cli_path, full_prompt]

        # Insert agentic auto-approve flags. codex's go after the "exec"
        # subcommand; the others are top-level flags.
        insert_at = 2 if cli_name == "codex" else 1
        cmd[insert_at:insert_at] = self.AGENTIC_FLAGS.get(cli_name, [])

        # Prepare a clean environment for the subprocess
        env = os.environ.copy()
        if "GOOGLE_CLOUD_PROJECT" in env:
            del env["GOOGLE_CLOUD_PROJECT"]

        return cmd, env

    @staticmethod
    def _is_noise_line(line: str) -> bool:
        """Diagnostic lines some CLIs (e.g. gemini) print to stdout."""
        return line.startswith("[ExtensionManager]") or "MCP issues detected" in line

    def _run_cli(self, cli_name: str, messages: List[Dict[str, str]], model: str) -> str:
        """Executes a local CLI (gemini, codex, claude) and returns its full output."""
        cmd, env = self._build_cmd(cli_name, messages, model)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, env=env)

            output = result.stdout.strip()
            err_output = result.stderr.strip()

            if result.returncode != 0 and not output:
                raise Exception(f"{cli_name} CLI failed: {err_output}")

            cleaned_lines = [line for line in output.split("\n") if not self._is_noise_line(line)]
            final_output = "\n".join(cleaned_lines).strip()
            if not final_output:
                raise Exception(f"{cli_name} CLI returned no usable output. STDOUT: {output} STDERR: {err_output}")
            return final_output
        except Exception as e:
            raise Exception(f"{cli_name} CLI execution error: {e}")

    def _run_cli_stream(self, cli_name: str, messages: List[Dict[str, str]], model: str) -> Generator[str, None, None]:
        """Executes a local CLI and yields its stdout incrementally, line by line."""
        cmd, env = self._build_cmd(cli_name, messages, model)
        try:
            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, bufsize=1
            )
            yielded_any = False
            try:
                for line in process.stdout:
                    if self._is_noise_line(line):
                        continue
                    yielded_any = True
                    yield line
            finally:
                process.stdout.close()
                process.wait()

            err_output = process.stderr.read().strip()
            if process.returncode != 0 and not yielded_any:
                raise Exception(f"{cli_name} CLI failed: {err_output}")
            if not yielded_any:
                raise Exception(f"{cli_name} CLI returned no usable output. STDERR: {err_output}")
        except Exception as e:
            raise Exception(f"{cli_name} CLI execution error: {e}")

    def _prioritize_models(self, models: List[str]) -> List[str]:
        """Prioritizes models/CLIs for which we have active subscriptions."""
        priority_models = []
        other_models = []
        
        for model in models:
            provider = model.split("/")[0]
            # Map model prefix to scavenger provider names
            provider_map = {
                "anthropic": "anthropic",
                "gemini": "google",
                "github": "github",
                "codex": "github"
            }
            scav_provider = provider_map.get(provider)
            if scav_provider in self.active_providers or (scav_provider == "google" and "gemini_cli" in self.active_providers):
                priority_models.append(model)
            else:
                other_models.append(model)
        
        return priority_models + other_models

    @staticmethod
    def _cli_for_model(model: str) -> str:
        """Maps a "<provider>/<model>" entry to its CLI binary name."""
        if model.startswith("gemini/"):
            return "gemini"
        elif model.startswith("github/") or model.startswith("codex/"):
            return "codex"
        elif model.startswith("anthropic/"):
            return "claude"
        raise Exception(f"Unsupported model provider in: {model}")

    def request(
        self,
        prompt: str,
        tier: Optional[str] = None,
        history: List[Dict[str, str]] = [],
        cli_override: Optional[str] = None,
        model_override: Optional[str] = None,
    ) -> str:
        """Routes request to appropriate CLI model with fallback logic.

        If cli_override is set (and not "auto"), that CLI is used directly
        with no fallback. model_override picks the exact model name for it,
        defaulting to CLI_DEFAULT_MODELS[cli_override][tier].
        """
        if tier is None:
            tier = self.classifier.evaluate(prompt)

        messages = history + [{"role": "user", "content": prompt}]

        if cli_override and cli_override != "auto":
            if cli_override not in self.CLI_DEFAULT_MODELS:
                raise Exception(f"Unknown CLI override: {cli_override}")
            model_name = model_override or self.CLI_DEFAULT_MODELS[cli_override][tier]
            return self._run_cli(cli_override, messages, f"{cli_override}/{model_name}")

        models = self.MODEL_MAP.get(tier, self.MODEL_MAP["medium"])
        models = self._prioritize_models(models)

        last_exception = None
        for model in models:
            try:
                return self._run_cli(self._cli_for_model(model), messages, model)
            except Exception as e:
                last_exception = e
                error_msg = str(e).split("\n")[0]
                print(f"Error on {model}: {error_msg}")
                continue

        raise last_exception or Exception("All CLIs failed. No active subscriptions found.")

    def request_stream(
        self,
        prompt: str,
        tier: Optional[str] = None,
        history: List[Dict[str, str]] = [],
        cli_override: Optional[str] = None,
        model_override: Optional[str] = None,
    ) -> Generator[str, None, None]:
        """Like request(), but yields output incrementally as the CLI produces it.

        Fallback to the next model only happens if the chosen CLI produces no
        output at all; once any chunk is yielded, that model is committed.
        """
        if tier is None:
            tier = self.classifier.evaluate(prompt)

        messages = history + [{"role": "user", "content": prompt}]

        if cli_override and cli_override != "auto":
            if cli_override not in self.CLI_DEFAULT_MODELS:
                raise Exception(f"Unknown CLI override: {cli_override}")
            model_name = model_override or self.CLI_DEFAULT_MODELS[cli_override][tier]
            yield from self._run_cli_stream(cli_override, messages, f"{cli_override}/{model_name}")
            return

        models = self._prioritize_models(self.MODEL_MAP.get(tier, self.MODEL_MAP["medium"]))

        last_exception = None
        for model in models:
            try:
                gen = self._run_cli_stream(self._cli_for_model(model), messages, model)
                first_chunk = next(gen)
            except StopIteration:
                continue
            except Exception as e:
                last_exception = e
                error_msg = str(e).split("\n")[0]
                print(f"Error on {model}: {error_msg}")
                continue

            yield first_chunk
            yield from gen
            return

        raise last_exception or Exception("All CLIs failed. No active subscriptions found.")


if __name__ == "__main__":
    gateway = Gateway()
    # Basic test
    try:
        res = gateway.request("Say hello", tier="low")
        print(f"Response: {res}")
    except Exception as e:
        print(f"Test failed: {e}")
