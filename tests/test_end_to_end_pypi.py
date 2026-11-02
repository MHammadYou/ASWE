import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "check_requirements.py"
SAMPLES = Path(__file__).resolve().parent / "samples"


def run_script(*arguments):
    return subprocess.run([sys.executable, str(SCRIPT), *arguments], capture_output=True, text=True, timeout=120)


class LivePyPITests(unittest.TestCase):
    def test_clean_file_with_null_and_empty_requires_python(self):
        completed = run_script(str(SAMPLES / "all_compatible.txt"))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            completed.stdout.splitlines(),
            [
                "Lines in file: 4",
                "Requirements checked: 4",
                "Not found on PyPI: 0",
                "Incompatible with Python 3.11: 0",
                "Highest requirement: requests==2.31.0",
            ],
        )

    def test_file_with_every_kind_of_problem(self):
        completed = run_script(str(SAMPLES / "with_problems.txt"))
        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertEqual(
            completed.stdout.splitlines(),
            [
                "Lines in file: 12",
                "Requirements checked: 7",
                "Not found on PyPI: 2",
                "  this-package-does-not-exist-xyz123==1.0 - package does not exist",
                "  requests==99.99.99 - pinned version does not exist",
                "Incompatible with Python 3.11: 2",
                "  scipy==1.7.3 - requires_python >=3.7,<3.11",
                "  Django==6.0 - requires_python >=3.12",
                "Highest requirement: Django==6.0",
                "Exists but unpinned, no release to check: 1",
                "  flask",
                "Skipped lines: 2",
                "  -r other.txt - pip option",
                "  foo @ https://example.com/foo.zip - direct URL",
            ],
        )

    def test_highest_is_numeric_not_alphabetical_and_ignores_exclusions(self):
        completed = run_script(str(SAMPLES / "version_ordering.txt"))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Highest requirement: django==5.2", completed.stdout.splitlines())

    def test_utf16_file_from_powershell_redirect(self):
        with tempfile.TemporaryDirectory() as directory:
            requirements = Path(directory) / "requirements.txt"
            requirements.write_bytes("flask==2.0\r\nrequests==2.31.0\r\n".encode("utf-16"))
            completed = run_script(str(requirements))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Lines in file: 2", completed.stdout)
        self.assertIn("Highest requirement: requests==2.31.0", completed.stdout)

    def test_missing_file_reports_error(self):
        completed = run_script(str(SAMPLES / "does_not_exist.txt"))
        self.assertEqual(completed.returncode, 2)
        self.assertIn("Cannot read", completed.stderr)
        self.assertEqual(completed.stdout, "")


if __name__ == "__main__":
    unittest.main()
