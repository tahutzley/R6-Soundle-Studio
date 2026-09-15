"""Validated, transactional daily-set imports for the local Studio."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from processor.capture_contract import (
    CaptureValidationError,
    hash_file,
    read_and_validate_capture,
    resolve_capture_file,
)


MAP_SET_PATTERN = re.compile(r"^(?P<number>[1-9][0-9]*)-(?P<slug>[a-z0-9]+(?:-[a-z0-9]+)*)$")
CAPTURE_MAP_SLUG_ALIASES = {
    "casino": "calypso-casino",
    "nighthaven": "nighthavenlabs",
    "theme": "themepark",
}
BACKUP_PATTERN = re.compile(r"^\.[123]\.backup-[0-9TZ._+-]+-[a-f0-9]{8}$")
ROUND_FILES = {"capture.json", "listener.jpg", "listener.m4a", "replay.mp4"}
LEGACY_MEDIA_FILES = ROUND_FILES - {"capture.json"}
LEGACY_MANIFEST_ERROR = "capture manifest has not been generated for this legacy media"
DURATION_TOLERANCE_SECONDS = 0.5


class DailySetImportError(ValueError):
    """A safe, user-actionable daily-set import failure."""


class ScanChangedError(DailySetImportError):
    """The selected files no longer match the read-only scan."""


@dataclass(frozen=True)
class SlotScan:
    slot: int
    manifest_path: Path | None
    manifest: dict[str, Any] | None
    content_fingerprint: str | None
    input_fingerprint: str | None
    errors: tuple[str, ...]


@dataclass(frozen=True)
class DailySetScan:
    scan_id: str
    directory: Path
    source_label: str
    map_set: str
    map_slug: str
    set_number: int
    fingerprint: str | None
    slots: tuple[SlotScan, ...]
    report: dict[str, Any]


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def capture_content_fingerprint(manifest: dict[str, Any]) -> str:
    """Hash authored capture content while ignoring review and wall-clock metadata."""
    processing = dict(manifest["processing"])
    processing.pop("processedAt", None)
    content = {
        "schemaVersion": manifest["schemaVersion"],
        "id": manifest["id"],
        "source": manifest["source"],
        "evidence": manifest["evidence"],
        "replay": manifest["replay"],
        "alignment": manifest["alignment"],
        "durationSeconds": manifest["durationSeconds"],
        "media": manifest["media"],
        "processing": processing,
    }
    return hashlib.sha256(_canonical_json(content)).hexdigest()


def _input_fingerprint(manifest_path: Path, manifest: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    digest.update(manifest_path.read_bytes())
    digest.update(b"\0")
    for media in sorted(manifest["media"], key=lambda item: item["kind"]):
        path = resolve_capture_file(manifest_path.parent, media["path"], f"media[{media['kind']}].path")
        digest.update(media["path"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(path.stat().st_size).encode("ascii"))
        digest.update(b"\0")
        digest.update(hash_file(path).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _validate_durations(manifest: dict[str, Any]) -> None:
    expected = float(manifest["durationSeconds"])
    tolerance = max(DURATION_TOLERANCE_SECONDS, expected * 0.02)
    for media in manifest["media"]:
        if media["kind"] not in {"evidence_audio", "replay_video"}:
            continue
        actual = float(media["durationSeconds"])
        if abs(actual - expected) > tolerance:
            raise CaptureValidationError(
                f"{media['path']} duration {actual:.3f}s does not match capture duration {expected:.3f}s"
            )


def _inside_root(path: Path, roots: tuple[Path, ...]) -> tuple[Path, str]:
    for root in roots:
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if not relative.parts:
            raise DailySetImportError("Choose one map-set directory inside an import root")
        return root, f"{root.name}/{relative.as_posix()}"
    raise DailySetImportError("The selected directory is outside the configured import roots")


def _resolve_selected_directory(value: str | Path, roots: tuple[Path, ...]) -> tuple[Path, str]:
    raw = Path(value).expanduser()
    candidates = [raw] if raw.is_absolute() else [*(root / raw for root in roots), Path.cwd() / raw]
    selected = next((candidate for candidate in candidates if candidate.exists()), candidates[0])
    try:
        selected = selected.resolve(strict=True)
    except OSError as error:
        raise DailySetImportError("The selected daily-set directory does not exist") from error
    if not selected.is_dir():
        raise DailySetImportError("Choose a daily-set directory, not a file")
    for candidate in [selected, *selected.parents]:
        if candidate in roots:
            break
        if candidate.is_symlink():
            raise DailySetImportError("Import paths may not traverse a symlink")
    _, label = _inside_root(selected, roots)
    return selected, label


def _slot_payload(slot: SlotScan, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    manifest = slot.manifest or {}
    source = manifest.get("source", {})
    legacy = slot.errors == (LEGACY_MANIFEST_ERROR,)
    relation = "new"
    if existing:
        relation = "identical" if existing.get("contentFingerprint") == slot.content_fingerprint else "reprocessed"
    return {
        "slot": slot.slot,
        "state": "valid" if not slot.errors else ("legacy" if legacy else "invalid"),
        "captureId": manifest.get("id"),
        "summary": "Media ready; capture manifest missing" if legacy else None,
        "operatorId": source.get("operatorId"),
        "durationSeconds": manifest.get("durationSeconds"),
        "schemaVersion": manifest.get("schemaVersion"),
        "processingVersion": manifest.get("processing", {}).get("processingVersion"),
        "relation": relation,
        "errors": list(slot.errors),
        "manualFields": [] if not slot.manifest else [
            "Confirm runner operator" if source.get("operatorId") else "Choose runner operator",
            "Place listener position",
            "Set listener direction",
            "Place runner start",
            "Place runner target",
        ],
    }


class DailySetImporter:
    """Owns transient scans and commits them through a Store transaction."""

    def __init__(
        self,
        store: Any,
        import_roots: list[Path] | tuple[Path, ...],
        catalog_provider: Callable[[], dict[str, Any]],
    ) -> None:
        self.store = store
        self.roots = tuple(root.expanduser().resolve() for root in import_roots)
        self.catalog_provider = catalog_provider
        self.scans: dict[str, DailySetScan] = {}

    def _filesystem_scan(self, selected: str | Path) -> DailySetScan:
        directory, source_label = _resolve_selected_directory(selected, self.roots)
        match = MAP_SET_PATTERN.fullmatch(directory.name)
        if not match:
            raise DailySetImportError("Daily-set directories must be named <set-number>-<map-slug>")
        map_set = directory.name
        capture_map_slug = match.group("slug")
        map_slug = CAPTURE_MAP_SLUG_ALIASES.get(capture_map_slug, capture_map_slug)
        set_number = int(match.group("number"))
        scan_id = uuid.uuid4().hex
        entries = {entry.name: entry for entry in directory.iterdir()}
        global_errors: list[str] = []
        ignored_backups = sorted(
            name for name, entry in entries.items()
            if BACKUP_PATTERN.fullmatch(name) and entry.is_dir() and not entry.is_symlink()
        )
        unexpected = sorted(
            name for name in entries
            if name not in {"1", "2", "3"} and name not in ignored_backups
        )
        if unexpected:
            global_errors.append(f"Unexpected round entries: {', '.join(unexpected)}")

        catalog = self.catalog_provider()
        maps = {item["slug"]: item for item in catalog.get("maps", [])}
        operators = {item["id"] for item in catalog.get("operators", [])}
        if map_slug not in maps:
            global_errors.append(f"Map {map_slug!r} is not present in the current game catalog")

        slots: list[SlotScan] = []
        seen_ids: dict[str, int] = {}
        for slot in range(1, 4):
            slot_dir = entries.get(str(slot))
            errors: list[str] = []
            manifest: dict[str, Any] | None = None
            content_fingerprint: str | None = None
            input_fingerprint: str | None = None
            manifest_path: Path | None = None
            if slot_dir is None:
                errors.append("Round directory is missing")
            elif slot_dir.is_symlink() or not slot_dir.is_dir():
                errors.append("Round entry must be a regular directory")
            else:
                manifest_path = slot_dir / "capture.json"
                round_entries = {entry.name for entry in slot_dir.iterdir()}
                if round_entries == LEGACY_MEDIA_FILES:
                    try:
                        digest = hashlib.sha256()
                        for name in sorted(LEGACY_MEDIA_FILES):
                            media_path = slot_dir / name
                            if media_path.is_symlink() or not media_path.is_file() or media_path.stat().st_size <= 0:
                                raise CaptureValidationError(f"{name} must be a non-empty regular media file")
                            digest.update(name.encode("utf-8"))
                            digest.update(str(media_path.stat().st_size).encode("ascii"))
                            digest.update(hash_file(media_path).encode("ascii"))
                        input_fingerprint = digest.hexdigest()
                        errors.append(LEGACY_MANIFEST_ERROR)
                    except (CaptureValidationError, OSError) as error:
                        errors.append(str(error))
                else:
                    try:
                        unexpected_round_files = sorted(round_entries - ROUND_FILES)
                        missing_round_files = sorted(ROUND_FILES - round_entries)
                        if unexpected_round_files:
                            raise CaptureValidationError(
                                f"unexpected round file(s): {', '.join(unexpected_round_files)}"
                            )
                        if missing_round_files:
                            raise CaptureValidationError(
                                f"missing round file(s): {', '.join(missing_round_files)}"
                            )
                        manifest = read_and_validate_capture(manifest_path)
                        _validate_durations(manifest)
                        source = manifest["source"]
                        if (
                            source["mapSet"] != map_set
                            or source["mapSlug"] != capture_map_slug
                            or source["setNumber"] != set_number
                        ):
                            raise CaptureValidationError("manifest source does not match the daily-set directory")
                        if source["slot"] != slot:
                            raise CaptureValidationError("manifest slot does not match its round directory")
                        if source.get("operatorId") and source["operatorId"] not in operators:
                            raise CaptureValidationError(
                                f"operator {source['operatorId']!r} is not present in the current operator catalog"
                            )
                        folded = manifest["id"].casefold()
                        if folded in seen_ids:
                            raise CaptureValidationError(f"duplicate capture id also used by slot {seen_ids[folded]}")
                        seen_ids[folded] = slot
                        content_fingerprint = capture_content_fingerprint(manifest)
                        input_fingerprint = _input_fingerprint(manifest_path, manifest)
                    except (CaptureValidationError, OSError) as error:
                        errors.append(str(error))
            slots.append(
                SlotScan(slot, manifest_path, manifest, content_fingerprint, input_fingerprint, tuple(errors))
            )

        valid_inputs = not global_errors and all(not slot.errors for slot in slots)
        legacy_inputs = not global_errors and all(
            slot.errors == (LEGACY_MANIFEST_ERROR,) and slot.input_fingerprint
            for slot in slots
        )
        fingerprint = None
        if valid_inputs or legacy_inputs:
            fingerprint = hashlib.sha256(
                _canonical_json(
                    {
                        "mapSet": map_set,
                        "entries": ["1", "2", "3"],
                        "slots": [slot.input_fingerprint for slot in slots],
                    }
                )
            ).hexdigest()

        report = {
            "scanId": scan_id,
            "state": "valid" if valid_inputs else (
                "legacy_unindexed" if legacy_inputs else ("empty" if not entries else "partially_invalid")
            ),
            "canCommit": valid_inputs,
            "fingerprint": fingerprint,
            "source": {
                "label": source_label,
                "mapSet": map_set,
                "mapSlug": map_slug,
                "mapName": maps.get(map_slug, {}).get("name", map_slug.replace("-", " ").title()),
                "setNumber": set_number,
                "mapAssetVersion": str(catalog.get("assetVersion") or "game-checkout"),
                "ignoredBackups": ignored_backups,
            },
            "slots": [],
            "conflicts": global_errors,
            "draftOptions": [],
            "draftMatch": None,
            "requiresStaleConfirmation": False,
            "isIdempotentRetry": False,
            "legacyIndexerAvailable": shutil.which("ffprobe") is not None,
            "legacyIndexCommand": (
                f'python processor\\index_captures.py --root "daily sets\\{map_set}" --write-manifests'
                if legacy_inputs else None
            ),
        }
        if legacy_inputs:
            if report["legacyIndexerAvailable"]:
                report["message"] = (
                    "The media predates capture-v1. Back up this set, run the one-set preparation command below, "
                    "then scan it again. Media bytes are not re-encoded or renamed."
                )
            else:
                report["message"] = (
                    "The media predates capture-v1. FFprobe is not available, so install FFmpeg and restart Studio; "
                    "then back up this set, run the one-set command below, and scan again."
                )
        return DailySetScan(
            scan_id, directory, source_label, map_set, map_slug, set_number,
            fingerprint, tuple(slots), report,
        )

    @staticmethod
    def _capture_ids(scan: DailySetScan) -> list[str]:
        return [str(slot.manifest["id"]) for slot in scan.slots if slot.manifest]

    def _assess_database(self, scan: DailySetScan) -> DailySetScan:
        report = dict(scan.report)
        existing_by_id = {item["id"]: item for item in self.store.list_captures()}
        existing_by_identity = {
            (item.get("mapSet"), item.get("slot")): item
            for item in existing_by_id.values()
            if item.get("mapSet") and item.get("slot")
        }
        report["slots"] = [
            _slot_payload(slot, existing_by_id.get(str(slot.manifest.get("id"))) if slot.manifest else None)
            for slot in scan.slots
        ]
        if not report["canCommit"]:
            return DailySetScan(**{**scan.__dict__, "report": report})

        capture_ids = self._capture_ids(scan)
        conflicts = list(report["conflicts"])
        reprocessed_ids: list[str] = []
        for slot in report["slots"]:
            existing = existing_by_id.get(slot["captureId"])
            identity_match = existing_by_identity.get((scan.map_set, slot["slot"]))
            if identity_match and identity_match["id"] != slot["captureId"]:
                conflicts.append(
                    f"Map set {scan.map_set} slot {slot['slot']} is already owned by {identity_match['id']}"
                )
            if not existing:
                continue
            if existing.get("mapSet") not in {None, scan.map_set} or existing.get("slot") not in {None, slot["slot"]}:
                conflicts.append(f"{slot['captureId']} conflicts with existing capture identity metadata")
            elif slot["relation"] == "reprocessed":
                reprocessed_ids.append(slot["captureId"])

        draft_options: list[dict[str, Any]] = []
        exact_matches: list[dict[str, Any]] = []
        for item in self.store.list_sets():
            if item.get("kind") != "daily":
                continue
            if item.get("mapSlug") != scan.map_slug:
                continue
            if item["status"] == "archived":
                if reprocessed_ids and any(
                    round_item.get("captureId") in set(reprocessed_ids)
                    for round_item in item.get("rounds", [])
                ):
                    conflicts.append(f"Archived set {item['name']} uses reprocessed capture content")
                continue
            exact = item.get("importedMapSet") == scan.map_set
            if item["status"] != "draft" and not exact:
                continue
            attached = [round_item.get("captureId") for round_item in item.get("rounds", [])]
            compatible = len(attached) == 3 and all(not value or value == capture_ids[index] for index, value in enumerate(attached))
            if not compatible:
                continue
            option = {"id": item["id"], "name": item["name"], "exactMatch": exact}
            draft_options.append(option)
            if exact:
                exact_matches.append(option)
        if len(exact_matches) > 1:
            conflicts.append("More than one draft claims this imported map set")

        scheduled_set_ids = {entry["set_id"] for entry in self.store.list_schedule()}
        stale_sets = []
        if reprocessed_ids:
            changed = set(reprocessed_ids)
            for set_id in scheduled_set_ids:
                item = self.store.get_set(set_id)
                if item and any(round_item.get("captureId") in changed for round_item in item.get("rounds", [])):
                    stale_sets.append({"id": set_id, "name": item["name"]})

        if conflicts:
            state = "conflict"
        elif stale_sets:
            state = "stale"
        elif reprocessed_ids:
            state = "reprocessed"
        else:
            state = "valid"
        draft_match = exact_matches[0] if len(exact_matches) == 1 else None
        attached_match = False
        if draft_match:
            draft = self.store.get_set(draft_match["id"])
            attached_match = bool(
                draft
                and [round_item.get("captureId") for round_item in draft["rounds"]] == capture_ids
                and draft.get("importFingerprint") == scan.fingerprint
            )
        report.update(
            state=state,
            canCommit=not conflicts,
            conflicts=conflicts,
            draftOptions=draft_options,
            draftMatch=draft_match,
            reprocessedCaptureIds=reprocessed_ids,
            staleDrafts=stale_sets,
            requiresStaleConfirmation=bool(stale_sets),
            isIdempotentRetry=attached_match and all(slot["relation"] == "identical" for slot in report["slots"]),
        )
        return DailySetScan(**{**scan.__dict__, "report": report})

    def scan(self, selected: str | Path) -> dict[str, Any]:
        scan = self._assess_database(self._filesystem_scan(selected))
        if len(self.scans) >= 64:
            self.scans.pop(next(iter(self.scans)))
        self.scans[scan.scan_id] = scan
        return scan.report

    def _rescan(self, original: DailySetScan) -> DailySetScan:
        fresh = self._assess_database(self._filesystem_scan(original.directory))
        if fresh.fingerprint != original.fingerprint:
            raise ScanChangedError("The selected files changed after scanning; scan the directory again")
        return fresh

    def commit(
        self,
        scan_id: str,
        *,
        target_set_id: str | None = None,
        allow_stale: bool = False,
        failure_hook: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        original = self.scans.get(scan_id)
        if not original:
            raise DailySetImportError("This scan is no longer available; scan the directory again")
        if not original.report["canCommit"]:
            raise DailySetImportError("Resolve the scan errors before importing")
        scan = self._rescan(original)
        if not scan.report["canCommit"]:
            raise DailySetImportError("The import now conflicts with Studio data; scan again")
        if scan.report["requiresStaleConfirmation"] and not allow_stale:
            raise DailySetImportError("Confirm that the scheduled draft will become stale before importing")

        options = {item["id"]: item for item in scan.report["draftOptions"]}
        if target_set_id and target_set_id not in options:
            raise DailySetImportError("The selected draft is no longer a compatible import target")
        if not target_set_id and scan.report["draftMatch"]:
            target_set_id = scan.report["draftMatch"]["id"]

        now = self.store.now_text()
        changed_captures: set[str] = set()
        inserted = 0
        updated = 0
        draft_changed = False
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            transaction_scan = self._filesystem_scan(original.directory)
            if transaction_scan.fingerprint != original.fingerprint:
                raise ScanChangedError("The selected files changed during import; scan the directory again")
            scan = DailySetScan(**{**transaction_scan.__dict__, "report": scan.report})
            current_rows = {
                row["id"]: row
                for row in connection.execute("SELECT * FROM captures").fetchall()
            }
            for slot in scan.slots:
                assert slot.manifest and slot.manifest_path and slot.content_fingerprint
                manifest = slot.manifest
                capture_id = manifest["id"]
                current = current_rows.get(capture_id)
                same = bool(current and current["content_fingerprint"] == slot.content_fingerprint)
                if not same:
                    changed_captures.add(capture_id)
                    status = "available"
                    paths = {
                        "still": str(resolve_capture_file(slot.manifest_path.parent, manifest["evidence"]["stillPath"], "evidence.stillPath")),
                        "audio": str(resolve_capture_file(slot.manifest_path.parent, manifest["evidence"]["audioPath"], "evidence.audioPath")),
                        "video": str(resolve_capture_file(slot.manifest_path.parent, manifest["replay"]["videoPath"], "replay.videoPath")),
                    }
                    connection.execute(
                        """
                        INSERT INTO captures
                            (id, status, evidence_still_path, evidence_audio_path, replay_video_path,
                             manifest_json, content_fingerprint, schema_version, processing_version,
                             map_set, slot, imported_source, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET
                            status=excluded.status,
                            evidence_still_path=excluded.evidence_still_path,
                            evidence_audio_path=excluded.evidence_audio_path,
                            replay_video_path=excluded.replay_video_path,
                            manifest_json=excluded.manifest_json,
                            content_fingerprint=excluded.content_fingerprint,
                            schema_version=excluded.schema_version,
                            processing_version=excluded.processing_version,
                            map_set=excluded.map_set, slot=excluded.slot,
                            imported_source=excluded.imported_source,
                            updated_at=excluded.updated_at
                        """,
                        (
                            capture_id, status, paths["still"], paths["audio"], paths["video"],
                            json.dumps(manifest, separators=(",", ":")), slot.content_fingerprint,
                            manifest["schemaVersion"], manifest["processing"]["processingVersion"],
                            scan.map_set, slot.slot, scan.source_label, current["created_at"] if current else now, now,
                        ),
                    )
                    if current:
                        updated += 1
                    else:
                        inserted += 1
                if failure_hook:
                    failure_hook(f"capture:{slot.slot}")

            capture_ids = self._capture_ids(scan)
            affected_rows = []
            if changed_captures:
                for row in connection.execute("SELECT * FROM puzzle_sets").fetchall():
                    content = json.loads(row["content_json"])
                    if any(round_item.get("captureId") in changed_captures for round_item in content.get("rounds", [])):
                        affected_rows.append((row, content))
            scheduled_ids = {
                row["set_id"] for row in connection.execute("SELECT set_id FROM schedule_entries").fetchall()
            }
            stale_now = [row["id"] for row, _ in affected_rows if row["id"] in scheduled_ids]
            if stale_now and not allow_stale:
                raise DailySetImportError(
                    "A scheduled draft began using this content; scan again and confirm the stale transition"
                )

            target_row = None
            if target_set_id:
                target_row = connection.execute("SELECT * FROM puzzle_sets WHERE id=?", (target_set_id,)).fetchone()
                if not target_row:
                    raise DailySetImportError("The selected draft no longer exists")
                target_content = json.loads(target_row["content_json"])
                attached = [item.get("captureId") for item in target_content.get("rounds", [])]
                target_exact = target_content.get("importedMapSet") == scan.map_set
                target_compatible = (
                    target_content.get("mapSlug") == scan.map_slug
                    and len(attached) == 3
                    and all(not value or value == capture_ids[index] for index, value in enumerate(attached))
                    and (target_row["status"] == "draft" or target_exact)
                )
                if not target_compatible:
                    raise DailySetImportError("The selected draft changed and is no longer a compatible import target")

            for row, content in affected_rows:
                if target_set_id and row["id"] == target_set_id:
                    continue
                version = int(row["version"]) + 1
                content["status"] = "draft"
                content["version"] = version
                connection.execute(
                    "UPDATE puzzle_sets SET status='draft', version=?, content_json=?, updated_at=? WHERE id=?",
                    (version, json.dumps(content, separators=(",", ":")), now, row["id"]),
                )
                if failure_hook:
                    failure_hook(f"stale:{row['id']}")

            if target_row:
                content = json.loads(target_row["content_json"])
                rounds = content.get("rounds") or [{"position": position} for position in range(1, 4)]
                attachments_changed = [item.get("captureId") for item in rounds] != capture_ids
                for index, slot in enumerate(scan.slots):
                    manifest = slot.manifest or {}
                    rounds[index]["position"] = index + 1
                    rounds[index]["captureId"] = capture_ids[index]
                    if not rounds[index].get("operatorId") and manifest.get("source", {}).get("operatorId"):
                        rounds[index]["operatorId"] = manifest["source"]["operatorId"]
                draft_changed = bool(
                    attachments_changed
                    or changed_captures
                    or content.get("importFingerprint") != scan.fingerprint
                    or content.get("importedMapSet") != scan.map_set
                )
                if draft_changed:
                    version = int(target_row["version"]) + 1
                    content.update(
                        rounds=rounds, status="draft", version=version,
                        importedMapSet=scan.map_set, importFingerprint=scan.fingerprint,
                        mapAssetVersion=scan.report["source"]["mapAssetVersion"],
                    )
                    connection.execute(
                        """UPDATE puzzle_sets SET map_asset_version=?, status='draft', version=?,
                           content_json=?, updated_at=? WHERE id=?""",
                        (
                            content["mapAssetVersion"], version,
                            json.dumps(content, separators=(",", ":")), now, target_set_id,
                        ),
                    )
            else:
                target_set_id = f"set_{uuid.uuid4().hex[:12]}"
                rounds = []
                for index, slot in enumerate(scan.slots):
                    source = (slot.manifest or {}).get("source", {})
                    rounds.append(
                        {
                            "position": index + 1,
                            "operatorId": source.get("operatorId"),
                            "listenerPos": None,
                            "operatorStartPos": None,
                            "targetPos": None,
                            "captureId": capture_ids[index],
                        }
                    )
                content = {
                    "id": target_set_id,
                    "kind": "daily",
                    "name": f"Set {scan.set_number} · {scan.report['source']['mapName']}",
                    "mapSlug": scan.map_slug,
                    "mapName": scan.report["source"]["mapName"],
                    "mapAssetVersion": scan.report["source"]["mapAssetVersion"],
                    "status": "draft",
                    "version": 1,
                    "rounds": rounds,
                    "importedMapSet": scan.map_set,
                    "importFingerprint": scan.fingerprint,
                }
                connection.execute(
                    """INSERT INTO puzzle_sets
                       (id, name, map_slug, map_asset_version, status, version,
                        content_json, created_at, updated_at)
                       VALUES (?, ?, ?, ?, 'draft', 1, ?, ?, ?)""",
                    (
                        target_set_id, content["name"], scan.map_slug, content["mapAssetVersion"],
                        json.dumps(content, separators=(",", ":")), now, now,
                    ),
                )
                draft_changed = True
            if failure_hook:
                failure_hook("draft")

        result = {
            "state": "success",
            "scanId": scan_id,
            "set": self.store.get_set(target_set_id),
            "captures": [self.store.get_capture(capture_id) for capture_id in self._capture_ids(scan)],
            "insertedCaptures": inserted,
            "updatedCaptures": updated,
            "draftChanged": draft_changed,
            "idempotent": inserted == 0 and updated == 0 and not draft_changed,
            "manualFields": [item["manualFields"] for item in scan.report["slots"]],
        }
        self.scans[scan_id] = self._assess_database(scan)
        return result
