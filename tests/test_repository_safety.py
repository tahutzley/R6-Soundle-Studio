from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_EXAMPLES = (
    ".env",
    "config.local.json",
    "studio.db",
    "studio.db-wal",
    "daily sets/private-capture/capture.json",
    "videos/private-recording.mp4",
    "media/raw/private-recording.mp4",
    "media/processed/private-output.mp4",
)
PRIVATE_ROOTS = (
    ".env",
    "config.local.json",
    "studio.db",
    "daily sets/",
    "videos/",
    "media/raw/",
    "media/processed/",
)


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


class RepositorySafetyTests(unittest.TestCase):
    def test_private_paths_are_ignored_and_untracked(self) -> None:
        tracked = [
            line.strip().replace("\\", "/")
            for line in git("ls-files").stdout.splitlines()
            if line.strip()
        ]
        for private_root in PRIVATE_ROOTS:
            with self.subTest(path=private_root):
                self.assertFalse(
                    any(
                        path == private_root.rstrip("/") or path.startswith(private_root)
                        for path in tracked
                    ),
                    f"private Studio state is tracked under {private_root}",
                )
        for relative_path in PRIVATE_EXAMPLES:
            with self.subTest(ignore=relative_path):
                ignored = git(
                    "check-ignore", "--no-index", "--quiet", "--", relative_path
                )
                self.assertEqual(ignored.returncode, 0, "private path is not ignored")

    def test_safety_automation_is_present(self) -> None:
        for relative_path in (".gitleaks.toml", ".github/workflows/security.yml"):
            with self.subTest(path=relative_path):
                self.assertTrue((ROOT / relative_path).is_file())


if __name__ == "__main__":
    unittest.main()
