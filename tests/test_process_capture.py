from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "processor"))

from process_capture import (
    CaptureName,
    ProcessingError,
    collect_inputs,
    pair_captures,
    parse_capture_name,
)


class CaptureNamingTests(unittest.TestCase):
    def test_name_maps_to_daily_set_and_slot(self) -> None:
        self.assertEqual(
            CaptureName("1-clubhouse", 3, "listener"),
            parse_capture_name(Path("1-clubhouse-3-listener.mp4")),
        )

    def test_pairing_is_independent_of_input_order(self) -> None:
        pairs = pair_captures(
            [Path("2-oregon-2-runner.mp4"), Path("2-oregon-2-listener.mp4")]
        )
        self.assertEqual("2-oregon", pairs[0].name.mapset)
        self.assertEqual(2, pairs[0].name.slot)
        self.assertEqual("2-oregon-2-listener.mp4", pairs[0].listener.name)
        self.assertEqual("2-oregon-2-runner.mp4", pairs[0].runner.name)

    def test_missing_counterpart_fails_preflight(self) -> None:
        with self.assertRaisesRegex(ProcessingError, "Missing runner"):
            pair_captures([Path("1-bank-1-listener.mp4")])

    def test_duplicate_role_fails_preflight(self) -> None:
        with self.assertRaisesRegex(ProcessingError, "Duplicate listener"):
            pair_captures(
                [
                    Path("one/1-bank-1-listener.mp4"),
                    Path("two/1-bank-1-listener.mp4"),
                    Path("1-bank-1-runner.mp4"),
                ]
            )

    def test_zip_inputs_are_extracted_for_processing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "captures.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("player/1-bank-1-listener.mp4", b"listener")
                archive.writestr("player/notes.txt", b"ignored")

            paths, used_zip = collect_inputs([archive_path])

            self.assertTrue(used_zip)
            self.assertEqual(["1-bank-1-listener.mp4"], [path.name for path in paths])
            extracted = paths[0].materialize(root / "extracted")
            self.assertEqual(b"listener", extracted.read_bytes())

    def test_directory_inputs_include_zip_archives(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with zipfile.ZipFile(root / "captures.zip", "w") as archive:
                archive.writestr("player/1-bank-1-listener.mp4", b"listener")
                archive.writestr("player/1-bank-1-runner.mp4", b"runner")

            paths, used_zip = collect_inputs([root])
            pairs = pair_captures(paths)

            self.assertTrue(used_zip)
            self.assertEqual(1, len(pairs))
            self.assertEqual("1-bank", pairs[0].name.mapset)


if __name__ == "__main__":
    unittest.main()
