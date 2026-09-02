from __future__ import annotations

import sys
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from fastapi.testclient import TestClient


STUDIO_ROOT = Path(__file__).resolve().parents[1]
GAME_ROOT = STUDIO_ROOT.parent / "R6-Soundle"
sys.path.insert(0, str(GAME_ROOT))
sys.path.insert(0, str(STUDIO_ROOT))

from server.app import create_app
from server.config import Settings
from server.repositories import InMemoryRepository
from server.storage_filesystem import FilesystemStorage
from studio_publish import PublishInterrupted, PublisherClient, StudioPublisher
from studio_server import Store
from tests.capture_fixtures import write_capture


UTC = timezone.utc
PUBLISHER_TOKEN = "studio-phase7-test-token"


class InProcessPublisherClient:
    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.prepare_calls = 0
        self.upload_calls = 0

    @staticmethod
    def _value(response):
        if response.status_code >= 400:
            raise RuntimeError(response.text)
        return response.json()

    def capabilities(self):
        return self._value(self.client.get("/admin/v1/capabilities"))

    def prepare(self, payload, idempotency_key):
        self.prepare_calls += 1
        return self._value(
            self.client.post(
                "/admin/v1/publish-attempts",
                headers={"Idempotency-Key": idempotency_key},
                json=payload,
            )
        )

    def status(self, attempt_id):
        return self._value(self.client.get(f"/admin/v1/publish-attempts/{attempt_id}"))

    def upload(self, authorization, path):
        self.upload_calls += 1
        return self._value(
            self.client.put(
                urlsplit(authorization["url"]).path,
                headers=authorization["headers"],
                content=path.read_bytes(),
            )
        )

    def verify(self, attempt_id):
        return self._value(self.client.post(f"/admin/v1/publish-attempts/{attempt_id}/verify"))

    def finalize(self, attempt_id):
        return self._value(self.client.post(f"/admin/v1/publish-attempts/{attempt_id}/finalize"))

    def make_unavailable(self, release_date, expected_revision, reason, idempotency_key):
        return self._value(
            self.client.post(
                f"/admin/v1/releases/{release_date}/unavailable",
                headers={"Idempotency-Key": idempotency_key},
                json={"expectedRevision": expected_revision, "reason": reason},
            )
        )


