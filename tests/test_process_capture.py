from __future__ import annotations

import argparse
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "processor"))

from process_capture import (
    CaptureName,
    CapturePair,
    CaptureSource,
    ProcessingError,
    collect_inputs,
    pair_captures,
    parse_capture_name,
    process_pair,
)
from capture_contract import hash_file, read_and_validate_capture


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


def processor_args(**overrides):
    values = {
        "offset_ms": 0.0,
        "max_offset": 3.0,
        "duration": None,
        "audio_bitrate": "192k",
        "crf": 18,
        "preset": "medium",
        "remove_raw": False,
        "replace": False,
        "failure_after": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def fake_ffmpeg(command, capture=False):
    output = Path(command[-1])
    output.write_bytes((output.name + "-generated").encode())
    return b""


def fake_media_probe(path, kind, _ffprobe):
    mime = {
        "evidence_still": "image/jpeg",
        "evidence_audio": "audio/mp4",
        "replay_video": "video/mp4",
    }[kind]
    item = {
        "kind": kind,
        "path": path.name,
        "byteSize": path.stat().st_size,
        "sha256": hash_file(path),
        "mimeType": mime,
        "codec": "mjpeg" if kind == "evidence_still" else ("aac" if kind == "evidence_audio" else "h264"),
        "container": "test",
    }
    if kind != "evidence_still":
        item["durationSeconds"] = 4.0
    if kind in {"evidence_still", "replay_video"}:
        item.update(width=1920, height=1080)
    return item


class AtomicProcessingTests(unittest.TestCase):
    def make_pair(self, root: Path) -> CapturePair:
        listener = root / "1-bank-1-listener.mp4"
        runner = root / "1-bank-1-runner.mp4"
        listener.write_bytes(b"listener-source")
        runner.write_bytes(b"runner-source")
        return CapturePair(
            CaptureName("1-bank", 1, "pair"),
            CaptureSource(listener),
            CaptureSource(runner),
        )

    def run_pair(self, pair, output, args):
        with (
            mock.patch("process_capture.probe_duration", return_value=4.0),
            mock.patch("process_capture.run", side_effect=fake_ffmpeg),
            mock.patch("process_capture.probe_media", side_effect=fake_media_probe),
        ):
            return process_pair(pair, output, args, "ffmpeg", "ffprobe")

    def test_success_writes_manifest_last_with_three_verified_media(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "daily"
            target = self.run_pair(self.make_pair(root), output, processor_args())

            self.assertEqual(
                {"listener.jpg", "listener.m4a", "replay.mp4", "capture.json"},
                {path.name for path in target.iterdir()},
            )
            manifest = read_and_validate_capture(target / "capture.json")
            self.assertEqual(1, manifest["schemaVersion"])
            self.assertEqual("1-bank/1", manifest["id"])
            self.assertEqual({"listener", "runner"}, {
                item["role"] for item in manifest["processing"]["sourceFingerprints"]
            })

    def test_every_injected_failure_leaves_no_importable_target(self) -> None:
        stages = ("evidence_still", "evidence_audio", "replay_video", "validated_media", "manifest")
        for stage in stages:
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                output = root / "daily"
                with self.assertRaisesRegex(ProcessingError, "Injected failure"):
                    self.run_pair(
                        self.make_pair(root), output, processor_args(failure_after=stage)
                    )
                self.assertFalse((output / "1-bank" / "1").exists())
                self.assertEqual([], list((output / "1-bank").glob("*.processing-*")))

    def test_failed_replace_preserves_existing_capture_hash_and_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "daily"
            pair = self.make_pair(root)
            target = output / "1-bank" / "1"
            target.mkdir(parents=True)
            old = target / "capture.json"
            old.write_bytes(b"old-capture")
            before = (hash_file(old), old.stat().st_mtime_ns)

            with self.assertRaises(ProcessingError):
                self.run_pair(
                    pair,
                    output,
                    processor_args(replace=True, failure_after="replay_video"),
                )

            self.assertEqual(before, (hash_file(old), old.stat().st_mtime_ns))
            self.assertEqual(b"old-capture", old.read_bytes())

    def test_successful_replace_retains_recoverable_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "daily"
            pair = self.make_pair(root)
            target = output / "1-bank" / "1"
            target.mkdir(parents=True)
            (target / "old.txt").write_text("old", encoding="utf-8")

            self.run_pair(pair, output, processor_args(replace=True))

            backups = list(target.parent.glob(".1.backup-*"))
            self.assertEqual(1, len(backups))
            self.assertEqual("old", (backups[0] / "old.txt").read_text(encoding="utf-8"))
            read_and_validate_capture(target / "capture.json")


if __name__ == "__main__":
    unittest.main()
