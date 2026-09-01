from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "processor"))

from capture_contract import CaptureValidationError, hash_file, validate_capture
from index_captures import IndexingError, index_legacy_root


def fake_probe(path: Path, kind: str, _ffprobe: str) -> dict[str, object]:
    mime = {
        "evidence_still": "image/jpeg",
        "evidence_audio": "audio/mp4",
        "replay_video": "video/mp4",
    }[kind]
    item: dict[str, object] = {
        "kind": kind,
        "path": path.name,
        "byteSize": path.stat().st_size,
        "sha256": hash_file(path),
        "mimeType": mime,
        "codec": "mjpeg" if kind == "evidence_still" else ("aac" if kind == "evidence_audio" else "h264"),
        "container": "fixture",
    }
    if kind != "evidence_still":
        item["durationSeconds"] = 2.0
    if kind in {"evidence_still", "replay_video"}:
        item.update(width=1280, height=720)
    return item


def make_legacy_tree(root: Path, map_set: str = "1-bank") -> None:
    for slot in (1, 2, 3):
        directory = root / map_set / str(slot)
        directory.mkdir(parents=True)
        for name in ("listener.jpg", "listener.m4a", "replay.mp4"):
            (directory / name).write_bytes(f"{map_set}/{slot}/{name}".encode())


def manifest_for(directory: Path) -> dict:
    media = [
        fake_probe(directory / "listener.jpg", "evidence_still", "fixture"),
        fake_probe(directory / "listener.m4a", "evidence_audio", "fixture"),
        fake_probe(directory / "replay.mp4", "replay_video", "fixture"),
    ]
    return {
        "schemaVersion": 1,
        "id": "1-bank/1",
        "source": {
            "mapSet": "1-bank",
            "mapSlug": "bank",
            "setNumber": 1,
            "slot": 1,
            "listenerSourceName": "1-bank-1-listener.mp4",
            "runnerSourceName": "1-bank-1-runner.mp4",
        },
        "evidence": {"stillPath": "listener.jpg", "audioPath": "listener.m4a"},
        "replay": {"videoPath": "replay.mp4"},
        "alignment": {
            "runnerOffsetMs": 12.5,
            "confidence": 0.9,
            "correlationMethod": "audio-envelope-v1",
            "measuredAt": "2026-08-31T12:00:00+00:00",
        },
        "durationSeconds": 2.0,
        "media": media,
        "processing": {
            "processingVersion": 1,
            "processorVersion": "fixture/1",
            "settings": {"alignment": "audio-envelope-v1"},
            "processedAt": "2026-08-31T12:00:01+00:00",
            "sourceFingerprints": [
                {"role": "listener", "name": "listener.mp4", "byteSize": 1, "sha256": "a" * 64},
                {"role": "runner", "name": "runner.mp4", "byteSize": 1, "sha256": "b" * 64},
            ],
        },
        "review": {"status": "needs_review"},
    }


class CaptureContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        make_legacy_tree(self.root)
        self.directory = self.root / "1-bank" / "1"
        self.manifest = manifest_for(self.directory)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_valid_manifest_checks_schema_semantics_and_bytes(self) -> None:
        validate_capture(self.manifest, self.directory)

    def test_checked_in_contract_fixtures(self) -> None:
        fixture_root = ROOT / "tests" / "fixtures" / "contracts"
        valid = json.loads((fixture_root / "capture-v1-valid.json").read_text(encoding="utf-8"))
        invalid = json.loads((fixture_root / "capture-v1-invalid.json").read_text(encoding="utf-8"))
        validate_capture(valid)
        with self.assertRaises(CaptureValidationError):
            validate_capture(invalid)

    def test_rejects_traversal_absolute_duplicate_bad_hash_mime_and_credentials(self) -> None:
        mutations = []
        traversal = copy.deepcopy(self.manifest)
        traversal["evidence"]["stillPath"] = "../listener.jpg"
        mutations.append(traversal)
        absolute = copy.deepcopy(self.manifest)
        absolute["replay"]["videoPath"] = "C:\\private\\replay.mp4"
        mutations.append(absolute)
        duplicate = copy.deepcopy(self.manifest)
        duplicate["media"][1]["kind"] = "evidence_still"
        mutations.append(duplicate)
        bad_hash = copy.deepcopy(self.manifest)
        bad_hash["media"][0]["sha256"] = "0" * 64
        mutations.append(bad_hash)
        bad_mime = copy.deepcopy(self.manifest)
        bad_mime["media"][2]["mimeType"] = "image/jpeg"
        mutations.append(bad_mime)
        credential = copy.deepcopy(self.manifest)
        credential["processing"]["settings"]["apiKey"] = "do-not-store"
        mutations.append(credential)
        for mutation in mutations:
            with self.subTest(index=mutations.index(mutation)):
                with self.assertRaises(CaptureValidationError):
                    validate_capture(mutation, self.directory)

    def test_rejects_media_symlink(self) -> None:
        link = self.directory / "listener.jpg"
        link.unlink()
        outside = self.root / "outside.jpg"
        outside.write_bytes(b"outside")
        try:
            link.symlink_to(outside)
        except OSError as error:
            self.skipTest(f"Symlink creation is unavailable: {error}")
        self.manifest["media"][0]["byteSize"] = outside.stat().st_size
        self.manifest["media"][0]["sha256"] = hash_file(outside)
        with self.assertRaisesRegex(CaptureValidationError, "symlink"):
            validate_capture(self.manifest, self.directory)

    def test_symlink_guard_is_enforced_without_platform_privileges(self) -> None:
        marked = self.directory / "listener.jpg"
        original = Path.is_symlink

        def reports_marked_path(path: Path) -> bool:
            return path == marked or original(path)

        with mock.patch.object(Path, "is_symlink", reports_marked_path):
            with self.assertRaisesRegex(CaptureValidationError, "symlink"):
                validate_capture(self.manifest, self.directory)


class LegacyIndexerTests(unittest.TestCase):
    def inventory(self, root: Path):
        return {
            path.relative_to(root).as_posix(): (hash_file(path), path.stat().st_mtime_ns)
            for path in root.rglob("*")
            if path.is_file()
        }

    def test_dry_run_changes_nothing_then_write_only_adds_manifests_and_repeats_noop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_legacy_tree(root)
            before = self.inventory(root)

            self.assertEqual((3, 0), index_legacy_root(root))
            self.assertEqual(before, self.inventory(root))
            self.assertEqual(
                (3, 0),
                index_legacy_root(root, write_manifests=True, ffprobe="fixture", probe=fake_probe),
            )
            after = self.inventory(root)
            added = set(after) - set(before)
            self.assertEqual(
                {f"1-bank/{slot}/capture.json" for slot in (1, 2, 3)},
                added,
            )
            self.assertEqual(before, {key: after[key] for key in before})
            self.assertEqual(
                (0, 3),
                index_legacy_root(root, write_manifests=True, ffprobe="fixture", probe=fake_probe),
            )

    def test_preflight_rejects_partial_extra_and_unexpected_directories(self) -> None:
        cases = ("partial", "extra", "slot")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                make_legacy_tree(root)
                if case == "partial":
                    (root / "1-bank" / "2" / "listener.m4a").unlink()
                elif case == "extra":
                    (root / "1-bank" / "2" / "listener.wav").write_bytes(b"extra")
                else:
                    (root / "1-bank" / "4").mkdir()
                with self.assertRaises(IndexingError):
                    index_legacy_root(root)

    def test_conflicting_existing_manifest_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_legacy_tree(root)
            manifest = manifest_for(root / "1-bank" / "1")
            manifest["id"] = "1-bank/2"
            manifest["source"]["slot"] = 2
            (root / "1-bank" / "1" / "capture.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            with self.assertRaisesRegex(IndexingError, "conflicts"):
                index_legacy_root(root)

    def test_probe_failure_writes_no_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_legacy_tree(root)

            def fails_on_second_slot(path, kind, ffprobe):
                if path.parent.name == "2" and kind == "evidence_audio":
                    raise RuntimeError("injected probe failure")
                return fake_probe(path, kind, ffprobe)

            with self.assertRaisesRegex(RuntimeError, "injected probe failure"):
                index_legacy_root(
                    root,
                    write_manifests=True,
                    ffprobe="fixture",
                    probe=fails_on_second_slot,
                )
            self.assertEqual([], list(root.rglob("capture.json")))


if __name__ == "__main__":
    unittest.main()
