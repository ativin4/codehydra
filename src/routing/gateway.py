import subprocess
import shutil
import os
from typing import List, Dict, Optional
from src.auth.scavenger import Scavenger
from src.routing.classifier import Classifier

class Gateway:
    MODEL_MAP = {
        "low": ["gemini/gemini-2.5-flash", "anthropic/haiku"],
        "medium": ["anthropic/sonnet", "gemini/gemini-2.5-pro", "codex/default"],
        "high": ["anthropic/opus", "gemini/gemini-3-pro-preview", "codex/default"],
    }

    def __init__(self):
        self.scavenger = Scavenger()
        self.scavenger.apply_to_env()
        self.headers = self.scavenger.get_all_headers()
        self.active_providers = self.scavenger.get_active_providers()
        self.classifier = Classifier()

    def _run_cli(self, cli_name: str, messages: List[Dict[str, str]], model: str) -> str:
        """Executes a local CLI (gemini, codex, claude) directly as a subprocess."""
        cli_path = shutil.which(cli_name)
        if not cli_path:
            raise Exception(f"'{cli_name}' CLI not found on PATH. Install it and ensure it's accessible.")


        full_prompt = ""
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            full_prompt += f"{role.upper()}:\n{content}\n\n"
            
        try:
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
            
            # Prepare a clean environment for the subprocess
            env = os.environ.copy()
            if "GOOGLE_CLOUD_PROJECT" in env:
                del env["GOOGLE_CLOUD_PROJECT"]
                
            # Capture both streams
            result = subprocess.run(cmd, capture_output=True, text=True, env=env)
            
            output = result.stdout.strip()
            err_output = result.stderr.strip()
            
            if result.returncode != 0 and not output:
                raise Exception(f"{cli_name} CLI failed: {err_output}")
            
            # Clean up CLI diagnostic output (specifically for gemini/caveman)
            cleaned_lines = []
            for line in output.split('\n'):
                if line.startswith("[ExtensionManager]") or "MCP issues detected" in line:
                    continue
                cleaned_lines.append(line)
                
            final_output = "\n".join(cleaned_lines).strip()
            if not final_output:
                raise Exception(f"{cli_name} CLI returned no usable output. STDOUT: {output} STDERR: {err_output}")
            return final_output
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

    def request(self, prompt: str, tier: Optional[str] = None, history: List[Dict[str, str]] = []) -> str:
        """Routes request to appropriate CLI model with fallback logic."""
        if tier is None:
            tier = self.classifier.evaluate(prompt)
        
        models = self.MODEL_MAP.get(tier, self.MODEL_MAP["medium"])
        models = self._prioritize_models(models)
        
        messages = history + [{"role": "user", "content": prompt}]

        last_exception = None
        for model in models:
            try:
                # Dispatch directly to the local CLI matching this model's provider
                if model.startswith("gemini/"):
                    return self._run_cli("gemini", messages, model)
                elif model.startswith("github/") or model.startswith("codex/"):
                    return self._run_cli("codex", messages, model)
                elif model.startswith("anthropic/"):
                    return self._run_cli("claude", messages, model)
                else:
                    raise Exception(f"Unsupported model provider in: {model}")
            except Exception as e:
                last_exception = e
                error_msg = str(e).split("\n")[0]
                print(f"Error on {model}: {error_msg}")
                continue
        
        raise last_exception or Exception("All CLIs failed. No active subscriptions found.")


if __name__ == "__main__":
    gateway = Gateway()
    # Basic test
    try:
        res = gateway.request("Say hello", tier="low")
        print(f"Response: {res}")
    except Exception as e:
        print(f"Test failed: {e}")
