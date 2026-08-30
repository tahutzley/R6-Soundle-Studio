#!/usr/bin/env python3
"""Local API and web server for R6 Soundle Studio."""

from __future__ import annotations

import argparse
import calendar
import json
import mimetypes
import sqlite3
import sys
import uuid
import webbrowser
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Timer
from typing import Any
from urllib.parse import unquote, urlparse


ROOT = Path(__file__).resolve().parent
STUDIO_DIR = ROOT / "studio"
DEFAULT_DB = ROOT / "studio.db"
DEFAULT_GAME_REPO = ROOT.parent / "R6-Soundle"
MAP_NAMES = {
    "bank": "Bank",
    "border": "Border",
    "calypso-casino": "Calypso Casino",
    "chalet": "Chalet",
    "clubhouse": "Clubhouse",
    "consulate": "Consulate",
    "fortress": "Fortress",
    "kafe": "Kafe Dostoyevsky",
    "kanal": "Kanal",
    "lair": "Lair",
    "nighthavenlabs": "Nighthaven Labs",
    "oregon": "Oregon",
    "themepark": "Theme Park",
    "villa": "Villa",
}


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS puzzle_sets (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    map_slug TEXT NOT NULL,
    map_asset_version TEXT NOT NULL,
    status TEXT NOT NULL,
    version INTEGER NOT NULL,
    content_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS captures (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    evidence_still_path TEXT NOT NULL,
    evidence_audio_path TEXT NOT NULL,
    replay_video_path TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schedule_entries (
    release_date TEXT PRIMARY KEY,
    release_at TEXT NOT NULL,
    set_id TEXT NOT NULL REFERENCES puzzle_sets(id),
    set_version INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS published_releases (
    release_date TEXT PRIMARY KEY,
    release_at TEXT NOT NULL,
    set_id TEXT NOT NULL,
    set_version INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    published_at TEXT NOT NULL
);
"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime | None = None) -> str:
    return (value or utc_now()).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def release_at_for_date(value: str) -> str:
    release_date = date.fromisoformat(value)
    # Midnight is before the 02:00 clock transition on both changeover days.
    # This avoids requiring the optional `tzdata` wheel on Windows while still
    # following the post-2007 America/New_York rules used by the project.
    march_sundays = [
        day for day in range(1, 15)
        if calendar.weekday(release_date.year, 3, day) == calendar.SUNDAY
    ]
    november_sundays = [
        day for day in range(1, 8)
        if calendar.weekday(release_date.year, 11, day) == calendar.SUNDAY
    ]
    dst_start = date(release_date.year, 3, march_sundays[1])
    dst_end = date(release_date.year, 11, november_sundays[0])
    utc_offset = -4 if dst_start < release_date <= dst_end else -5
    local_midnight = datetime.combine(
        release_date,
        time.min,
        timezone(timedelta(hours=utc_offset), "America/New_York"),
    )
    return iso_utc(local_midnight)


def slug_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def empty_round(position: int) -> dict[str, Any]:
    return {
        "position": position,
        "operatorId": None,
        "listenerPos": None,
        "operatorStartPos": None,
        "targetPos": None,
        "captureId": None,
    }


def normalize_set(payload: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    rounds_by_position = {
        int(item.get("position", index + 1)): item
        for index, item in enumerate(payload.get("rounds") or [])
        if isinstance(item, dict)
    }
    rounds = []
    for position in range(1, 4):
        source = rounds_by_position.get(position, {})
        item = empty_round(position)
        for key in ("operatorId", "listenerPos", "operatorStartPos", "targetPos", "captureId"):
            item[key] = source.get(key)
        rounds.append(item)

    return {
        "id": existing["id"] if existing else str(payload.get("id") or slug_id("set")),
        "name": str(payload.get("name") or (existing or {}).get("name") or "Untitled set").strip(),
        "mapSlug": str(payload.get("mapSlug") or (existing or {}).get("mapSlug") or "").strip(),
        "mapName": str(payload.get("mapName") or (existing or {}).get("mapName") or "").strip(),
        "mapAssetVersion": str(payload.get("mapAssetVersion") or "game-checkout").strip(),
        "status": str(payload.get("status") or "draft"),
        "version": int((existing or {}).get("version", 0)) + 1,
        "rounds": rounds,
    }


def position_is_valid(value: Any) -> bool:
    if not isinstance(value, dict) or not value.get("floorKey"):
        return False
    try:
        return 0 <= float(value["x"]) <= 1 and 0 <= float(value["y"]) <= 1
    except (KeyError, TypeError, ValueError):
        return False


def validate_set(item: dict[str, Any], captures: dict[str, dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    if not item.get("name"):
        errors.append("Set name is required")
    if not item.get("mapSlug"):
        errors.append("Map is required")
    rounds = item.get("rounds") or []
    if len(rounds) != 3:
        errors.append("A set must contain exactly three rounds")
        return errors
    for index, round_item in enumerate(rounds, 1):
        prefix = f"Round {index}"
        if not round_item.get("operatorId"):
            errors.append(f"{prefix}: operator is required")
        for key, label in (
            ("listenerPos", "listener position"),
            ("operatorStartPos", "runner start"),
            ("targetPos", "target position"),
        ):
            if not position_is_valid(round_item.get(key)):
                errors.append(f"{prefix}: {label} is required")
        capture_id = round_item.get("captureId")
        if not capture_id:
            errors.append(f"{prefix}: processed capture is required")
        elif captures.get(capture_id, {}).get("status") != "approved":
            errors.append(f"{prefix}: capture must be approved")
    return errors


class Store:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _decode_set(row: sqlite3.Row) -> dict[str, Any]:
        item = json.loads(row["content_json"])
        item.update(
            status=row["status"],
            version=row["version"],
            createdAt=row["created_at"],
            updatedAt=row["updated_at"],
        )
        return item

    @staticmethod
    def _decode_capture(row: sqlite3.Row) -> dict[str, Any]:
        manifest = json.loads(row["manifest_json"])
        return {
            "id": row["id"],
            "status": row["status"],
            "evidence": {
                "stillPath": row["evidence_still_path"],
                "audioPath": row["evidence_audio_path"],
            },
            "replay": {"videoPath": row["replay_video_path"]},
            "alignment": manifest.get("alignment", {}),
            "durationSeconds": manifest.get("durationSeconds"),
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    def list_sets(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM puzzle_sets ORDER BY updated_at DESC"
            ).fetchall()
        return [self._decode_set(row) for row in rows]

    def get_set(self, set_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM puzzle_sets WHERE id = ?", (set_id,)).fetchone()
        return self._decode_set(row) if row else None

    def save_set(self, payload: dict[str, Any], set_id: str | None = None) -> dict[str, Any]:
        existing = self.get_set(set_id) if set_id else None
        if set_id and not existing:
            raise KeyError("Set not found")
        item = normalize_set(payload, existing)
        if item["status"] not in {"draft", "ready", "approved", "archived"}:
            raise ValueError("Invalid set status")
        captures = {capture["id"]: capture for capture in self.list_captures()}
        validation_errors = validate_set(item, captures)
        if item["status"] in {"ready", "approved"} and validation_errors:
            raise ValueError("; ".join(validation_errors))
        now = iso_utc()
        item["validationErrors"] = validation_errors
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO puzzle_sets
                    (id, name, map_slug, map_asset_version, status, version,
                     content_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name, map_slug=excluded.map_slug,
                    map_asset_version=excluded.map_asset_version,
                    status=excluded.status, version=excluded.version,
                    content_json=excluded.content_json, updated_at=excluded.updated_at
                """,
                (
                    item["id"], item["name"], item["mapSlug"], item["mapAssetVersion"],
                    item["status"], item["version"], json.dumps(item, separators=(",", ":")),
                    existing.get("createdAt", now) if existing else now, now,
                ),
            )
        return self.get_set(item["id"])  # type: ignore[return-value]

    def delete_set(self, set_id: str) -> None:
        if not self.get_set(set_id):
            raise KeyError("Set not found")
        with self.connect() as connection:
            connection.execute("DELETE FROM schedule_entries WHERE set_id = ?", (set_id,))
            connection.execute("DELETE FROM puzzle_sets WHERE id = ?", (set_id,))

    def list_captures(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM captures ORDER BY created_at DESC").fetchall()
        return [self._decode_capture(row) for row in rows]

    def get_capture(self, capture_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM captures WHERE id = ?", (capture_id,)).fetchone()
        return self._decode_capture(row) if row else None

    def import_capture(self, manifest_path: Path) -> dict[str, Any]:
        manifest_path = manifest_path.expanduser().resolve()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        capture_id = str(manifest["id"])
        evidence = manifest["evidence"]
        replay = manifest["replay"]

        def resolved(value: str) -> str:
            candidate = Path(value)
            if not candidate.is_absolute():
                candidate = manifest_path.parent / candidate
            candidate = candidate.resolve()
            if not candidate.is_file():
                raise ValueError(f"Capture output does not exist: {candidate}")
            return str(candidate)

        still = resolved(evidence["stillPath"])
        audio = resolved(evidence["audioPath"])
        video = resolved(replay["videoPath"])
        now = iso_utc()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO captures
                    (id, status, evidence_still_path, evidence_audio_path,
                     replay_video_path, manifest_json, created_at, updated_at)
                VALUES (?, 'needs_review', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    evidence_still_path=excluded.evidence_still_path,
                    evidence_audio_path=excluded.evidence_audio_path,
                    replay_video_path=excluded.replay_video_path,
                    manifest_json=excluded.manifest_json,
                    status='needs_review', updated_at=excluded.updated_at
                """,
                (capture_id, still, audio, video, json.dumps(manifest), now, now),
            )
        return self.get_capture(capture_id)  # type: ignore[return-value]

    def approve_capture(self, capture_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE captures SET status='approved', updated_at=? WHERE id=?",
                (iso_utc(), capture_id),
            )
            if not cursor.rowcount:
                raise KeyError("Capture not found")
        return self.get_capture(capture_id)  # type: ignore[return-value]

    def schedule(self, release_date: str, set_id: str) -> dict[str, Any]:
        item = self.get_set(set_id)
        if not item:
            raise KeyError("Set not found")
        if item["status"] != "approved":
            raise ValueError("Only an approved set can be scheduled")
        release_at = release_at_for_date(release_date)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO schedule_entries
                    (release_date, release_at, set_id, set_version, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(release_date) DO UPDATE SET
                    release_at=excluded.release_at, set_id=excluded.set_id,
                    set_version=excluded.set_version, created_at=excluded.created_at
                """,
                (release_date, release_at, set_id, item["version"], iso_utc()),
            )
        return self.get_schedule_entry(release_date)  # type: ignore[return-value]

    def get_schedule_entry(self, release_date: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM schedule_entries WHERE release_date=?", (release_date,)
            ).fetchone()
        return dict(row) if row else None

    def list_schedule(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT s.*, p.name AS set_name, p.status AS set_status
                FROM schedule_entries s JOIN puzzle_sets p ON p.id=s.set_id
                ORDER BY s.release_date
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def _snapshot(self, entry: dict[str, Any]) -> dict[str, Any]:
        item = self.get_set(entry["set_id"])
        if not item:
            raise KeyError("Scheduled set not found")
        if item["version"] != entry["set_version"]:
            raise ValueError(
                f"Scheduled version {entry['set_version']} no longer matches set version {item['version']}; reschedule it"
            )
        captures = {capture["id"]: capture for capture in self.list_captures()}
        errors = validate_set(item, captures)
        if errors:
            raise ValueError("; ".join(errors))
        rounds = []
        for source in item["rounds"]:
            capture = captures[source["captureId"]]
            rounds.append(
                {
                    **source,
                    "evidenceImageUrl": f"/media/{capture['id']}/listener.jpg",
                    "audioUrl": f"/media/{capture['id']}/listener.m4a",
                    "replayVideoUrl": f"/media/{capture['id']}/replay.mp4",
                }
            )
        return {
            "date": entry["release_date"],
            "releaseAt": entry["release_at"],
            "setId": item["id"],
            "setVersion": item["version"],
            "mapSlug": item["mapSlug"],
            "mapName": item.get("mapName") or item["mapSlug"],
            "mapAssetVersion": item["mapAssetVersion"],
            "rounds": rounds,
        }

    def publish_due(self, now: datetime | None = None) -> list[str]:
        now_text = iso_utc(now)
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM schedule_entries
                WHERE release_at <= ? AND release_date NOT IN
                    (SELECT release_date FROM published_releases)
                ORDER BY release_date
                """,
                (now_text,),
            ).fetchall()
        published: list[str] = []
        for row in rows:
            entry = dict(row)
            try:
                snapshot = self._snapshot(entry)
            except (KeyError, ValueError):
                # A stale scheduled version must be explicitly rescheduled, but
                # it must not prevent unrelated releases from publishing.
                continue
            with self.connect() as connection:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO published_releases
                        (release_date, release_at, set_id, set_version,
                         snapshot_json, published_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        entry["release_date"], entry["release_at"], entry["set_id"],
                        entry["set_version"], json.dumps(snapshot, separators=(",", ":")), iso_utc(),
                    ),
                )
            published.append(entry["release_date"])
        return published

    def public_puzzle(self, release_date: str, now: datetime | None = None) -> dict[str, Any] | None:
        self.publish_due(now)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM published_releases WHERE release_date=?", (release_date,)
            ).fetchone()
        if not row or parse_iso(row["release_at"]) > (now or utc_now()):
            return None
        return json.loads(row["snapshot_json"])


def load_catalog(game_repo: Path) -> dict[str, Any]:
    manifest_path = game_repo / "assets" / "maps" / "blueprint_manifest_wide_upscaled.json"
    operator_path = game_repo / "assets" / "data" / "operator_catalog.json"
    if not manifest_path.is_file() or not operator_path.is_file():
        raise FileNotFoundError(f"Game authoring assets were not found under {game_repo}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    operator_catalog = json.loads(operator_path.read_text(encoding="utf-8"))
    maps: dict[str, dict[str, Any]] = {}
    for image in manifest.get("images", []):
        if image.get("selector_enabled") is False or not (image.get("ai_output_file") or image.get("output_file")):
            continue
        wide = image.get("wide_crop_box")
        square = image.get("square_crop_box")
        if (
            isinstance(wide, list) and len(wide) == 4
            and isinstance(square, list) and len(square) == 4
            and wide[2] != wide[0] and wide[3] != wide[1]
        ):
            coordinate_frame = {
                "x": (square[0] - wide[0]) / (wide[2] - wide[0]),
                "y": (square[1] - wide[1]) / (wide[3] - wide[1]),
                "width": (square[2] - square[0]) / (wide[2] - wide[0]),
                "height": (square[3] - square[1]) / (wide[3] - wide[1]),
            }
        else:
            coordinate_frame = {"x": 0, "y": 0, "width": 1, "height": 1}
        slug = image["map_slug"]
        target = maps.setdefault(slug, {"slug": slug, "name": MAP_NAMES.get(slug, slug.replace("-", " ").title()), "floors": []})
        target["floors"].append(
            {
                "key": image["floor_key"],
                "label": floor_label(image["floor_key"]),
                "imageUrl": f"/game-assets/maps/{image.get('ai_output_file') or image['output_file']}",
                "coordinateFrame": coordinate_frame,
            }
        )
    floor_order = {"tunnel": -2, "basement": -1, "1f": 1, "2f": 2, "3f": 3, "big-tower-t3": 4}
    for item in maps.values():
        item["floors"].sort(
            key=lambda floor: floor_order.get(
                floor["key"],
                int("".join(character for character in floor["key"] if character.isdigit()) or 99),
            )
        )
    operators = []
    for operator in operator_catalog.get("operators", []):
        item = dict(operator)
        svg_path = str(item.get("svgPath") or "").removeprefix("assets/")
        png_path = str(item.get("pngPath") or "").removeprefix("assets/")
        item["iconUrl"] = f"/game-assets/{svg_path}" if svg_path else ""
        item["portraitUrl"] = f"/game-assets/{png_path}" if png_path else ""
        operators.append(item)

    return {
        "assetVersion": str(manifest.get("settings", {}).get("refreshedAt") or manifest_path.stat().st_mtime_ns),
        "maps": sorted(maps.values(), key=lambda item: item["name"]),
        "operators": operators,
    }


def floor_label(key: str) -> str:
    aliases = {"basement": "B", "tunnel": "T", "big-tower-t3": "3F", "1f": "1F", "2f": "2F", "3f": "3F"}
    if key in aliases:
        return aliases[key]
    if key.startswith("floor-") and key[6:].isdigit():
        return f"{int(key[6:])}F"
    return key.replace("-", " ").title()


@dataclass
class App:
    store: Store
    game_repo: Path

    @property
    def catalog(self) -> dict[str, Any]:
        return load_catalog(self.game_repo)


class Handler(BaseHTTPRequestHandler):
    server_version = "R6SoundleStudio/0.1"

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}")

    def _json(self, value: Any, status: int = 200) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        size = int(self.headers.get("Content-Length", "0"))
        if size > 2_000_000:
            raise ValueError("Request is too large")
        value = json.loads(self.rfile.read(size) or b"{}")
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object")
        return value

    def _serve_file(self, root: Path, relative: str) -> None:
        root = root.resolve()
        candidate = (root / unquote(relative).lstrip("/")).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if candidate.is_dir():
            candidate /= "index.html"
        if not candidate.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store" if candidate.suffix in {".html", ".js"} else "public, max-age=3600")
        self.end_headers()
        self.wfile.write(body)

    def _media_path(self, relative: str) -> Path | None:
        parts = [part for part in relative.split("/") if part]
        if len(parts) != 2:
            return None
        capture = self.app.store.get_capture(parts[0])
        if not capture:
            return None
        filename = parts[1]
        mapping = {
            "listener.jpg": capture["evidence"]["stillPath"],
            "listener.m4a": capture["evidence"]["audioPath"],
            "replay.mp4": capture["replay"]["videoPath"],
        }
        return Path(mapping[filename]) if filename in mapping else None

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/health":
                self._json({"ok": True, "time": iso_utc()})
            elif path == "/api/catalog":
                self._json(self.app.catalog)
            elif path == "/api/sets":
                self._json(self.app.store.list_sets())
            elif path.startswith("/api/sets/"):
                item = self.app.store.get_set(path.rsplit("/", 1)[1])
                self._json(item, 200 if item else 404)
            elif path == "/api/captures":
                self._json(self.app.store.list_captures())
            elif path == "/api/schedule":
                self._json(self.app.store.list_schedule())
            elif path.startswith("/api/puzzles/"):
                item = self.app.store.public_puzzle(path.rsplit("/", 1)[1])
                self._json(item or {"error": "Puzzle is not released"}, 200 if item else 404)
            elif path.startswith("/media/"):
                candidate = self._media_path(path[len("/media/"):])
                if not candidate or not candidate.is_file():
                    self.send_error(HTTPStatus.NOT_FOUND)
                else:
                    self._serve_file(candidate.parent, candidate.name)
            elif path.startswith("/game-assets/"):
                self._serve_file(self.app.game_repo / "assets", path[len("/game-assets/"):])
            else:
                relative = "index.html" if path == "/" else path
                self._serve_file(STUDIO_DIR, relative)
        except Exception as error:
            self._json({"error": str(error)}, 500)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
            if path == "/api/sets":
                self._json(self.app.store.save_set(payload), 201)
            elif path == "/api/captures/import":
                self._json(self.app.store.import_capture(Path(str(payload.get("manifestPath", "")))), 201)
            elif path.startswith("/api/captures/") and path.endswith("/approve"):
                capture_id = path.split("/")[3]
                self._json(self.app.store.approve_capture(capture_id))
            elif path == "/api/schedule":
                self._json(self.app.store.schedule(str(payload["releaseDate"]), str(payload["setId"])), 201)
            elif path == "/api/releases/publish-due":
                self._json({"published": self.app.store.publish_due()})
            else:
                self._json({"error": "Not found"}, 404)
        except KeyError as error:
            self._json({"error": str(error)}, 404)
        except (ValueError, json.JSONDecodeError) as error:
            self._json({"error": str(error)}, 400)
        except Exception as error:
            self._json({"error": str(error)}, 500)

    def do_PUT(self) -> None:
        path = urlparse(self.path).path
        try:
            if not path.startswith("/api/sets/"):
                self._json({"error": "Not found"}, 404)
                return
            set_id = path.rsplit("/", 1)[1]
            self._json(self.app.store.save_set(self._read_json(), set_id))
        except KeyError as error:
            self._json({"error": str(error)}, 404)
        except (ValueError, json.JSONDecodeError) as error:
            self._json({"error": str(error)}, 400)
        except Exception as error:
            self._json({"error": str(error)}, 500)

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        try:
            if not path.startswith("/api/sets/"):
                self._json({"error": "Not found"}, 404)
                return
            self.app.store.delete_set(path.rsplit("/", 1)[1])
            self._json({"deleted": True})
        except KeyError as error:
            self._json({"error": str(error)}, 404)
        except Exception as error:
            self._json({"error": str(error)}, 500)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: App):
        super().__init__(address, Handler)
        self.app = app


def run_server(args: argparse.Namespace) -> None:
    game_repo = args.game_repo.resolve()
    app = App(Store(args.db.resolve()), game_repo)
    server = Server((args.host, args.port), app)
    url = f"http://{args.host}:{server.server_port}/"
    print(f"R6 Soundle Studio: {url}")
    print(f"Database: {app.store.db_path}")
    print(f"Game authoring assets: {game_repo}")
    if not args.no_browser:
        Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Studio.")
    finally:
        server.server_close()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--host", default="127.0.0.1")
    result.add_argument("--port", type=int, default=4180)
    result.add_argument("--db", type=Path, default=DEFAULT_DB)
    result.add_argument("--game-repo", type=Path, default=DEFAULT_GAME_REPO)
    result.add_argument("--no-browser", action="store_true")
    return result


if __name__ == "__main__":
    run_server(parser().parse_args())
