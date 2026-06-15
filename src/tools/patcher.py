import logging
import re
from pathlib import Path
from typing import List, Tuple

class Patcher:
    def __init__(self):
        # Regex to find SEARCH/REPLACE blocks and the preceding filename
        # Expecting format: File: path/to/file\n<<<<<<< SEARCH...
        self.file_block_re = re.compile(
            r"File:\s*(.*?)\s*\n<<<<<<< SEARCH\n(.*?)\n=======\n(.*?)\n>>>>>>> REPLACE",
            re.DOTALL
        )

    def apply_all_patches(self, content: str, cwd: Path | None = None) -> List[str]:
        """Parses all patches from LLM response and applies them. Returns list of patched files."""
        cwd = (cwd or Path.cwd()).resolve()
        matches = self.file_block_re.findall(content)
        patched_files = []

        for file_path, search, replace in matches:
            file_path = file_path.strip()
            path = (cwd / file_path).resolve()
            if not path.is_relative_to(cwd):
                logging.warning("Rejected path outside workspace: %s", file_path)
                continue
            if not path.exists():
                print(f"File not found: {file_path}")
                continue

            with open(path, "r") as f:
                file_content = f.read()

            if search in file_content:
                new_content = file_content.replace(search, replace, 1)
                with open(path, "w") as f:
                    f.write(new_content)
                patched_files.append(file_path)
            else:
                print(f"Search block not found in {file_path}")

        return list(set(patched_files))

if __name__ == "__main__":
    # Simple test
    test_file = Path("test_patch.txt")
    test_file.write_text("line 1\nline 2\nline 3")
    
    patcher = Patcher()
    llm_output = """
    I will fix the file now.
    <<<<<<< SEARCH
    line 2
    =======
    line TWO (fixed)
    >>>>>>> REPLACE
    """
    
    if patcher.apply_patches(llm_output, "test_patch.txt"):
        print("Patch applied successfully!")
        print(f"New content:\n{test_file.read_text()}")
    else:
        print("Patch failed.")
    
    test_file.unlink()
