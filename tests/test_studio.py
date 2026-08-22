from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from studio_server import Store, release_at_for_date, validate_set


class StudioStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / "studio.db")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_capture(self, capture_id: str) -> None:
        output = self.root / capture_id
        output.mkdir()
        for filename in ("listener.jpg", "listener.m4a", "replay.mp4"):
            (output / filename).write_bytes(b"test")
        manifest = {
            "id": capture_id,
            "durationSeconds": 4.2,
            "evidence": {"stillPath": "listener.jpg", "audioPath": "listener.m4a"},
            "replay": {"videoPath": "replay.mp4"},
            "alignment": {"runnerOffsetMs": 10, "confidence": 0.9},
        }
        path = output / "capture.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        self.store.import_capture(path)
        self.store.approve_capture(capture_id)

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

    def test_incomplete_set_cannot_be_approved(self) -> None:
        item = self.store.save_set({"name": "Test", "mapSlug": "bank"})
        item["status"] = "approved"
        with self.assertRaisesRegex(ValueError, "Round 1"):
            self.store.save_set(item, item["id"])

    def test_schedule_and_publish_immutable_snapshot(self) -> None:
        for index in range(1, 4):
            self.make_capture(f"capture-{index}")
        draft = self.store.save_set(
            {
                "name": "Complete set",
                "mapSlug": "bank",
                "mapName": "Bank",
                "rounds": [self.complete_round(index, f"capture-{index}") for index in range(1, 4)],
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
        self.assertEqual("/media/capture-1/listener.jpg", puzzle["rounds"][0]["evidenceImageUrl"])
        self.assertEqual("/media/capture-1/listener.m4a", puzzle["rounds"][0]["audioUrl"])
        self.assertEqual("/media/capture-1/replay.mp4", puzzle["rounds"][0]["replayVideoUrl"])

    def test_new_york_midnight_handles_daylight_saving(self) -> None:
        self.assertEqual("2026-01-10T05:00:00Z", release_at_for_date("2026-01-10"))
        self.assertEqual("2026-07-10T04:00:00Z", release_at_for_date("2026-07-10"))


if __name__ == "__main__":
    unittest.main()