class StudioPublishClientTests(unittest.TestCase):
    def test_remote_client_requires_bearer_and_https_outside_loopback(self) -> None:
        with self.assertRaisesRegex(ValueError, "TOKEN"):
            PublisherClient("http://127.0.0.1:4190")
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            PublisherClient("http://publisher.example.invalid", "test-publisher-token-value")
        client = PublisherClient("https://publisher.example.invalid", "test-publisher-token-value")
        self.assertEqual(client.bearer_token, "test-publisher-token-value")

    def test_vendored_publish_contract_matches_game_authority(self) -> None:
        authority = json.loads((GAME_ROOT / "contracts" / "publish-v1.schema.json").read_text(encoding="utf-8"))
        vendored = json.loads((STUDIO_ROOT / "schemas" / "game-publish-v1.schema.json").read_text(encoding="utf-8"))
        source = json.loads((STUDIO_ROOT / "schemas" / "game-publish-v1.source.json").read_text(encoding="utf-8"))
        canonical = json.dumps(authority, sort_keys=True, separators=(",", ":")).encode()
        self.assertEqual(vendored, authority)
        self.assertEqual(hashlib.sha256(canonical).hexdigest(), source["canonicalJsonSha256"])

    def _workflow(self, interrupt_after: int) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = Store(root / "studio.db")
            for position in range(1, 4):
                store.import_capture(write_capture(root / "1-bank", position))
            point = {"x": 0.5, "y": 0.4, "floorKey": "1f"}
            catalog = {
                "assetVersion": "phase6-assets",
                "maps": [
                    {
                        "slug": "bank",
                        "name": "Bank",
                        "floors": [
                            {
                                "key": "1f",
                                "label": "1F",
                                "imageUrl": "/game-assets/maps/wide-upscaled/bank.png",
                                "pixelsPerMeter": 40,
                                "coordinateSize": 2048,
                                "coordinateFrame": {"x": 0, "y": 0, "width": 1, "height": 1},
                            }
                        ],
                    }
                ],
                "operators": [],
            }
            rounds = [
                {
                    "position": position,
                    "operatorId": "vigil",
                    "listenerPos": {**point, "angle": 90},
                    "operatorStartPos": point,
                    "targetPos": {**point, "x": 0.7},
                    "captureId": f"1-bank/{position}",
                }
                for position in range(1, 4)
            ]
            draft = store.save_set(
                {
                    "name": "Phase 6 set",
                    "mapSlug": "bank",
                    "mapName": "Bank",
                    "mapAssetVersion": catalog["assetVersion"],
                    "rounds": rounds,
                }
            )
            draft["status"] = "approved"
            approved = store.save_set(draft, draft["id"])

            now = datetime(2026, 9, 1, 12, tzinfo=UTC)
            storage = FilesystemStorage(root / "remote-objects", b"i" * 32)
            game = create_app(
                settings=Settings(
                    environment="test",
                    admin_api_enabled=True,
                    local_publisher_token=PUBLISHER_TOKEN,
                ),
                repository=InMemoryRepository(),
                storage=storage,
                clock=lambda: now,
            )
            client = InProcessPublisherClient(
                TestClient(game, headers={"Authorization": f"Bearer {PUBLISHER_TOKEN}"})
            )
            publisher = StudioPublisher(store, catalog, 5, client)
            with self.assertRaises(PublishInterrupted):
                publisher.publish("2026-09-02", approved["id"], interrupt_after=interrupt_after)
            interrupted = publisher.list_attempts()[0]
            self.assertEqual(interrupted["state"], "uploading")
            self.assertEqual(client.prepare_calls, 1)
            with store.connect() as connection:
                persisted = connection.execute(
                    "SELECT objects_json FROM publish_attempts WHERE id=?", (interrupted["id"],)
                ).fetchone()[0]
                self.assertNotIn('"upload"', persisted)

            restarted = StudioPublisher(store, catalog, 5, client)
            completed = restarted.publish("2026-09-02", approved["id"])
            self.assertEqual(completed["state"], "scheduled")
            self.assertEqual(client.prepare_calls, 1)
            self.assertEqual(client.upload_calls, 9)
            self.assertIsNotNone(completed["remote_release_id"])
            self.assertIsNotNone(completed["remote_release_version_id"])
            self.assertEqual(store.get_schedule_entry("2026-09-02")["set_version"], approved["version"])
            with store.connect() as connection:
                published = connection.execute(
                    "SELECT * FROM published_releases WHERE release_date='2026-09-02'"
                ).fetchone()
                self.assertEqual(published["remote_release_id"], completed["remote_release_id"])
            if interrupt_after == 1:
                stopped = restarted.stop_scheduled("2026-09-02", "content needs correction")
                self.assertEqual(stopped["remote_state"], "emergency_unavailable")
                self.assertEqual(stopped["transition_reason"], "content needs correction")
                replacement_draft = store.save_set(
                    {
                        "name": "Replacement set",
                        "mapSlug": "bank",
                        "mapName": "Bank",
                        "mapAssetVersion": catalog["assetVersion"],
                        "rounds": rounds,
                    }
                )
                replacement_draft["status"] = "approved"
                replacement = store.save_set(replacement_draft, replacement_draft["id"])
                corrected = restarted.publish(
                    "2026-09-02",
                    replacement["id"],
                    expected_revision=1,
                    reason="replace stopped set",
                )
                self.assertEqual(corrected["remote_state"], "scheduled")
                self.assertTrue(corrected["remote_release_version_id"].endswith(":r2"))
                attempts = restarted.list_attempts()
                original = next(item for item in attempts if item["id"] == stopped["id"])
                self.assertEqual(original["remote_state"], "superseded")

    def test_resume_after_objects_one_five_and_nine(self) -> None:
        for count in (1, 5, 9):
            with self.subTest(interrupt_after=count):
                self._workflow(count)

    def test_stale_map_assets_block_before_remote_prepare(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary) / "studio.db")
            item = store.save_set({"name": "Stale", "mapSlug": "bank"})
            item["status"] = "approved"
            # Incomplete content fails approval first, so call bundle against a synthetic stale row.
            with store.connect() as connection:
                connection.execute("UPDATE puzzle_sets SET status='approved'")
            publisher = StudioPublisher(
                store,
                {"assetVersion": "new", "maps": [], "operators": []},
                5,
                client=None,  # type: ignore[arg-type]
            )
            with self.assertRaisesRegex(ValueError, "stale map assets"):
                publisher.publish("2026-09-02", item["id"])

    def test_correction_bundle_requires_revision_and_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary) / "studio.db")
            publisher = StudioPublisher(
                store,
                {"assetVersion": "test", "maps": [], "operators": []},
                5,
                client=None,  # type: ignore[arg-type]
            )
            with self.assertRaisesRegex(ValueError, "both expected_revision and reason"):
                publisher._bundle("2026-09-02", "missing", expected_revision=1)
            with self.assertRaisesRegex(ValueError, "both expected_revision and reason"):
                publisher._bundle("2026-09-02", "missing", reason="fix")


if __name__ == "__main__":
    unittest.main()
