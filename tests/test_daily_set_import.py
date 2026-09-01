from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

from studio_import import DailySetImportError, DailySetImporter, ScanChangedError
from studio_server import App, Server, Store
from tests.capture_fixtures import write_capture, write_daily_set


def catalog() -> dict:
    return {
        "assetVersion": "test-map-assets",
        "maps": [{"slug": "bank", "name": "Bank", "floors": [{"key": "1f"}]}],
        "operators": [{"id": "vigil", "name": "Vigil"}],
    }


class DailySetImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.import_root = self.root / "daily sets"
        self.import_root.mkdir()
        self.store = Store(self.root / "studio.db")
        self.importer = DailySetImporter(self.store, [self.import_root], catalog)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def counts(self) -> tuple[int, int]:
        with self.store.connect() as connection:
            return (
                connection.execute("SELECT COUNT(*) FROM captures").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM puzzle_sets").fetchone()[0],
            )

    def complete_set(self, item: dict) -> dict:
        point = {"x": 0.4, "y": 0.6, "floorKey": "1f"}
        for round_item in item["rounds"]:
            round_item.update(
                operatorId="vigil",
                listenerPos={**point, "angle": 90},
                operatorStartPos=point,
                targetPos={**point, "x": 0.7},
            )
        return item

    def test_exactly_three_rounds_import_as_one_draft_transaction(self) -> None:
        directory = write_daily_set(self.import_root)

        scan = self.importer.scan(directory.name)
        result = self.importer.commit(scan["scanId"])

        self.assertEqual("valid", scan["state"])
        self.assertEqual((3, 1), self.counts())
        self.assertEqual(["1-bank/1", "1-bank/2", "1-bank/3"], [item["id"] for item in result["captures"]])
        self.assertEqual([1, 2, 3], [item["position"] for item in result["set"]["rounds"]])
        self.assertEqual(["1-bank/1", "1-bank/2", "1-bank/3"], [item["captureId"] for item in result["set"]["rounds"]])
        self.assertEqual("1-bank", result["set"]["importedMapSet"])
        self.assertTrue(all(item["status"] == "available" for item in result["captures"]))

    def test_zero_two_and_four_round_directories_create_no_rows(self) -> None:
        cases = []
        empty = self.import_root / "1-bank"
        empty.mkdir()
        cases.append((empty, "empty"))
        cases.append((write_daily_set(self.import_root, slots=(1, 2), set_number=2), "partially_invalid"))
        four = write_daily_set(self.import_root, set_number=3)
        (four / "4").mkdir()
        cases.append((four, "partially_invalid"))

        for directory, expected_state in cases:
            with self.subTest(directory=directory.name):
                report = self.importer.scan(directory)
                self.assertEqual(expected_state, report["state"])
                self.assertFalse(report["canCommit"])
                with self.assertRaisesRegex(DailySetImportError, "Resolve"):
                    self.importer.commit(report["scanId"])
        self.assertEqual((0, 0), self.counts())

    def test_processor_backup_is_ignored_but_unknown_round_files_are_rejected(self) -> None:
        directory = write_daily_set(self.import_root)
        (directory / ".2.backup-2026-08-31T120000.000Z-deadbeef").mkdir()
        report = self.importer.scan(directory)
        self.assertEqual("valid", report["state"])
        self.assertEqual(
            [".2.backup-2026-08-31T120000.000Z-deadbeef"],
            report["source"]["ignoredBackups"],
        )

        (directory / "3" / "notes.txt").write_text("not canonical", encoding="utf-8")
        invalid = self.importer.scan(directory)
        self.assertEqual("partially_invalid", invalid["state"])
        self.assertIn("unexpected round file", json.dumps(invalid))
        self.assertEqual((0, 0), self.counts())

    def test_unknown_capture_operator_is_rejected(self) -> None:
        directory = write_daily_set(self.import_root)
        write_capture(directory, 1, operator_id="unknown-operator")

        report = self.importer.scan(directory)

        self.assertEqual("partially_invalid", report["state"])
        self.assertIn("current operator catalog", json.dumps(report))
        self.assertEqual((0, 0), self.counts())

    def test_legacy_media_reports_one_set_preparation_without_database_writes(self) -> None:
        directory = write_daily_set(self.import_root)
        for slot in (1, 2, 3):
            (directory / str(slot) / "capture.json").unlink()

        report = self.importer.scan("1-bank")

        self.assertEqual("legacy_unindexed", report["state"])
        self.assertFalse(report["canCommit"])
        self.assertEqual(
            'python processor\\index_captures.py --root "daily sets\\1-bank" --write-manifests',
            report["legacyIndexCommand"],
        )
        self.assertTrue(all(slot["state"] == "legacy" for slot in report["slots"]))
        self.assertTrue(all(not slot["manualFields"] for slot in report["slots"]))
        self.assertEqual((0, 0), self.counts())

    def test_bad_map_checksum_duration_and_duplicate_identity_are_rejected(self) -> None:
        bad_map = write_daily_set(self.import_root, map_slug="unknown", set_number=4)
        checksum = write_daily_set(self.import_root, set_number=5)
        checksum_audio = checksum / "2" / "listener.m4a"
        checksum_audio.write_bytes(b"x" * checksum_audio.stat().st_size)
        duration = write_daily_set(self.import_root, set_number=6)
        write_capture(duration, 2, set_number=6, duration=4.2, replay_duration=8.0)
        duplicate = write_daily_set(self.import_root, set_number=7)
        duplicate_manifest = json.loads((duplicate / "2" / "capture.json").read_text(encoding="utf-8"))
        duplicate_manifest["id"] = "7-bank/1"
        duplicate_manifest["source"]["slot"] = 1
        (duplicate / "2" / "capture.json").write_text(json.dumps(duplicate_manifest), encoding="utf-8")

        reports = [self.importer.scan(path) for path in (bad_map, checksum, duration, duplicate)]

        self.assertTrue(all(report["state"] == "partially_invalid" for report in reports))
        all_errors = json.dumps(reports)
        self.assertIn("current game catalog", all_errors)
        self.assertIn("hash does not match", all_errors)
        self.assertIn("duration", all_errors)
        self.assertIn("slot does not match", all_errors)
        self.assertEqual((0, 0), self.counts())

    def test_identical_retry_is_idempotent(self) -> None:
        directory = write_daily_set(self.import_root)
        first = self.importer.commit(self.importer.scan(directory)["scanId"])
        version = first["set"]["version"]

        retry_scan = self.importer.scan(directory)
        retry = self.importer.commit(retry_scan["scanId"])

        self.assertTrue(retry_scan["isIdempotentRetry"])
        self.assertTrue(retry["idempotent"])
        self.assertEqual((3, 1), self.counts())
        self.assertEqual(version, retry["set"]["version"])

    def test_existing_compatible_draft_is_attached_without_duplication(self) -> None:
        draft = self.store.save_set({"name": "Existing", "mapSlug": "bank"})
        directory = write_daily_set(self.import_root)
        scan = self.importer.scan(directory)

        result = self.importer.commit(scan["scanId"], target_set_id=draft["id"])

        self.assertEqual((3, 1), self.counts())
        self.assertEqual(draft["id"], result["set"]["id"])
        self.assertEqual("Existing", result["set"]["name"])

    def test_reprocessed_capture_remains_available(self) -> None:
        directory = write_daily_set(self.import_root)
        self.importer.commit(self.importer.scan(directory)["scanId"])
        write_capture(directory, 2, token="reprocessed")

        scan = self.importer.scan(directory)
        result = self.importer.commit(scan["scanId"])

        self.assertEqual("reprocessed", scan["state"])
        self.assertEqual(["1-bank/2"], scan["reprocessedCaptureIds"])
        self.assertEqual("available", self.store.get_capture("1-bank/2")["status"])
        self.assertEqual("available", self.store.get_capture("1-bank/1")["status"])
        self.assertEqual(1, result["updatedCaptures"])
        self.assertEqual((3, 1), self.counts())

    def test_scheduled_reprocess_requires_explicit_stale_choice(self) -> None:
        directory = write_daily_set(self.import_root)
        imported = self.importer.commit(self.importer.scan(directory)["scanId"])
        complete = self.complete_set(imported["set"])
        complete["status"] = "approved"
        approved = self.store.save_set(complete, complete["id"])
        scheduled = self.store.schedule("2026-09-01", approved["id"])
        write_capture(directory, 1, token="replacement")

        scan = self.importer.scan(directory)
        with self.assertRaisesRegex(DailySetImportError, "scheduled draft"):
            self.importer.commit(scan["scanId"])
        self.assertEqual("stale", scan["state"])
        self.assertEqual("available", self.store.get_capture("1-bank/1")["status"])

        result = self.importer.commit(scan["scanId"], allow_stale=True)
        self.assertEqual("available", self.store.get_capture("1-bank/1")["status"])
        self.assertEqual("draft", result["set"]["status"])
        self.assertGreater(result["set"]["version"], scheduled["set_version"])
        self.assertEqual(scheduled["set_version"], self.store.get_schedule_entry("2026-09-01")["set_version"])

    def test_changed_files_and_injected_failure_leave_no_rows(self) -> None:
        directory = write_daily_set(self.import_root)
        changed_scan = self.importer.scan(directory)
        (directory / "1" / "listener.jpg").write_bytes(b"changed-after-scan")
        with self.assertRaisesRegex(ScanChangedError, "changed after scanning"):
            self.importer.commit(changed_scan["scanId"])
        self.assertEqual((0, 0), self.counts())

        write_capture(directory, 1, token="restored")
        failure_scan = self.importer.scan(directory)

        def fail_after_first_capture(step: str) -> None:
            if step == "capture:1":
                raise RuntimeError("injected transaction failure")

        with self.assertRaisesRegex(RuntimeError, "injected"):
            self.importer.commit(failure_scan["scanId"], failure_hook=fail_after_first_capture)
        self.assertEqual((0, 0), self.counts())

    def test_outside_import_root_is_refused(self) -> None:
        outside = write_daily_set(self.root / "outside")
        with self.assertRaisesRegex(DailySetImportError, "outside the configured import roots"):
            self.importer.scan(outside)
        self.assertEqual((0, 0), self.counts())

    def test_scan_and_commit_http_endpoints_use_the_same_transaction_flow(self) -> None:
        directory = write_daily_set(self.import_root)
        game_repo = self.root / "game"
        maps = game_repo / "assets" / "maps"
        data = game_repo / "assets" / "data"
        maps.mkdir(parents=True)
        data.mkdir(parents=True)
        (maps / "blueprint_manifest_wide_upscaled.json").write_text(
            json.dumps(
                {
                    "settings": {"refreshedAt": "endpoint-test"},
                    "images": [
                        {
                            "map_slug": "bank", "floor_key": "1f",
                            "ai_output_file": "wide-upscaled/bank_1f.png",
                            "selector_enabled": True,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (data / "operator_catalog.json").write_text(json.dumps({"operators": []}), encoding="utf-8")
        server = Server(("127.0.0.1", 0), App(self.store, game_repo, (self.import_root,)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def post(path: str, payload: dict) -> dict:
            request = Request(
                f"http://127.0.0.1:{server.server_port}{path}",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request) as response:
                return json.load(response)

        try:
            scan = post("/api/imports/scan", {"directoryPath": str(directory)})
            result = post("/api/imports/commit", {"scanId": scan["scanId"]})
            with urlopen(f"http://127.0.0.1:{server.server_port}/media/1-bank%2F1/listener.jpg") as media_response:
                media_body = media_response.read()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual("valid", scan["state"])
        self.assertEqual("success", result["state"])
        self.assertEqual("available", result["captures"][0]["status"])
        self.assertEqual(b"jpeg:1:original", media_body)
        self.assertEqual((3, 1), self.counts())


class CaptureMigrationTests(unittest.TestCase):
    def test_existing_capture_rows_survive_additive_migration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "studio.db"
            connection = sqlite3.connect(db)
            connection.execute(
                """CREATE TABLE captures (
                   id TEXT PRIMARY KEY, status TEXT NOT NULL,
                   evidence_still_path TEXT NOT NULL, evidence_audio_path TEXT NOT NULL,
                   replay_video_path TEXT NOT NULL, manifest_json TEXT NOT NULL,
                   created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
            )
            connection.execute(
                "INSERT INTO captures VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("legacy", "approved", "still", "audio", "video", "{}", "then", "then"),
            )
            connection.commit()
            connection.close()

            store = Store(db)

            with store.connect() as migrated:
                columns = {row[1] for row in migrated.execute("PRAGMA table_info(captures)")}
                row = migrated.execute("SELECT id, status FROM captures").fetchone()
                version = migrated.execute("PRAGMA user_version").fetchone()[0]
            self.assertEqual(("legacy", "approved"), tuple(row))
            self.assertIn("content_fingerprint", columns)
            self.assertIn("imported_source", columns)
            self.assertEqual(2, version)


if __name__ == "__main__":
    unittest.main()
