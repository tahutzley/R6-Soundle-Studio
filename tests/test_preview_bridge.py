from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from studio_server import App, PREVIEW_SCHEMA_VERSION, STUDIO_API_VERSION, Server, Store
from tests.capture_fixtures import write_capture


class PreviewBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.game_repo = self.root / "game"
        self.import_root = self.root / "daily sets"
        self.import_root.mkdir()
        self._write_game_checkout()
        self.store = Store(self.root / "studio.db")
        for slot in (1, 2, 3):
            self.store.import_capture(write_capture(self.import_root / "1-bank", slot))
        point = {"x": 0.4, "y": 0.6, "floorKey": "1f"}
        self.item = self.store.save_set({
            "name": "Preview set",
            "mapSlug": "bank",
            "mapName": "Bank",
            "mapAssetVersion": "test-assets",
            "rounds": [
                {
                    "position": slot,
                    "operatorId": "vigil",
                    "listenerPos": {**point, "angle": slot * 90},
                    "operatorStartPos": point,
                    "targetPos": {**point, "x": 0.7},
                    "captureId": f"1-bank/{slot}",
                }
                for slot in (1, 2, 3)
            ],
        })
        self.app = App(self.store, self.game_repo, (self.import_root,))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_game_checkout(self) -> None:
        (self.game_repo / "assets" / "data").mkdir(parents=True)
        (self.game_repo / "assets" / "maps").mkdir(parents=True)
        (self.game_repo / "assets" / "js").mkdir(parents=True)
        (self.game_repo / "tools").mkdir(parents=True)
        (self.game_repo / "index.html").write_text("<script type='module' src='assets/js/app.mjs'></script>", encoding="utf-8")
        (self.game_repo / "assets" / "js" / "app.mjs").write_text("export const realGame = true;", encoding="utf-8")
        (self.game_repo / "assets" / "data" / "scoring.json").write_text(json.dumps({"version": 5}), encoding="utf-8")
        (self.game_repo / "assets" / "data" / "daily_puzzles.json").write_text("[]", encoding="utf-8")
        (self.game_repo / "assets" / "data" / "operator_catalog.json").write_text(
            json.dumps({"operators": [{"id": "vigil", "name": "Vigil", "svgPath": "assets/operators/vigil.svg", "pngPath": "assets/operators/vigil.png"}]}),
            encoding="utf-8",
        )
        (self.game_repo / "assets" / "maps" / "blueprint_manifest_wide_upscaled.json").write_text(
            json.dumps({
                "settings": {"refreshedAt": "test-assets"},
                "images": [{
                    "map_slug": "bank", "floor_key": "1f", "selector_enabled": True,
                    "output_file": "bank.png", "wide_crop_box": [0, 0, 100, 100],
                    "square_crop_box": [0, 0, 100, 100],
                }],
            }),
            encoding="utf-8",
        )
        (self.game_repo / ".env").write_text("SECRET=must-not-be-served", encoding="utf-8")
        (self.game_repo / "tools" / "private.py").write_text("private = True", encoding="utf-8")

    @staticmethod
    def _canonical_hash(path: Path) -> str:
        value = json.loads(path.read_text(encoding="utf-8"))
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(canonical).hexdigest()

    def test_future_preview_is_versioned_immutable_and_uses_real_game_paths(self) -> None:
        session = self.app.previews.create(self.item["id"], "2031-04-05", self.item["version"])
        contract = session.contract

        self.assertEqual(PREVIEW_SCHEMA_VERSION, contract["schemaVersion"])
        self.assertEqual(5, contract["scoringVersion"])
        self.assertEqual("2031-04-05", contract["puzzle"]["date"])
        self.assertEqual([], contract["issues"])
        self.assertTrue(contract["puzzle"]["rounds"][0]["evidenceImageUrl"].endswith("/listener.jpg"))
        self.assertTrue(contract["puzzle"]["rounds"][0]["videoUrl"].startswith(f"/preview/{session.token}/media/"))

        changed = self.store.get_set(self.item["id"])
        changed["name"] = "Changed later"
        changed["mapName"] = "Changed map label"
        self.store.save_set(changed, changed["id"])
        self.assertEqual("Bank", session.contract["puzzle"]["mapName"])
        self.assertEqual(self.item["version"], session.contract["set"]["version"])

    def test_incomplete_missing_and_stale_states_are_explicit(self) -> None:
        item = self.store.get_set(self.item["id"])
        item["rounds"][0]["targetPos"] = None
        item["rounds"][1]["captureId"] = None
        item["mapAssetVersion"] = "old-assets"
        item = self.store.save_set(item, item["id"])

        session = self.app.previews.create(item["id"], "2032-01-02")
        codes = {issue["code"] for issue in session.contract["issues"]}

        self.assertIn("STALE_MAP_ASSET", codes)
        self.assertIn("INCOMPLETE_ROUND_1", codes)
        self.assertIn("MISSING_MEDIA_2", codes)
        self.assertIsNone(session.contract["puzzle"]["rounds"][1]["videoUrl"])

    def test_approval_refreshes_the_reviewed_map_asset_version(self) -> None:
        item = self.store.get_set(self.item["id"])
        item["mapAssetVersion"] = "old-assets"
        item["status"] = "approved"

        approved = self.app.save_set(item, item["id"])

        self.assertEqual("approved", approved["status"])
        self.assertEqual("test-assets", approved["mapAssetVersion"])

    def test_how_to_example_does_not_use_daily_challenge_preview(self) -> None:
        example = self.store.save_set(
            {
                "kind": "example",
                "name": "Example",
                "mapSlug": "bank",
                "mapAssetVersion": "test-assets",
                "rounds": [self.item["rounds"][0]],
            }
        )

        with self.assertRaisesRegex(ValueError, "How-to examples"):
            self.app.previews.create(example["id"], "2031-04-05")

    def test_expired_session_rejects_contract_and_media(self) -> None:
        now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self.app.previews.now = lambda: now
        self.app.previews.ttl = timedelta(seconds=1)
        session = self.app.previews.create(self.item["id"], "2031-04-05")
        self.app.previews.now = lambda: now + timedelta(seconds=2)

        with self.assertRaisesRegex(KeyError, "expired"):
            self.app.previews.get(session.token)
        self.assertEqual(session, self.app.previews.get(session.token, allow_expired=True))

    def test_http_bridge_allowlists_game_assets_and_negotiates_schema(self) -> None:
        server = Server(("127.0.0.1", 0), self.app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            health = json.loads(urlopen(f"{base}/api/health").read())
            self.assertEqual(STUDIO_API_VERSION, health["apiVersion"])

            self.item["status"] = "approved"
            self.item = self.store.save_set(self.item, self.item["id"])
            self.store.schedule("2035-04-12", self.item["id"])
            remove = Request(f"{base}/api/schedule/2035-04-12", method="DELETE")
            removed = json.loads(urlopen(remove).read())
            self.assertEqual("2035-04-12", removed["releaseDate"])
            self.assertIsNone(self.store.get_schedule_entry("2035-04-12"))

            bad = Request(
                f"{base}/api/previews",
                data=json.dumps({"schemaVersion": 99, "setId": self.item["id"]}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(HTTPError) as bad_error:
                urlopen(bad)
            self.assertEqual(426, bad_error.exception.code)
            bad_error.exception.close()

            request = Request(
                f"{base}/api/previews",
                data=json.dumps({
                    "schemaVersion": 1, "setId": self.item["id"],
                    "setVersion": self.item["version"], "displayDate": "2031-04-05",
                }).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            created = json.loads(urlopen(request).read())
            token = created["sessionId"]
            self.assertIn("assets/js/app.mjs", urlopen(f"{base}{created['url']}").read().decode())
            self.assertIn("realGame", urlopen(f"{base}/preview/{token}/assets/js/app.mjs").read().decode())
            contract = json.loads(urlopen(f"{base}/preview/{token}/api/preview").read())
            self.assertEqual("2031-04-05", contract["displayDate"])
            self.assertTrue(urlopen(base + contract["puzzle"]["rounds"][0]["evidenceImageUrl"]).read())
            self.assertTrue(urlopen(base + contract["puzzle"]["rounds"][0]["videoUrl"]).read())
            for forbidden in (
                f"/preview/{token}/assets/data/daily_puzzles.json",
                f"/preview/{token}/assets/js/%252e%252e/data/daily_puzzles.json",
                f"/preview/{token}/.env",
                f"/preview/{token}/tools/private.py",
            ):
                with self.subTest(forbidden=forbidden), self.assertRaises(HTTPError) as error:
                    urlopen(base + forbidden)
                self.assertEqual(404, error.exception.code)
                error.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_vendored_schema_matches_game_authority(self) -> None:
        studio_schema = Path(__file__).resolve().parents[1] / "schemas" / "game-preview-v1.schema.json"
        source = json.loads((studio_schema.parent / "game-preview-v1.source.json").read_text(encoding="utf-8"))
        game_schema = Path(__file__).resolve().parents[2] / "R6-Soundle" / source["authorityPath"]
        if not game_schema.is_file():
            self.skipTest("Sibling game checkout is unavailable")
        self.assertEqual(source["canonicalJsonSha256"], self._canonical_hash(game_schema))
        self.assertEqual(self._canonical_hash(game_schema), self._canonical_hash(studio_schema))

    def test_vendored_release_schema_matches_game_authority(self) -> None:
        studio_schema = Path(__file__).resolve().parents[1] / "schemas" / "game-release-v1.schema.json"
        source = json.loads((studio_schema.parent / "game-release-v1.source.json").read_text(encoding="utf-8"))
        game_schema = Path(__file__).resolve().parents[2] / "R6-Soundle" / source["authorityPath"]
        if not game_schema.is_file():
            self.skipTest("Sibling game checkout is unavailable")
        self.assertEqual(source["canonicalJsonSha256"], self._canonical_hash(game_schema))
        self.assertEqual(self._canonical_hash(game_schema), self._canonical_hash(studio_schema))


if __name__ == "__main__":
    unittest.main()
