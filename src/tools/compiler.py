import shlex
import subprocess
import tomllib
from pathlib import Path
from typing import Optional, Tuple

class Compiler:
    def __init__(self, root_dir: str = "."):
        self.root_dir = Path(root_dir).resolve()
        self.config_path = self.root_dir / ".agentrc.toml"

    def get_build_command(self) -> Optional[str]:
        """Reads the build command from .agentrc.toml."""
        if not self.config_path.exists():
            return None
        
        try:
            with open(self.config_path, "rb") as f:
                config = tomllib.load(f)
                return config.get("build", {}).get("command")
        except Exception:
            return None

    def run_build(self) -> Tuple[int, str]:
        """Executes the build command and returns exit code and output (combined)."""
        cmd = self.get_build_command()
        if not cmd:
            return 0, "No build command defined in .agentrc.toml"

        try:
            result = subprocess.run(
                shlex.split(cmd),
                cwd=self.root_dir,
                capture_output=True,
                text=True
            )
            output = result.stdout + result.stderr
            return result.returncode, output
        except Exception as e:
            return 1, str(e)

if __name__ == "__main__":
    # Test
    compiler = Compiler()
    print(f"Build command: {compiler.get_build_command()}")
    code, out = compiler.run_build()
    print(f"Exit code: {code}")
    print(f"Output: {out}")
