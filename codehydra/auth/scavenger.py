import os
import sys
import json
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

class Scavenger:
    def __init__(self):
        self.home = Path.home()

    def get_claude_token(self) -> Optional[str]:
        """Extracts Claude Code token from ~/.claude/.credentials.json or Keychain."""
        # Try filesystem first
        paths = [
            self.home / ".claude" / ".credentials.json",
            self.home / ".claude" / "session.json",
        ]
        for cred_path in paths:
            if cred_path.exists():
                try:
                    with open(cred_path, "r") as f:
                        data = json.load(f)
                        token = data.get("accessToken") or data.get("token") or data.get("sessionToken")
                        if token:
                            return token
                except Exception:
                    pass

        # Try macOS Keychain
        if sys.platform == "darwin":
            try:
                # Claude Code might store tokens in Keychain
                result = subprocess.run(
                    ["security", "find-generic-password", "-s", "claude-code", "-w"],
                    capture_output=True,
                    text=True,
                    check=False
                )
                if result.returncode == 0:
                    return result.stdout.strip()
            except Exception:
                pass

        return None

    def get_copilot_token(self) -> Optional[str]:
        """Extracts GitHub Copilot token from hosts.json or 'gh auth token'."""
        paths = [
            self.home / ".config" / "gh" / "hosts.yml",
            self.home / ".config" / "github-copilot" / "hosts.json",
            self.home / ".copilot" / "config.json",
        ]

        for path in paths:
            if path.exists():
                try:
                    if path.suffix == ".yml" or path.suffix == ".yaml":
                        # Basic YAML parsing without external lib for just the token
                        with open(path, "r") as f:
                            content = f.read()
                            import re
                            match = re.search(r"oauth_token:\s*(\S+)", content)
                            if match:
                                return match.group(1)
                    else:
                        with open(path, "r") as f:
                            data = json.load(f)
                            if "github.com" in data:
                                return data["github.com"].get("oauth_token")
                            return data.get("oauth_token") or data.get("token")
                except Exception:
                    continue

        # Fallback to 'gh' CLI
        try:
            result = subprocess.run(
                ["gh", "auth", "token"],
                capture_output=True,
                text=True,
                check=False
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass

        return None

    def get_google_headers(self) -> Dict[str, str]:
        """Extracts Google ADC headers."""
        try:
            import google.auth
            from google.auth.transport.requests import Request

            credentials, project = google.auth.default()
            if not credentials.valid:
                credentials.refresh(Request())
            
            headers = {}
            credentials.apply(headers)
            if project and "x-goog-user-project" not in headers:
                headers["x-goog-user-project"] = project
            return headers
        except Exception:
            return {}

    def get_agy_cli_token(self) -> Optional[str]:
        """Extracts token from Agy CLI (~/.agy/oauth_creds.json)."""
        # Agy CLI uses this path for its session tokens
        cred_path = self.home / ".agy" / "oauth_creds.json"
        if cred_path.exists():
            try:
                with open(cred_path, "r") as f:
                    data = json.load(f)
                    # Agy CLI usually stores the access token directly
                    return data.get("access_token") or data.get("token")
            except Exception:
                pass
        return None

    def get_active_providers(self) -> List[str]:
        """Returns a list of providers with active subscriptions."""
        providers = []
        if self.get_claude_token(): providers.append("anthropic")
        if self.get_copilot_token(): providers.append("github")
        if self.get_google_headers(): providers.append("google")
        if self.get_agy_cli_token(): providers.append("agy_cli")
        return providers

    def get_all_headers(self) -> Dict[str, str]:
        """Aggregates all found credentials into a header dictionary."""
        headers = {}
        
        agy_token = self.get_agy_cli_token()
        if agy_token:
            headers["authorization"] = f"Bearer {agy_token}"

        claude_token = self.get_claude_token()
        if claude_token:
            headers["anthropic-session-token"] = claude_token

        copilot_token = self.get_copilot_token()
        if copilot_token:
            headers["github-copilot-token"] = copilot_token

        google_hdrs = self.get_google_headers()
        headers.update(google_hdrs)

        return headers

    def get_cli_auth_status(self) -> Dict[str, bool]:
        """Checks whether each underlying CLI (claude, agy, codex) is logged in."""
        status = {"claude": False, "agy": False, "codex": False}

        if shutil.which("claude"):
            try:
                result = subprocess.run(
                    ["claude", "auth", "status"], capture_output=True, text=True, check=False
                )
                data = json.loads(result.stdout)
                status["claude"] = bool(data.get("loggedIn"))
            except Exception:
                pass

        if shutil.which("codex"):
            try:
                result = subprocess.run(
                    ["codex", "login", "status"], capture_output=True, text=True, check=False
                )
                combined = (result.stdout + result.stderr).lower()
                status["codex"] = result.returncode == 0 and "logged in" in combined
            except Exception:
                pass

        if shutil.which("agy"):
            status["agy"] = self.get_agy_cli_token() is not None

        return status

    def apply_to_env(self):
        """Applies scavenged credentials to environment variables for tools that expect them."""
        # For Google ADC, if we have a project ID, set it
        hdrs = self.get_google_headers()
        if "x-goog-user-project" in hdrs:
            os.environ["GOOGLE_CLOUD_PROJECT"] = hdrs["x-goog-user-project"]
        
        # We don't want to set ANTHROPIC_API_KEY if we are using session tokens
        # but some tools might need a dummy key to bypass initial checks
        # os.environ["ANTHROPIC_API_KEY"] = "sk-ant-session-bypass"

if __name__ == "__main__":
    import sys as _sys
    scavenger = Scavenger()
    if "--dump-tokens" in _sys.argv:
        print(json.dumps(scavenger.get_all_headers(), indent=2))
    else:
        print(f"Active providers: {scavenger.get_active_providers()}")
        print("Pass --dump-tokens to print raw credential headers.")
