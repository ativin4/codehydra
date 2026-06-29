import os
import ast
import subprocess
from pathlib import Path
import pathspec
from typing import List, Dict, Optional

class Scanner:
    def __init__(self, root_dir: str = "."):
        self.root_dir = Path(root_dir).resolve()
        self.ignore_spec = self._load_gitignore()

    def _load_gitignore(self) -> pathspec.PathSpec:
        """Loads .gitignore and adds some default ignores."""
        ignore_patterns = [
            ".git/", "__pycache__/", "node_modules/", ".venv/", "*.pyc",
            ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx",
            "credentials.json", "secrets.json", ".secrets",
        ]
        gitignore_path = self.root_dir / ".gitignore"
        if gitignore_path.exists():
            with open(gitignore_path, "r") as f:
                ignore_patterns.extend(f.readlines())
        return pathspec.PathSpec.from_lines("gitwildmatch", ignore_patterns)

    def _get_ast_summary(self, file_path: Path) -> str:
        """Generates a compact AST summary of classes and functions."""
        if file_path.suffix != ".py":
            return ""
        
        try:
            with open(file_path, "r") as f:
                tree = ast.parse(f.read())
            
            summary = []
            for node in tree.body:
                if isinstance(node, ast.ClassDef):
                    methods = [n.name for n in node.body if isinstance(n, ast.FunctionDef)]
                    summary.append(f"class {node.name}({', '.join(methods)})")
                elif isinstance(node, ast.FunctionDef):
                    summary.append(f"def {node.name}()")
            
            return " # " + ", ".join(summary) if summary else ""
        except Exception:
            return ""

    def scan(self) -> str:
        """Crawls the filesystem and returns a text-based tree map."""
        tree = []
        for root, dirs, files in os.walk(self.root_dir):
            rel_path = Path(root).relative_to(self.root_dir)
            
            # Filter dirs in place to prevent descending into ignored ones
            dirs[:] = [d for d in dirs if not self.ignore_spec.match_file(str(rel_path / d) + "/")]
            
            if self.ignore_spec.match_file(str(rel_path) + "/"):
                continue
            
            level = len(rel_path.parts)
            indent = "  " * level
            if rel_path == Path("."):
                tree.append(f"{self.root_dir.name}/")
            else:
                tree.append(f"{indent}{rel_path.name}/")
            
            sub_indent = "  " * (level + 1)
            for f in sorted(files):
                f_rel_path = rel_path / f
                if not self.ignore_spec.match_file(str(f_rel_path)):
                    ast_info = self._get_ast_summary(self.root_dir / f_rel_path)
                    tree.append(f"{sub_indent}{f}{ast_info}")

        return "\n".join(tree)

    def get_git_context(self) -> str:
        """Returns current git branch and working-tree status."""
        try:
            branch = subprocess.run(
                ["git", "branch", "--show-current"],
                capture_output=True, text=True, cwd=self.root_dir, timeout=3,
            ).stdout.strip()
            status = subprocess.run(
                ["git", "status", "--short"],
                capture_output=True, text=True, cwd=self.root_dir, timeout=3,
            ).stdout.strip()
            if not branch and not status:
                return ""
            parts = []
            if branch:
                parts.append(f"Branch: {branch}")
            if status:
                parts.append(f"Uncommitted changes:\n{status}")
            return "\nGIT:\n" + "\n".join(parts) + "\n"
        except Exception:
            return ""

    def get_system_prompt_context(self) -> str:
        """Returns workspace structure + git state injected into the system prompt."""
        tree_map = self.scan()
        git = self.get_git_context()
        return f"\nCURRENT WORKSPACE STRUCTURE:\n```text\n{tree_map}\n```\n{git}"

if __name__ == "__main__":
    scanner = Scanner()
    print(scanner.get_system_prompt_context())
