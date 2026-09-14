from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from studio_server import (
    Store,
    load_catalog,
    load_local_env,
    position_is_valid,
    release_at_for_date,
    validate_set,
)
from tests.capture_fixtures import write_capture


class LocalEnvironmentTests(unittest.TestCase):
    def test_dotenv_loads_allowlisted_unquoted_values_and_preserves_shell(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text(
                "R6_STUDIO_PUBLISHER_URL=https://from-file.example\n"
                "R6_STUDIO_PUBLISHER_TOKEN=unquoted-file-token\n"
                "UNRELATED_VALUE=ignored\n",
                encoding="utf-8",
            )
            with patch.dict(
                "os.environ",
                {"R6_STUDIO_PUBLISHER_URL": "https://from-shell.example"},
                clear=True,
            ):
                loaded = load_local_env(env_file)
                self.assertEqual(("R6_STUDIO_PUBLISHER_TOKEN",), loaded)
                self.assertEqual(
                    "https://from-shell.example",
                    os.environ["R6_STUDIO_PUBLISHER_URL"],
                )
                self.assertEqual(
                    "unquoted-file-token",
                    os.environ["R6_STUDIO_PUBLISHER_TOKEN"],
                )
                self.assertNotIn("UNRELATED_VALUE", os.environ)


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
        self.assertEqual("daily", item["kind"])
        self.assertEqual([1, 2, 3], [round_item["position"] for round_item in item["rounds"]])
        self.assertNotIn("difficulty", item["rounds"][0])

    def test_how_to_examples_have_one_round_and_cannot_be_scheduled(self) -> None:
        self.make_capture("capture-1")
        item = self.store.save_set(
            {
                "kind": "example",
                "name": "How to Play",
                "mapSlug": "bank",
                "rounds": [{
                    **self.complete_round(1, "1-bank/1"),
                    "guessPos": {"x": 0.65, "y": 0.5, "floorKey": "floor-1"},
                }],
            }
        )
        self.assertEqual("example", item["kind"])
        self.assertEqual([1], [round_item["position"] for round_item in item["rounds"]])

        item["status"] = "approved"
        approved = self.store.save_set(item, item["id"])
        self.assertEqual("approved", approved["status"])
        with self.assertRaisesRegex(ValueError, "How-to examples"):
            self.store.schedule("2035-04-12", approved["id"])

    def test_how_to_example_persists_and_requires_guess(self) -> None:
        self.make_capture("capture-1")
        round_item = self.complete_round(1, "1-bank/1")
        item = self.store.save_set(
            {
                "kind": "example",
                "name": "How to Play",
                "mapSlug": "bank",
                "rounds": [round_item],
            }
        )
        item["status"] = "approved"
        with self.assertRaisesRegex(ValueError, "example guess is required"):
            self.store.save_set(item, item["id"])

        item["rounds"][0]["guessPos"] = {
            "x": 0.65,
            "y": 0.5,
            "floorKey": "floor-1",
        }
        approved = self.store.save_set(item, item["id"])
        self.assertEqual(item["rounds"][0]["guessPos"], approved["rounds"][0]["guessPos"])

    def test_daily_set_discards_example_guess(self) -> None:
        item = self.store.save_set(
            {
                "name": "Daily",
                "mapSlug": "bank",
                "rounds": [{
                    "position": 1,
                    "guessPos": {"x": 0.5, "y": 0.5, "floorKey": "floor-1"},
                }],
            }
        )
        self.assertNotIn("guessPos", item["rounds"][0])

    def test_alternate_runner_end_floor_persists_and_must_be_distinct(self) -> None:
        for position in range(1, 4):
            self.make_capture(f"capture-{position}")
        rounds = [
            self.complete_round(position, f"1-bank/{position}")
            for position in range(1, 4)
        ]
        rounds[0]["alternateTargetFloorKey"] = "2f"

        item = self.store.save_set({
            "name": "Staircase set",
            "mapSlug": "bank",
            "rounds": rounds,
        })

        self.assertEqual("2f", item["rounds"][0]["alternateTargetFloorKey"])
        item["status"] = "approved"
        item = self.store.save_set(item, item["id"])
        self.assertEqual("approved", item["status"])
        self.assertEqual("2f", item["rounds"][0]["alternateTargetFloorKey"])

        item["rounds"][0]["alternateTargetFloorKey"] = "1f"
        errors = validate_set(item, {})
        self.assertIn(
            "Round 1: alternate runner-end floor must differ from the marked floor",
            errors,
        )

    def test_phase6_publish_fields_are_additive_and_inert(self) -> None:
        with self.store.connect() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 5)
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
            publish_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(publish_attempts)")
            }
            self.assertTrue(
                {
                    "remote_publish_attempt_id", "request_json", "objects_json", "completed_at",
                    "remote_state", "transition_reason", "transitioned_at",
                }.issubset(
                    publish_columns
                )
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
            self.assertEqual(migrated_connection.execute("PRAGMA user_version").fetchone()[0], 5)

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

    def test_unpublished_schedule_can_be_removed_without_deleting_its_set(self) -> None:
        item = self.store.save_set({"name": "Scheduled", "mapSlug": "bank"})
        with self.store.connect() as connection:
            connection.execute("UPDATE puzzle_sets SET status='approved' WHERE id=?", (item["id"],))

        self.store.schedule("2035-04-12", item["id"])
        removed = self.store.unschedule("2035-04-12")

        self.assertEqual("2035-04-12", removed["release_date"])
        self.assertIsNone(self.store.get_schedule_entry("2035-04-12"))
        self.assertIsNotNone(self.store.get_set(item["id"]))
        with self.assertRaisesRegex(KeyError, "Scheduled release not found"):
            self.store.unschedule("2035-04-12")

    def test_published_release_cannot_be_removed_from_schedule(self) -> None:
        for index in range(1, 4):
            self.make_capture(f"capture-{index}")
        item = self.store.save_set(
            {
                "name": "Published",
                "mapSlug": "bank",
                "rounds": [self.complete_round(index, f"1-bank/{index}") for index in range(1, 4)],
            }
        )
        item["status"] = "approved"
        item = self.store.save_set(item, item["id"])
        self.store.schedule("2035-04-12", item["id"])
        self.store.public_puzzle("2035-04-12", datetime(2035, 4, 13, tzinfo=timezone.utc))

        with self.assertRaisesRegex(ValueError, "published release"):
            self.store.unschedule("2035-04-12")
        self.assertIsNotNone(self.store.get_schedule_entry("2035-04-12"))

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
