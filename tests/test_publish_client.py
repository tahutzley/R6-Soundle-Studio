from __future__ import annotations

import sys
import hashlib
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from fastapi.testclient import TestClient


STUDIO_ROOT = Path(__file__).resolve().parents[1]
GAME_ROOT = STUDIO_ROOT.parent / "R6-Soundle"
sys.path.insert(0, str(GAME_ROOT))
sys.path.insert(0, str(STUDIO_ROOT))

from server.app import create_app
from server.config import Settings
from server.repositories import InMemoryRepository
from server.scoring import load_scoring_config
from server.storage_filesystem import FilesystemStorage
from studio_publish import (
    PublishClientError,
    PublishInterrupted,
    PublishQueue,
    PublisherClient,
    StudioPublisher,
)
from studio_server import App, Server, Store
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

    def list_challenges(self):
        return self._value(self.client.get("/api/v1/challenges"))

    def remove_challenge(self, challenge_id, reason, idempotency_key):
        return self._value(
            self.client.post(
                f"/admin/v1/challenges/{challenge_id}/unavailable",
                headers={"Idempotency-Key": idempotency_key},
                json={"reason": reason},
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

    def test_media_upload_retries_transient_network_failures(self) -> None:
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        with tempfile.TemporaryDirectory() as temporary:
            media = Path(temporary) / "listener.jpg"
            media.write_bytes(b"media")
            client = PublisherClient(
                "https://publisher.example.invalid",
                "test-publisher-token-value",
            )
            with (
                patch(
                    "studio_publish.urlopen",
                    side_effect=[URLError(OSError(11002, "getaddrinfo failed")), Response()],
                ) as request,
                patch("studio_publish.time.sleep") as sleep,
            ):
                client.upload(
                    {"url": "https://s3.example.invalid/object", "method": "PUT", "headers": {}},
                    media,
                )

        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(0.5)

    def test_vendored_publish_contracts_match_game_authority(self) -> None:
        for version in (1, 2):
            with self.subTest(version=version):
                authority = json.loads(
                    (GAME_ROOT / "contracts" / f"publish-v{version}.schema.json").read_text(encoding="utf-8")
                )
                vendored = json.loads(
                    (STUDIO_ROOT / "schemas" / f"game-publish-v{version}.schema.json").read_text(encoding="utf-8")
                )
                source = json.loads(
                    (STUDIO_ROOT / "schemas" / f"game-publish-v{version}.source.json").read_text(encoding="utf-8")
                )
                canonical = json.dumps(authority, sort_keys=True, separators=(",", ":")).encode()
                self.assertEqual(vendored, authority)
                self.assertEqual(hashlib.sha256(canonical).hexdigest(), source["canonicalJsonSha256"])

    def _workflow(self, interrupt_after: int, *, immediate: bool = False) -> None:
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
                            },
                            {
                                "key": "2f",
                                "label": "2F",
                                "imageUrl": "/game-assets/maps/wide-upscaled/bank-2f.png",
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
            rounds[0]["alternateTargetFloorKey"] = "2f"
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
            test_client = TestClient(game, headers={"Authorization": f"Bearer {PUBLISHER_TOKEN}"})
            client = InProcessPublisherClient(test_client)
            scoring_version = load_scoring_config().version
            publisher = StudioPublisher(store, catalog, scoring_version, client, now=lambda: now)
            with self.assertRaises(PublishInterrupted):
                if immediate:
                    publisher.publish_immediately(approved["id"], interrupt_after=interrupt_after)
                else:
                    publisher.publish("2026-09-02", approved["id"], interrupt_after=interrupt_after)
            interrupted = publisher.list_attempts()[0]
            self.assertEqual(interrupted["state"], "uploading")
            self.assertEqual(client.prepare_calls, 1)
            self.assertEqual(
                sum(item["status"] == "uploaded" for item in interrupted["objects"]),
                interrupt_after,
            )
            with store.connect() as connection:
                persisted = connection.execute(
                    "SELECT objects_json FROM publish_attempts WHERE id=?", (interrupted["id"],)
                ).fetchone()[0]
                self.assertNotIn('"upload"', persisted)

            restarted = StudioPublisher(store, catalog, scoring_version, client, now=lambda: now)
            completed = (
                restarted.publish_immediately(approved["id"])
                if immediate
                else restarted.publish("2026-09-02", approved["id"])
            )
            self.assertEqual(completed["state"], "scheduled")
            self.assertEqual(client.prepare_calls, 1)
            self.assertEqual(client.upload_calls, 9)
            self.assertIsNotNone(completed["remote_release_id"])
            self.assertIsNotNone(completed["remote_release_version_id"])
            release_date = completed["release_date"]
            self.assertEqual(store.get_schedule_entry(release_date)["set_version"], approved["version"])
            with store.connect() as connection:
                published = connection.execute(
                    "SELECT * FROM published_releases WHERE release_date=?", (release_date,)
                ).fetchone()
                self.assertEqual(published["remote_release_id"], completed["remote_release_id"])
            if immediate:
                self.assertEqual(completed["remote_state"], "released")
                challenges = restarted.list_remote_challenges()["challenges"]
                self.assertEqual(len(challenges), 1)
                challenge = test_client.get(f"/api/v1/challenges/{challenges[0]['id']}")
                self.assertEqual(challenge.status_code, 200)
                self.assertEqual(
                    challenge.json()["puzzle"]["rounds"][0]["alternateTargetFloorKey"],
                    "2f",
                )
                removed = restarted.remove_challenge(
                    challenges[0]["id"], "Remove from production in Studio test"
                )
                self.assertEqual(removed["state"], "emergency_unavailable")
                self.assertEqual(restarted.list_remote_challenges()["challenges"], [])
                local = restarted.get_attempt(completed["id"])
                self.assertEqual(local["remote_state"], "emergency_unavailable")
                self.assertEqual(
                    test_client.get(f"/api/v1/challenges/{challenges[0]['id']}").status_code,
                    404,
                )
            elif interrupt_after == 1:
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

    def test_immediate_publish_resumes_and_is_live_without_a_date(self) -> None:
        self._workflow(5, immediate=True)

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


class StubPublisher:
    """Stands in for StudioPublisher so the queue's own behaviour is isolated."""

    def __init__(self, names: list[str], failures: frozenset[str] = frozenset()) -> None:
        self.items = [
            {"id": f"set_{name}", "name": name, "version": 3, "status": "approved", "kind": "daily"}
            for name in names
        ]
        self.failures = failures
        self.gate: threading.Event | None = None
        self.started: list[str] = []
        self.published: list[str] = []
        self._lock = threading.Lock()
        self._active = 0
        self.max_active = 0

    def publishable_sets(self) -> list[dict]:
        return [item for item in self.items if item["id"] not in self.published]

    def publish_immediately(self, set_id: str) -> dict:
        with self._lock:
            self.started.append(set_id)
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            if self.gate is not None and not self.gate.wait(timeout=5):
                raise AssertionError("Publish gate was never released")
            if set_id in self.failures:
                raise PublishClientError(f"Publisher rejected {set_id}")
            self.published.append(set_id)
            return {"last_response": {"publishAttempt": {"challengeId": f"bank-{set_id}"}}}
        finally:
            with self._lock:
                self._active -= 1


def drain(queue: PublishQueue, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = queue.snapshot()
        if not snapshot["pending"]:
            return snapshot
        time.sleep(0.01)
    raise AssertionError(f"Publish queue did not drain: {queue.snapshot()}")


def wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Condition was never met")


class PublishQueueTests(unittest.TestCase):
    def test_every_approved_set_publishes_one_at_a_time(self) -> None:
        publisher = StubPublisher(["Bank", "Border", "Chalet"])
        queue = PublishQueue(publisher)
        enqueued = queue.enqueue()
        self.assertEqual(enqueued["added"], 3)

        jobs = drain(queue)["jobs"]
        self.assertEqual([job["state"] for job in jobs], ["succeeded"] * 3)
        self.assertEqual([job["name"] for job in jobs], ["Bank", "Border", "Chalet"])
        self.assertEqual([job["challengeId"] for job in jobs],
                         [f"bank-set_{name}" for name in ("Bank", "Border", "Chalet")])
        # Production allocates each immediate challenge the next internal slot,
        # so overlapping publishes would race for the same date.
        self.assertEqual(publisher.max_active, 1)
        self.assertEqual(publisher.published, ["set_Bank", "set_Border", "set_Chalet"])

    def test_a_failing_set_does_not_stop_the_rest_of_the_queue(self) -> None:
        publisher = StubPublisher(["Bank", "Border", "Chalet"], failures=frozenset({"set_Border"}))
        queue = PublishQueue(publisher)
        queue.enqueue()

        jobs = drain(queue)["jobs"]
        self.assertEqual([job["state"] for job in jobs], ["succeeded", "failed", "succeeded"])
        self.assertIn("Publisher rejected set_Border", jobs[1]["error"])
        self.assertIsNone(jobs[1]["challengeId"])
        self.assertEqual(publisher.published, ["set_Bank", "set_Chalet"])

    def test_a_set_already_waiting_is_not_queued_twice(self) -> None:
        publisher = StubPublisher(["Bank", "Border"])
        publisher.gate = threading.Event()
        queue = PublishQueue(publisher)
        queue.enqueue()
        wait_until(lambda: queue.running)

        again = queue.enqueue()
        self.assertEqual(again["added"], 0)
        self.assertEqual(len(again["jobs"]), 2)

        publisher.gate.set()
        jobs = drain(queue)["jobs"]
        self.assertEqual([job["state"] for job in jobs], ["succeeded", "succeeded"])

    def test_clearing_drops_queued_work_but_lets_the_running_publish_finish(self) -> None:
        publisher = StubPublisher(["Bank", "Border", "Chalet"])
        publisher.gate = threading.Event()
        queue = PublishQueue(publisher)
        queue.enqueue()
        wait_until(lambda: queue.running)

        cleared = queue.clear()
        self.assertEqual([job["name"] for job in cleared["jobs"]], ["Bank"])
        publisher.gate.set()

        jobs = drain(queue)["jobs"]
        self.assertEqual([job["state"] for job in jobs], ["succeeded"])
        self.assertEqual(publisher.published, ["set_Bank"])
        self.assertEqual(publisher.started, ["set_Bank"])

    def test_a_set_outside_the_publishable_list_is_rejected(self) -> None:
        queue = PublishQueue(StubPublisher(["Bank"]))
        with self.assertRaisesRegex(ValueError, "awaiting production"):
            queue.enqueue(["set_Missing"])
        self.assertEqual(queue.snapshot()["jobs"], [])

    def test_studio_routes_queue_every_set_and_report_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            publisher = StubPublisher(["Bank", "Border"])
            publisher.gate = threading.Event()
            app = App(Store(root / "studio.db"), root / "game", (root,), publisher)
            server = Server(("127.0.0.1", 0), app)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                idle = json.loads(urlopen(f"{base}/api/publish-queue").read())
                self.assertEqual(idle, {"running": False, "pending": 0, "jobs": []})

                queued = json.loads(urlopen(Request(
                    f"{base}/api/publish-queue",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )).read())
                self.assertEqual(queued["added"], 2)

                wait_until(lambda: app.publish_queue.running)
                # A second publish path would race the queue for a release slot.
                with self.assertRaises(HTTPError) as conflict:
                    urlopen(Request(
                        f"{base}/api/publish",
                        data=json.dumps({"setId": "set_Border"}).encode(),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    ))
                self.assertEqual(conflict.exception.code, 409)
                conflict.exception.close()

                publisher.gate.set()
                drain(app.publish_queue)
                finished = json.loads(urlopen(f"{base}/api/publish-queue").read())
                self.assertEqual([job["state"] for job in finished["jobs"]], ["succeeded", "succeeded"])

                emptied = json.loads(urlopen(Request(
                    f"{base}/api/publish-queue/clear",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )).read())
                self.assertEqual(emptied, {"running": False, "pending": 0, "jobs": []})
            finally:
                server.shutdown()
                server.server_close()
