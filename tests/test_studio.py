from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from studio_server import Store, load_catalog, position_is_valid, release_at_for_date, validate_set
from tests.capture_fixtures import write_capture


class StudioStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / "studio.db")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_capture(self, capture_id: str) -> None:
        slot = int(capture_id.rsplit("-", 1)[1])
        path = write_capture(self.root / "1-bank", slot)
        self.store.import_capture(path)

    def complete_round(self, position: int, capture_id: str) -> dict:
        point = {"x": 0.5, "y": 0.4, "floorKey": "1f"}
        return {
            "position": position,
            "operatorId": "vigil",
            "listenerPos": {**point, "angle": 90},
            "operatorStartPos": point,
            "targetPos": {**point, "x": 0.7},
            "captureId": capture_id,
        }

    def test_sets_always_have_three_neutral_rounds(self) -> None:
        item = self.store.save_set({"name": "Test", "mapSlug": "bank", "rounds": []})
        self.assertEqual([1, 2, 3], [round_item["position"] for round_item in item["rounds"]])
        self.assertNotIn("difficulty", item["rounds"][0])

    def test_phase5_publish_fields_are_additive_and_inert(self) -> None:
        with self.store.connect() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 3)
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertIn("publish_attempts", tables)
            published_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(published_releases)")
            }
            self.assertTrue(
                {
                    "remote_release_id",
                    "remote_release_version_id",
                    "publisher_idempotency_key",
                }.issubset(published_columns)
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM publish_attempts").fetchone()[0],
                0,
            )

    def test_phase5_migration_preserves_existing_published_snapshot(self) -> None:
        legacy_path = self.root / "legacy-v2.db"
        connection = sqlite3.connect(legacy_path)
        try:
            connection.execute(
                """CREATE TABLE published_releases (
                       release_date TEXT PRIMARY KEY,
                       release_at TEXT NOT NULL,
                       set_id TEXT NOT NULL,
                       set_version INTEGER NOT NULL,
                       snapshot_json TEXT NOT NULL,
                       published_at TEXT NOT NULL
                   )"""
            )
            connection.execute(
                "INSERT INTO published_releases VALUES (?, ?, ?, ?, ?, ?)",
                ("2026-08-31", "2026-08-31T04:00:00Z", "set-1", 2, "{}", "2026-08-31T04:00:01Z"),
            )
            connection.execute("PRAGMA user_version = 2")
            connection.commit()
        finally:
            connection.close()

        migrated = Store(legacy_path)
        with migrated.connect() as migrated_connection:
            row = migrated_connection.execute(
                "SELECT * FROM published_releases WHERE release_date='2026-08-31'"
            ).fetchone()
            self.assertEqual(row["set_id"], "set-1")
            self.assertEqual(row["set_version"], 2)
            self.assertIsNone(row["remote_release_id"])
            self.assertEqual(migrated_connection.execute("PRAGMA user_version").fetchone()[0], 3)

    def test_set_can_be_deleted(self) -> None:
        item = self.store.save_set({"name": "Disposable", "mapSlug": "bank", "rounds": []})

        self.store.delete_set(item["id"])

        self.assertIsNone(self.store.get_set(item["id"]))
        with self.assertRaisesRegex(KeyError, "Set not found"):
            self.store.delete_set(item["id"])

    def test_incomplete_set_cannot_be_approved(self) -> None:
        item = self.store.save_set({"name": "Test", "mapSlug": "bank"})
        item["status"] = "approved"
        with self.assertRaisesRegex(ValueError, "Round 1"):
            self.store.save_set(item, item["id"])

    def test_capture_review_status_does_not_block_set_approval(self) -> None:
        for index in range(1, 4):
            self.make_capture(f"capture-{index}")
        with self.store.connect() as connection:
            connection.execute("UPDATE captures SET status = 'needs_review'")

        item = self.store.save_set(
            {
                "name": "Legacy review set",
                "mapSlug": "bank",
                "rounds": [self.complete_round(index, f"1-bank/{index}") for index in range(1, 4)],
            }
        )
        item["status"] = "approved"
        approved = self.store.save_set(item, item["id"])

        self.assertEqual("approved", approved["status"])

    def test_positions_can_reach_the_full_wide_map(self) -> None:
        self.assertTrue(position_is_valid({"x": -0.4, "y": 0.5, "floorKey": "1f"}))
        self.assertTrue(position_is_valid({"x": 1.4, "y": 1.02, "floorKey": "1f"}))
        self.assertFalse(position_is_valid({"x": float("nan"), "y": 0.5, "floorKey": "1f"}))

    def test_schedule_and_publish_immutable_snapshot(self) -> None:
        for index in range(1, 4):
            self.make_capture(f"capture-{index}")
        draft = self.store.save_set(
            {
                "name": "Complete set",
                "mapSlug": "bank",
                "mapName": "Bank",
                "rounds": [self.complete_round(index, f"1-bank/{index}") for index in range(1, 4)],
            }
        )
        draft["status"] = "approved"
        approved = self.store.save_set(draft, draft["id"])
        self.assertEqual([], validate_set(approved, {item["id"]: item for item in self.store.list_captures()}))
        entry = self.store.schedule("2020-01-02", approved["id"])
        self.assertEqual("2020-01-02T05:00:00Z", entry["release_at"])
        puzzle = self.store.public_puzzle("2020-01-02", datetime(2020, 1, 2, 6, tzinfo=timezone.utc))
        self.assertIsNotNone(puzzle)
        self.assertEqual(3, len(puzzle["rounds"]))
        self.assertEqual("/media/1-bank%2F1/listener.jpg", puzzle["rounds"][0]["evidenceImageUrl"])
        self.assertEqual("/media/1-bank%2F1/listener.m4a", puzzle["rounds"][0]["audioUrl"])
        self.assertEqual("/media/1-bank%2F1/replay.mp4", puzzle["rounds"][0]["replayVideoUrl"])

    def test_new_york_midnight_handles_daylight_saving(self) -> None:
        cases = {
            "2026-01-10": "2026-01-10T05:00:00Z",
            "2026-03-08": "2026-03-08T05:00:00Z",
            "2026-03-09": "2026-03-09T04:00:00Z",
            "2026-07-10": "2026-07-10T04:00:00Z",
            "2026-11-01": "2026-11-01T04:00:00Z",
            "2026-11-02": "2026-11-02T05:00:00Z",
            "2028-02-29": "2028-02-29T05:00:00Z",
            "2040-07-04": "2040-07-04T04:00:00Z",
        }
        for local_date, expected in cases.items():
            with self.subTest(local_date=local_date):
                self.assertEqual(expected, release_at_for_date(local_date))

    def test_catalog_exposes_browser_ready_operator_artwork(self) -> None:
        game_repo = self.root / "game"
        maps_dir = game_repo / "assets" / "maps"
        data_dir = game_repo / "assets" / "data"
        maps_dir.mkdir(parents=True)
        data_dir.mkdir(parents=True)
        (maps_dir / "blueprint_manifest_wide_upscaled.json").write_text(
            json.dumps(
                {
                    "settings": {"refreshedAt": "test-version"},
                    "images": [
                        {
                            "map_slug": "bank",
                            "floor_key": "1f",
                            "ai_output_file": "wide-upscaled/bank_1f.png",
                            "wide_crop_box": [100, 50, 1500, 850],
                            "square_crop_box": [400, 100, 1200, 900],
                            "selector_enabled": True,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (data_dir / "operator_catalog.json").write_text(
            json.dumps(
                {
                    "operators": [
                        {
                            "id": "montagne",
                            "name": "Montagne",
                            "svgPath": "assets/operators/svg/montagne.svg",
                            "pngPath": "assets/operators/png/montagne.png",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        catalog = load_catalog(game_repo)

        floor = catalog["maps"][0]["floors"][0]
        self.assertEqual("1F", floor["label"])
        self.assertEqual("/game-assets/maps/wide-upscaled/bank_1f.png", floor["imageUrl"])
        self.assertEqual(
            {"x": 3 / 14, "y": 1 / 16, "width": 4 / 7, "height": 1},
            floor["coordinateFrame"],
        )
        self.assertEqual("/game-assets/operators/svg/montagne.svg", catalog["operators"][0]["iconUrl"])
        self.assertEqual("/game-assets/operators/png/montagne.png", catalog["operators"][0]["portraitUrl"])


if __name__ == "__main__":
    unittest.main()
