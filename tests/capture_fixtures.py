"""Small schema-valid capture fixtures; media bytes are synthetic and private-free."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


NOW = "2026-08-31T12:00:00Z"


def _media(path: Path, kind: str, mime_type: str, **metadata: Any) -> dict[str, Any]:
    return {
        "kind": kind,
        "path": path.name,
        "byteSize": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "mimeType": mime_type,
        **metadata,
    }


def write_capture(
    set_directory: Path,
    slot: int,
    *,
    map_slug: str = "bank",
    set_number: int = 1,
    token: str = "original",
    duration: float = 4.2,
    audio_duration: float | None = None,
    replay_duration: float | None = None,
    operator_id: str | None = None,
) -> Path:
    round_directory = set_directory / str(slot)
    round_directory.mkdir(parents=True, exist_ok=True)
    still = round_directory / "listener.jpg"
    audio = round_directory / "listener.m4a"
    replay = round_directory / "replay.mp4"
    still.write_bytes(f"jpeg:{slot}:{token}".encode())
    audio.write_bytes(f"audio:{slot}:{token}".encode())
    replay.write_bytes(f"video:{slot}:{token}".encode())
    map_set = f"{set_number}-{map_slug}"
    source: dict[str, Any] = {
        "mapSet": map_set,
        "mapSlug": map_slug,
        "setNumber": set_number,
        "slot": slot,
        "listenerSourceName": f"{map_set}-{slot}-listener.mp4",
        "runnerSourceName": f"{map_set}-{slot}-runner.mp4",
    }
    if operator_id:
        source["operatorId"] = operator_id
    manifest = {
        "schemaVersion": 1,
        "id": f"{map_set}/{slot}",
        "source": source,
        "evidence": {"stillPath": "listener.jpg", "audioPath": "listener.m4a"},
        "replay": {"videoPath": "replay.mp4"},
        "alignment": {
            "runnerOffsetMs": 10,
            "confidence": 0.9,
            "correlationMethod": "audio-envelope-v1",
            "measuredAt": NOW,
        },
        "durationSeconds": duration,
        "media": [
            _media(still, "evidence_still", "image/jpeg", codec="mjpeg", container="image2", width=1920, height=1080),
            _media(audio, "evidence_audio", "audio/mp4", codec="aac", container="mov,mp4", durationSeconds=audio_duration or duration),
            _media(replay, "replay_video", "video/mp4", codec="h264", container="mov,mp4", durationSeconds=replay_duration or duration, width=1920, height=1080),
        ],
        "processing": {
            "processingVersion": 1,
            "processorVersion": "r6-soundle-capture/1",
            "settings": {"alignment": "audio-envelope-v1"},
            "processedAt": NOW,
            "sourceFingerprints": [
                {"role": "listener", "name": source["listenerSourceName"], "byteSize": 10, "sha256": "a" * 64},
                {"role": "runner", "name": source["runnerSourceName"], "byteSize": 10, "sha256": "b" * 64},
            ],
        },
        "review": {"status": "needs_review"},
    }
    path = round_directory / "capture.json"
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return path


def write_daily_set(
    root: Path,
    *,
    slots: tuple[int, ...] = (1, 2, 3),
    map_slug: str = "bank",
    set_number: int = 1,
    token: str = "original",
) -> Path:
    directory = root / f"{set_number}-{map_slug}"
    directory.mkdir(parents=True, exist_ok=True)
    for slot in slots:
        write_capture(directory, slot, map_slug=map_slug, set_number=set_number, token=token)
    return directory
