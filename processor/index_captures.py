#!/usr/bin/env python3
"""Safely add capture-v1 manifests to legacy daily-set media directories."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from capture_contract import (
    CAPTURE_SCHEMA_VERSION,
    MEDIA_BY_KIND,
    PROCESSING_VERSION,
    PROCESSOR_VERSION,
    CaptureValidationError,
    hash_file,
    read_and_validate_capture,
    utc_now,
    validate_capture,
)
from process_capture import ProcessingError, executable, fsync_directory, probe_media


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = ROOT / "daily sets"
MAPSET_NAME = re.compile(r"^(?P<number>[1-9]\d*)-(?P<slug>[a-z0-9]+(?:-[a-z0-9]+)*)$")
CANONICAL_FILES = {name for name, _mime in MEDIA_BY_KIND.values()}
ALLOWED_FILES = CANONICAL_FILES | {"capture.json"}


class IndexingError(RuntimeError):
    pass


@dataclass(frozen=True)
class RoundPlan:
    directory: Path
    map_set: str
    map_slug: str
    set_number: int
    slot: int
    existing: bool

    @property
    def capture_id(self) -> str:
        return f"{self.map_set}/{self.slot}"


def _regular_file(path: Path) -> None:
    if path.is_symlink():
        raise IndexingError(f"Symlinks are not allowed in legacy captures: {path}")
    if not path.is_file():
        raise IndexingError(f"Expected a regular media file: {path}")
    if path.stat().st_size <= 0:
        raise IndexingError(f"Legacy media file is empty: {path}")


def scan_legacy_root(root: Path) -> list[RoundPlan]:
    root = root.expanduser()
    if root.is_symlink():
        raise IndexingError(f"Legacy root may not be a symlink: {root}")
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise IndexingError(f"Legacy root must be a real directory: {root}")
    plans: list[RoundPlan] = []
    seen_ids: dict[str, Path] = {}
    entries = sorted(root.iterdir(), key=lambda item: item.name.casefold())
    if not entries:
        raise IndexingError(f"Legacy root is empty: {root}")
    for mapset_dir in entries:
        if mapset_dir.is_symlink() or not mapset_dir.is_dir():
            raise IndexingError(f"Unexpected item in legacy root: {mapset_dir.name}")
        match = MAPSET_NAME.fullmatch(mapset_dir.name)
        if not match:
            raise IndexingError(f"Unexpected map-set directory name: {mapset_dir.name}")
        slot_entries = sorted(mapset_dir.iterdir(), key=lambda item: item.name)
        slot_names = [item.name for item in slot_entries]
        if slot_names != ["1", "2", "3"]:
            raise IndexingError(
                f"{mapset_dir.name} must contain exactly slot directories 1, 2, and 3"
            )
        for slot_dir in slot_entries:
            if slot_dir.is_symlink() or not slot_dir.is_dir():
                raise IndexingError(f"Slot must be a real directory: {slot_dir}")
            names = {entry.name for entry in slot_dir.iterdir()}
            unexpected = sorted(names - ALLOWED_FILES)
            missing = sorted(CANONICAL_FILES - names)
            if unexpected:
                raise IndexingError(f"Unexpected file(s) in {slot_dir}: {', '.join(unexpected)}")
            if missing:
                raise IndexingError(f"Partial legacy round {slot_dir}; missing {', '.join(missing)}")
            for name in CANONICAL_FILES:
                _regular_file(slot_dir / name)
                # Dry-run still proves bounded hashing works against every input byte.
                hash_file(slot_dir / name)
            plan = RoundPlan(
                directory=slot_dir,
                map_set=mapset_dir.name,
                map_slug=match.group("slug"),
                set_number=int(match.group("number")),
                slot=int(slot_dir.name),
                existing="capture.json" in names,
            )
            folded = plan.capture_id.casefold()
            if folded in seen_ids:
                raise IndexingError(
                    f"Duplicate capture id {plan.capture_id}: {seen_ids[folded]} and {slot_dir}"
                )
            seen_ids[folded] = slot_dir
            if plan.existing:
                try:
                    existing = read_and_validate_capture(slot_dir / "capture.json")
                except CaptureValidationError as error:
                    raise IndexingError(f"Conflicting manifest in {slot_dir}: {error}") from error
                if existing["id"] != plan.capture_id:
                    raise IndexingError(f"Manifest id conflicts with directory {slot_dir}")
            plans.append(plan)
    return plans


def build_legacy_manifest(
    plan: RoundPlan,
    ffprobe: str,
    probe: Callable[[Path, str, str], dict[str, object]] = probe_media,
) -> dict[str, object]:
    media = [
        probe(plan.directory / "listener.jpg", "evidence_still", ffprobe),
        probe(plan.directory / "listener.m4a", "evidence_audio", ffprobe),
        probe(plan.directory / "replay.mp4", "replay_video", ffprobe),
    ]
    durations = [
        float(item["durationSeconds"])
        for item in media
        if item["kind"] in {"evidence_audio", "replay_video"}
    ]
    manifest: dict[str, object] = {
        "schemaVersion": CAPTURE_SCHEMA_VERSION,
        "id": plan.capture_id,
        "source": {
            "mapSet": plan.map_set,
            "mapSlug": plan.map_slug,
            "setNumber": plan.set_number,
            "slot": plan.slot,
            "listenerSourceName": "legacy-unavailable",
            "runnerSourceName": "legacy-unavailable",
        },
        "evidence": {"stillPath": "listener.jpg", "audioPath": "listener.m4a"},
        "replay": {"videoPath": "replay.mp4"},
        "alignment": {
            "runnerOffsetMs": 0,
            "confidence": 0,
            "correlationMethod": "legacy-unavailable",
            "measuredAt": utc_now(),
        },
        "durationSeconds": min(durations),
        "media": media,
        "processing": {
            "processingVersion": PROCESSING_VERSION,
            "processorVersion": f"{PROCESSOR_VERSION}+legacy-index",
            "settings": {"alignment": "legacy-unavailable"},
            "processedAt": utc_now(),
            "sourceFingerprints": [],
        },
        "review": {"status": "needs_review", "note": "Indexed from legacy processed media."},
    }
    validate_capture(manifest, plan.directory)
    return manifest


def _write_new_manifest(plan: RoundPlan, manifest: dict[str, object]) -> None:
    temporary = plan.directory / "capture.json.tmp"
    target = plan.directory / "capture.json"
    if target.exists():
        raise IndexingError(f"Manifest appeared during indexing: {target}")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(manifest, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        fsync_directory(plan.directory)
    finally:
        temporary.unlink(missing_ok=True)


def index_legacy_root(
    root: Path,
    *,
    write_manifests: bool = False,
    ffprobe: str | None = None,
    probe: Callable[[Path, str, str], dict[str, object]] = probe_media,
) -> tuple[int, int]:
    plans = scan_legacy_root(root)
    pending = [plan for plan in plans if not plan.existing]
    if not write_manifests:
        for plan in pending:
            print(f"WOULD INDEX {plan.capture_id} at {plan.directory}")
        return len(pending), len(plans) - len(pending)

    ffprobe_path = ffprobe or executable("ffprobe")
    # Probe and validate the complete tree before writing the first manifest.
    manifests = [(plan, build_legacy_manifest(plan, ffprobe_path, probe)) for plan in pending]
    written: list[Path] = []
    try:
        for plan, manifest in manifests:
            _write_new_manifest(plan, manifest)
            written.append(plan.directory / "capture.json")
            print(f"INDEXED {plan.capture_id} at {plan.directory}")
    except BaseException:
        for path in written:
            path.unlink(missing_ok=True)
        raise
    return len(written), len(plans) - len(pending)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    mode = result.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Report actions without writing (default)")
    mode.add_argument("--write-manifests", action="store_true", help="Write only missing capture.json files")
    result.add_argument("--ffprobe", help="Explicit ffprobe executable used in write mode")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        changed, unchanged = index_legacy_root(
            args.root,
            write_manifests=args.write_manifests,
            ffprobe=args.ffprobe,
        )
    except (IndexingError, ProcessingError, CaptureValidationError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    action = "written" if args.write_manifests else "planned"
    print(f"Legacy capture index complete: {changed} {action}, {unchanged} unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
