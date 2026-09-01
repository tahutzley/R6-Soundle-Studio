#!/usr/bin/env python3
"""Local API and web server for R6 Soundle Studio."""

from __future__ import annotations

import argparse
import json
import math
import mimetypes
import os
import secrets
import sqlite3
import sys
import tempfile
import uuid
import webbrowser
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Timer
from typing import Any, Callable
from urllib.parse import quote, unquote, urlparse
from zoneinfo import ZoneInfo

from processor.capture_contract import read_and_validate_capture, resolve_capture_file
from studio_import import (
    DailySetImportError,
    DailySetImporter,
    ScanChangedError,
    capture_content_fingerprint,
)
from studio_publish import PublishClientError, PublisherClient, StudioPublisher


ROOT = Path(__file__).resolve().parent
STUDIO_DIR = ROOT / "studio"
DEFAULT_DB = ROOT / "studio.db"
DEFAULT_GAME_REPO = ROOT.parent / "R6-Soundle"
PREVIEW_SCHEMA_VERSION = 1
PREVIEW_TTL = timedelta(minutes=30)
EASTERN = ZoneInfo("America/New_York")
GAME_PREVIEW_ASSET_PREFIXES = (
    "branding/", "css/", "fonts/", "hero/", "icons/", "js/", "maps/", "operators/",
)
GAME_PREVIEW_ASSET_FILES = {
    "data/operator_catalog.json",
    "data/scoring.json",
    "clips/Screen Recording 2026-06-28 235114.mp4",
}
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
    content_fingerprint TEXT,
    schema_version INTEGER,
    processing_version INTEGER,
    map_set TEXT,
    slot INTEGER CHECK(slot BETWEEN 1 AND 3),
    imported_source TEXT,
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
    published_at TEXT NOT NULL,
    remote_release_id TEXT,
    remote_release_version_id TEXT,
    publisher_idempotency_key TEXT
);
CREATE TABLE IF NOT EXISTS publish_attempts (
    id TEXT PRIMARY KEY,
    release_date TEXT NOT NULL,
    set_id TEXT NOT NULL REFERENCES puzzle_sets(id),
    set_version INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK(state IN ('approved', 'uploading', 'upload_failed', 'scheduled')),
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK(retry_count >= 0),
    remote_release_id TEXT,
    remote_release_version_id TEXT,
    remote_publish_attempt_id TEXT,
    request_json TEXT,
    objects_json TEXT NOT NULL DEFAULT '[]',
    last_response_json TEXT,
    completed_at TEXT,
    error_summary TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
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
    local_midnight = datetime.combine(release_date, time.min, EASTERN)
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

    item = {
        "id": existing["id"] if existing else str(payload.get("id") or slug_id("set")),
        "name": str(payload.get("name") or (existing or {}).get("name") or "Untitled set").strip(),
        "mapSlug": str(payload.get("mapSlug") or (existing or {}).get("mapSlug") or "").strip(),
        "mapName": str(payload.get("mapName") or (existing or {}).get("mapName") or "").strip(),
        "mapAssetVersion": str(payload.get("mapAssetVersion") or "game-checkout").strip(),
        "status": str(payload.get("status") or "draft"),
        "version": int((existing or {}).get("version", 0)) + 1,
        "rounds": rounds,
    }
    for key in ("importedMapSet", "importFingerprint"):
        value = payload.get(key, (existing or {}).get(key))
        if value:
            item[key] = str(value)
    return item


def position_is_valid(value: Any) -> bool:
    if not isinstance(value, dict) or not value.get("floorKey"):
        return False
    try:
        return math.isfinite(float(value["x"])) and math.isfinite(float(value["y"]))
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
        elif capture_id not in captures:
            errors.append(f"{prefix}: processed capture was not found")
    return errors


class Store:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            self._migrate(connection)

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(captures)")}
        additions = {
            "content_fingerprint": "TEXT",
            "schema_version": "INTEGER",
            "processing_version": "INTEGER",
            "map_set": "TEXT",
            "slot": "INTEGER CHECK(slot BETWEEN 1 AND 3)",
            "imported_source": "TEXT",
        }
        for name, declaration in additions.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE captures ADD COLUMN {name} {declaration}")
        connection.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS captures_map_set_slot_unique
               ON captures(map_set, slot) WHERE map_set IS NOT NULL AND slot IS NOT NULL"""
        )
        published_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(published_releases)")
        }
        published_additions = {
            "remote_release_id": "TEXT",
            "remote_release_version_id": "TEXT",
            "publisher_idempotency_key": "TEXT",
        }
        for name, declaration in published_additions.items():
            if name not in published_columns:
                connection.execute(
                    f"ALTER TABLE published_releases ADD COLUMN {name} {declaration}"
                )
        connection.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS published_releases_remote_version_unique
               ON published_releases(remote_release_version_id)
               WHERE remote_release_version_id IS NOT NULL"""
        )
        publish_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(publish_attempts)")
        }
        publish_additions = {
            "remote_publish_attempt_id": "TEXT",
            "request_json": "TEXT",
            "objects_json": "TEXT NOT NULL DEFAULT '[]'",
            "last_response_json": "TEXT",
            "completed_at": "TEXT",
        }
        for name, declaration in publish_additions.items():
            if name not in publish_columns:
                connection.execute(f"ALTER TABLE publish_attempts ADD COLUMN {name} {declaration}")
        connection.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS publish_attempts_remote_unique
               ON publish_attempts(remote_publish_attempt_id)
               WHERE remote_publish_attempt_id IS NOT NULL"""
        )
        connection.execute("PRAGMA user_version = 4")

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
    def now_text() -> str:
        return iso_utc()

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
        source = manifest.get("source", {})
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
            "media": manifest.get("media", []),
            "source": source,
            "contentFingerprint": row["content_fingerprint"],
            "schemaVersion": row["schema_version"],
            "processingVersion": row["processing_version"],
            "mapSet": row["map_set"],
            "slot": row["slot"],
            "importedSource": row["imported_source"],
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
        manifest = read_and_validate_capture(manifest_path)
        capture_id = str(manifest["id"])
        evidence = manifest["evidence"]
        replay = manifest["replay"]
        still = str(resolve_capture_file(manifest_path.parent, evidence["stillPath"], "evidence.stillPath"))
        audio = str(resolve_capture_file(manifest_path.parent, evidence["audioPath"], "evidence.audioPath"))
        video = str(resolve_capture_file(manifest_path.parent, replay["videoPath"], "replay.videoPath"))
        source = manifest["source"]
        fingerprint = capture_content_fingerprint(manifest)
        now = iso_utc()
        with self.connect() as connection:
            current = connection.execute("SELECT * FROM captures WHERE id=?", (capture_id,)).fetchone()
            if current and current["content_fingerprint"] == fingerprint:
                return self._decode_capture(current)
            connection.execute(
                """
                INSERT INTO captures
                    (id, status, evidence_still_path, evidence_audio_path,
                     replay_video_path, manifest_json, content_fingerprint,
                     schema_version, processing_version, map_set, slot,
                     imported_source, created_at, updated_at)
                VALUES (?, 'available', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    evidence_still_path=excluded.evidence_still_path,
                    evidence_audio_path=excluded.evidence_audio_path,
                    replay_video_path=excluded.replay_video_path,
                    manifest_json=excluded.manifest_json,
                    content_fingerprint=excluded.content_fingerprint,
                    schema_version=excluded.schema_version,
                    processing_version=excluded.processing_version,
                    map_set=excluded.map_set, slot=excluded.slot,
                    imported_source=excluded.imported_source,
                    status='available', updated_at=excluded.updated_at
                """,
                (
                    capture_id, still, audio, video, json.dumps(manifest), fingerprint,
                    manifest["schemaVersion"], manifest["processing"]["processingVersion"],
                    source["mapSet"], source["slot"], manifest_path.parent.name,
                    current["created_at"] if current else now, now,
                ),
            )
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
            media_id = quote(capture["id"], safe="")
            rounds.append(
                {
                    **source,
                    "evidenceImageUrl": f"/media/{media_id}/listener.jpg",
                    "audioUrl": f"/media/{media_id}/listener.m4a",
                    "replayVideoUrl": f"/media/{media_id}/replay.mp4",
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
                "pixelsPerMeter": float(image.get("pixels_per_meter") or 1),
                "coordinateSize": float(image.get("coordinate_size") or 2048),
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


class PreviewExpiredError(KeyError):
    pass


@dataclass(frozen=True)
class PreviewSession:
    token: str
    set_id: str
    set_version: int
    expires_at: datetime
    contract: dict[str, Any]
    media: dict[str, Path]


class PreviewSessions:
    def __init__(
        self,
        app: "App",
        ttl: timedelta = PREVIEW_TTL,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self.app = app
        self.ttl = ttl
        self.now = now
        self._items: dict[str, PreviewSession] = {}
        self._lock = Lock()

    def _snapshot(
        self,
        item: dict[str, Any],
        display_date: str,
        token: str,
        expires_at: datetime,
    ) -> PreviewSession:
        date.fromisoformat(display_date)
        catalog = self.app.catalog
        captures = {capture["id"]: capture for capture in self.app.store.list_captures()}
        operator_ids = {operator["id"] for operator in catalog.get("operators", [])}
        scoring_path = self.app.game_repo / "assets" / "data" / "scoring.json"
        scoring = json.loads(scoring_path.read_text(encoding="utf-8"))
        issues: list[dict[str, str]] = []
        if item.get("mapAssetVersion") != catalog["assetVersion"]:
            issues.append({
                "code": "STALE_MAP_ASSET",
                "severity": "warning",
                "message": "This draft was authored against a different map-asset version. Review marker placement before approval.",
            })
        media: dict[str, Path] = {}
        rounds: list[dict[str, Any]] = []
        for index, source in enumerate(item.get("rounds") or [], 1):
            operator_id = source.get("operatorId")
            complete = operator_id in operator_ids and all(
                position_is_valid(source.get(key))
                for key in ("listenerPos", "operatorStartPos", "targetPos")
            )
            if not complete:
                issues.append({
                    "code": f"INCOMPLETE_ROUND_{index}",
                    "severity": "error",
                    "message": f"Round {index} is incomplete. Add its operator and all listener/runner markers in Studio.",
                })
            capture = captures.get(source.get("captureId"))
            image_url: str | None = None
            audio_url: str | None = None
            replay_url: str | None = None
            if capture:
                encoded_id = quote(capture["id"], safe="")
                image_relative = f"{encoded_id}/listener.jpg"
                audio_relative = f"{encoded_id}/listener.m4a"
                replay_relative = f"{encoded_id}/replay.mp4"
                image_path = Path(capture["evidence"]["stillPath"])
                audio_path = Path(capture["evidence"]["audioPath"])
                replay_path = Path(capture["replay"]["videoPath"])
                if image_path.is_file():
                    media[image_relative] = image_path.resolve()
                    image_url = f"/preview/{token}/media/{image_relative}"
                if audio_path.is_file():
                    media[audio_relative] = audio_path.resolve()
                    audio_url = f"/preview/{token}/media/{audio_relative}"
                if replay_path.is_file():
                    media[replay_relative] = replay_path.resolve()
                    replay_url = f"/preview/{token}/media/{replay_relative}"
            if not image_url or not audio_url or not replay_url:
                issues.append({
                    "code": f"MISSING_MEDIA_{index}",
                    "severity": "error",
                    "message": f"Round {index} preview media is missing or unavailable.",
                })
            rounds.append({
                "position": index,
                "operatorId": operator_id if isinstance(operator_id, str) else None,
                "evidenceImageUrl": image_url,
                "videoUrl": audio_url,
                "replayVideoUrl": replay_url,
                "listenerPos": source.get("listenerPos") if position_is_valid(source.get("listenerPos")) else None,
                "operatorStartPos": source.get("operatorStartPos") if position_is_valid(source.get("operatorStartPos")) else None,
                "targetPos": source.get("targetPos") if position_is_valid(source.get("targetPos")) else None,
            })
        while len(rounds) < 3:
            index = len(rounds) + 1
            issues.append({
                "code": f"INCOMPLETE_ROUND_{index}",
                "severity": "error",
                "message": f"Round {index} is missing from this draft.",
            })
            rounds.append({
                "position": index, "operatorId": None, "evidenceImageUrl": None, "videoUrl": None,
                "replayVideoUrl": None, "listenerPos": None,
                "operatorStartPos": None, "targetPos": None,
            })
        contract = {
            "kind": "r6-soundle-preview",
            "schemaVersion": PREVIEW_SCHEMA_VERSION,
            "scoringVersion": int(scoring["version"]),
            "session": {"id": token, "expiresAt": iso_utc(expires_at)},
            "displayDate": display_date,
            "set": {
                "id": item["id"],
                "version": item["version"],
                "mapAssetVersion": item["mapAssetVersion"],
            },
            "assetVersion": catalog["assetVersion"],
            "issues": issues,
            "puzzle": {
                "date": display_date,
                "mapName": item.get("mapName") or item["mapSlug"].replace("-", " ").title(),
                "mapSlug": item["mapSlug"],
                "rounds": rounds[:3],
            },
        }
        return PreviewSession(token, item["id"], item["version"], expires_at, contract, media)

    def create(
        self,
        set_id: str,
        display_date: str,
        expected_version: int | None = None,
    ) -> PreviewSession:
        item = self.app.store.get_set(set_id)
        if not item:
            raise KeyError("Set not found")
        if expected_version is not None and item["version"] != expected_version:
            raise ValueError(
                f"Draft version changed from {expected_version} to {item['version']}; save and preview again"
            )
        token = secrets.token_urlsafe(32)
        expires_at = self.now() + self.ttl
        session = self._snapshot(item, display_date, token, expires_at)
        with self._lock:
            self._items[token] = session
        return session

    def get(self, token: str, allow_expired: bool = False) -> PreviewSession:
        with self._lock:
            session = self._items.get(token)
        if not session:
            raise KeyError("Preview session was not found")
        if not allow_expired and session.expires_at <= self.now():
            raise PreviewExpiredError("This preview session expired. Open a new preview from Studio.")
        return session


@dataclass
class App:
    store: Store
    game_repo: Path
    import_roots: tuple[Path, ...]
    publisher: StudioPublisher | None = None

    def __post_init__(self) -> None:
        self.import_roots = tuple(root.expanduser().resolve() for root in self.import_roots)
        self.importer = DailySetImporter(self.store, self.import_roots, lambda: self.catalog)
        self.previews = PreviewSessions(self)

    def import_single_capture(self, manifest_path: Path) -> dict[str, Any]:
        try:
            resolved = manifest_path.expanduser().resolve(strict=True)
        except OSError as error:
            raise DailySetImportError("The selected capture manifest does not exist") from error
        if not any(root == resolved or root in resolved.parents for root in self.import_roots):
            raise DailySetImportError("The selected manifest is outside the configured import roots")
        return self.store.import_capture(resolved)

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

    def _serve_file(self, root: Path, relative: str, *, no_store: bool = False) -> None:
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
        self.send_header(
            "Cache-Control",
            "no-store" if no_store or candidate.suffix in {".html", ".js", ".mjs"}
            else "public, max-age=3600",
        )
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _preview_parts(path: str) -> tuple[str, str] | None:
        parts = path.strip("/").split("/")
        if len(parts) < 2 or parts[0] != "preview":
            return None
        return parts[1], "/".join(parts[2:])

    @staticmethod
    def _preview_asset_path(relative: str) -> str | None:
        decoded = relative
        for _ in range(3):
            expanded = unquote(decoded)
            if expanded == decoded:
                break
            decoded = expanded
        decoded = decoded.replace("\\", "/").lstrip("/")
        parts = decoded.split("/")
        if "%" in decoded or any(part in {"", ".", ".."} for part in parts):
            return None
        if decoded in GAME_PREVIEW_ASSET_FILES or decoded.startswith(GAME_PREVIEW_ASSET_PREFIXES):
            return decoded
        return None

    def _serve_preview(self, token: str, relative: str) -> None:
        if relative in {"", "index.html"}:
            self.app.previews.get(token, allow_expired=True)
            self._serve_file(self.app.game_repo, "index.html", no_store=True)
            return
        if relative == "api/preview":
            self._json(self.app.previews.get(token).contract)
            return
        if relative.startswith("assets/"):
            asset = self._preview_asset_path(relative[len("assets/"):])
            if not asset:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.app.previews.get(token, allow_expired=True)
            self._serve_file(self.app.game_repo / "assets", asset, no_store=True)
            return
        if relative.startswith("media/"):
            session = self.app.previews.get(token)
            media_relative = relative[len("media/"):]
            candidate = session.media.get(media_relative)
            if not candidate or not candidate.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._serve_file(candidate.parent, candidate.name, no_store=True)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def _media_path(self, relative: str) -> Path | None:
        if "/" not in relative:
            return None
        encoded_id, filename = relative.rsplit("/", 1)
        capture = self.app.store.get_capture(unquote(encoded_id))
        if not capture:
            return None
        mapping = {
            "listener.jpg": capture["evidence"]["stillPath"],
            "listener.m4a": capture["evidence"]["audioPath"],
            "replay.mp4": capture["replay"]["videoPath"],
        }
        return Path(mapping[filename]) if filename in mapping else None

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            preview = self._preview_parts(path)
            if preview:
                self._serve_preview(*preview)
            elif path == "/api/health":
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
            elif path == "/api/publisher/status":
                self._json({"configured": self.app.publisher is not None})
            elif path == "/api/publish-attempts":
                self._json(self.app.publisher.list_attempts() if self.app.publisher else [])
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
        except PreviewExpiredError as error:
            self._json({"error": error.args[0]}, HTTPStatus.GONE)
        except KeyError as error:
            self._json({"error": error.args[0]}, HTTPStatus.NOT_FOUND)
        except Exception as error:
            self._json({"error": str(error)}, 500)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
            if path == "/api/sets":
                self._json(self.app.store.save_set(payload), 201)
            elif path == "/api/previews":
                requested_schema = int(payload.get("schemaVersion", PREVIEW_SCHEMA_VERSION))
                if requested_schema != PREVIEW_SCHEMA_VERSION:
                    self._json(
                        {
                            "error": f"Studio supports preview schema v{PREVIEW_SCHEMA_VERSION}, not v{requested_schema}",
                            "supportedSchemaVersions": [PREVIEW_SCHEMA_VERSION],
                        },
                        HTTPStatus.UPGRADE_REQUIRED,
                    )
                    return
                session = self.app.previews.create(
                    str(payload.get("setId", "")),
                    str(payload.get("displayDate") or date.today().isoformat()),
                    int(payload["setVersion"]) if payload.get("setVersion") is not None else None,
                )
                self._json(
                    {
                        "sessionId": session.token,
                        "schemaVersion": PREVIEW_SCHEMA_VERSION,
                        "expiresAt": iso_utc(session.expires_at),
                        "url": f"/preview/{session.token}/",
                        "issues": session.contract["issues"],
                    },
                    HTTPStatus.CREATED,
                )
            elif path == "/api/captures/import":
                self._json(self.app.import_single_capture(Path(str(payload.get("manifestPath", "")))), 201)
            elif path == "/api/imports/scan":
                self._json(self.app.importer.scan(str(payload.get("directoryPath", ""))))
            elif path == "/api/imports/commit":
                target = payload.get("targetSetId")
                self._json(
                    self.app.importer.commit(
                        str(payload.get("scanId", "")),
                        target_set_id=str(target) if target else None,
                        allow_stale=payload.get("allowStale") is True,
                    ),
                    201,
                )
            elif path == "/api/schedule":
                self._json(self.app.store.schedule(str(payload["releaseDate"]), str(payload["setId"])), 201)
            elif path == "/api/publish":
                if not self.app.publisher:
                    self._json({"error": "Remote publisher is not configured"}, 409)
                    return
                self._json(
                    self.app.publisher.publish(str(payload["releaseDate"]), str(payload["setId"])),
                    201,
                )
            elif path == "/api/releases/publish-due":
                self._json({"published": self.app.store.publish_due()})
            else:
                self._json({"error": "Not found"}, 404)
        except KeyError as error:
            self._json({"error": str(error)}, 404)
        except ScanChangedError as error:
            self._json({"error": str(error), "state": "retry"}, 409)
        except DailySetImportError as error:
            self._json({"error": str(error)}, 400)
        except PublishClientError as error:
            self._json({"error": str(error)}, 502)
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
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Studio is owner-only and must bind to a loopback host")
    game_repo = args.game_repo.resolve()
    import_roots = tuple(
        path.resolve() for path in (args.import_root or [ROOT / "daily sets"])
    )
    store = Store(args.db.resolve())
    publisher_url = args.publisher_url or os.getenv("R6_STUDIO_PUBLISHER_URL", "").strip() or None
    publisher_token = os.getenv("R6_STUDIO_PUBLISHER_TOKEN", "").strip() or None
    publisher = None
    if publisher_url:
        catalog = load_catalog(game_repo)
        scoring = json.loads((game_repo / "assets" / "data" / "scoring.json").read_text(encoding="utf-8"))
        publisher = StudioPublisher(
            store,
            catalog,
            int(scoring["version"]),
            PublisherClient(publisher_url, publisher_token),
        )
    app = App(store, game_repo, import_roots, publisher)
    server = Server((args.host, args.port), app)
    url = f"http://{args.host}:{server.server_port}/"
    print(f"R6 Soundle Studio: {url}")
    print(f"Database: {app.store.db_path}")
    print(f"Game authoring assets: {game_repo}")
    print(f"Remote publisher: {publisher_url or 'not configured'}")
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
    result.add_argument(
        "--import-root", type=Path, action="append",
        help="Allowed processed-media root; may be repeated (default: Studio daily sets)",
    )
    result.add_argument(
        "--publisher-url",
        help="Production-service base URL; credential is read only from R6_STUDIO_PUBLISHER_TOKEN",
    )
    result.add_argument("--no-browser", action="store_true")
    result.add_argument("--self-test", action="store_true")
    return result


def self_test() -> int:
    with tempfile.TemporaryDirectory() as temporary:
        store = Store(Path(temporary) / "studio.db")
        with store.connect() as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(captures)")}
            required = {
                "content_fingerprint", "schema_version", "processing_version",
                "map_set", "slot", "imported_source",
            }
            if not required.issubset(columns):
                raise RuntimeError("Phase 2 capture migration is incomplete")
            publish_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(publish_attempts)")
            }
            if not {"remote_publish_attempt_id", "request_json", "objects_json", "completed_at"}.issubset(publish_columns):
                raise RuntimeError("Phase 6 publish migration is incomplete")
    print("Studio self-test passed: schema migration and transaction setup are ready.")
    return 0


if __name__ == "__main__":
    arguments = parser().parse_args()
    if arguments.self_test:
        raise SystemExit(self_test())
    run_server(arguments)
