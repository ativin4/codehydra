import unittest
from pathlib import Path
from codehydra.tools.patcher import Patcher

class TestPatcher(unittest.TestCase):
    def setUp(self):
        self.patcher = Patcher()
        self.test_file = Path("test_file.txt")
        self.test_file.write_text("line 1\nline 2\nline 3")

    def tearDown(self):
        if self.test_file.exists():
            self.test_file.unlink()

    def test_apply_patch(self):
        llm_output = "File: test_file.txt\n<<<<<<< SEARCH\nline 2\n=======\nline TWO\n>>>>>>> REPLACE"
        patched = self.patcher.apply_all_patches(llm_output)
        self.assertIn("test_file.txt", patched)
        self.assertEqual(self.test_file.read_text(), "line 1\nline TWO\nline 3")

    def test_multi_block_patch(self):
        llm_output = (
            "File: test_file.txt\n<<<<<<< SEARCH\nline 1\n=======\nline ONE\n>>>>>>> REPLACE\n"
            "File: test_file.txt\n<<<<<<< SEARCH\nline 3\n=======\nline THREE\n>>>>>>> REPLACE"
        )
        patched = self.patcher.apply_all_patches(llm_output)
        self.assertIn("test_file.txt", patched)
        self.assertEqual(self.test_file.read_text(), "line ONE\nline 2\nline THREE")

    def test_multi_file_patch(self):
        file2 = Path("file2.txt")
        file2.write_text("aaa\nbbb")
        try:
            llm_output = (
                "File: test_file.txt\n<<<<<<< SEARCH\nline 1\n=======\nSTART\n>>>>>>> REPLACE\n"
                "File: file2.txt\n<<<<<<< SEARCH\naaa\n=======\nZZZ\n>>>>>>> REPLACE"
            )
            patched = self.patcher.apply_all_patches(llm_output)
            self.assertIn("test_file.txt", patched)
            self.assertIn("file2.txt", patched)
            self.assertEqual(self.test_file.read_text(), "START\nline 2\nline 3")
            self.assertEqual(file2.read_text(), "ZZZ\nbbb")
        finally:
            if file2.exists(): file2.unlink()

    def test_search_not_found(self):
        llm_output = """
        File: test_file.txt
        <<<<<<< SEARCH
        wrong line
        =======
        line TWO
        >>>>>>> REPLACE
        """
        patched = self.patcher.apply_all_patches(llm_output)
        self.assertEqual(len(patched), 0)
        self.assertEqual(self.test_file.read_text(), "line 1\nline 2\nline 3")

if __name__ == "__main__":
    unittest.main()
