"""Synthetic-only Studio workflow support for the Phase 10 launch rehearsal.

The public entry point lives in the game repository.  This module owns the
Studio-specific half so production tooling never depends on Studio test code or
owner media.  Every path supplied here must be inside the caller-created
rehearsal root.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from studio_server import App, Store


STUDIO_ROOT = Path(__file__).resolve().parent


class RehearsalSupportError(RuntimeError):
    """An isolated Studio rehearsal step failed."""


@dataclass(frozen=True)
class StudioRehearsalFixture:
    store: Store
    app: App
    set_item: dict[str, Any]
    preview_contract: dict[str, Any]
    processed_root: Path
    processor_failure: dict[str, Any]
    commands: tuple[dict[str, Any], ...]


def _inside(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as error:
        raise RehearsalSupportError(f"Synthetic path escapes the rehearsal root: {resolved}") from error
    return resolved


def _write_fake_media_tools(root: Path) -> tuple[Path, Path]:
    tools = _inside(root / "fake-media-tools", root)
    tools.mkdir(parents=True, exist_ok=False)
    common = """#!/usr/bin/env python3
import json
import pathlib
import sys

MODE = {mode!r}
args = sys.argv[1:]
path = pathlib.Path(args[-1])
if MODE == "ffmpeg":
    payload = {{".jpg": b"synthetic-jpeg", ".m4a": b"synthetic-audio", ".mp4": b"synthetic-video"}}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload.get(path.suffix.lower(), b"synthetic-output"))
elif "default=nw=1:nk=1" in args:
    print("5.0")
else:
    suffix = path.suffix.lower()
    if suffix == ".jpg":
        value = {{"format": {{"format_name": "image2"}}, "streams": [{{"codec_type": "video", "codec_name": "mjpeg", "width": 1920, "height": 1080}}]}}
    elif suffix == ".m4a":
        value = {{"format": {{"format_name": "mov,mp4", "duration": "4.2"}}, "streams": [{{"codec_type": "audio", "codec_name": "aac", "duration": "4.2"}}]}}
    else:
        value = {{"format": {{"format_name": "mov,mp4", "duration": "4.2"}}, "streams": [{{"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080, "duration": "4.2"}}, {{"codec_type": "audio", "codec_name": "aac", "duration": "4.2"}}]}}
    print(json.dumps(value))
"""
    paths: list[Path] = []
    for mode in ("ffmpeg", "ffprobe"):
        script = tools / f"{mode}.py"
        script.write_text(common.format(mode=mode), encoding="utf-8", newline="\n")
        if os.name == "nt":
            launcher = tools / f"{mode}.cmd"
            launcher.write_text(
                f'@"{sys.executable}" "%~dp0{mode}.py" %*\r\n',
                encoding="utf-8",
                newline="",
            )
        else:
            launcher = tools / mode
            launcher.write_text(
                f"#!/bin/sh\nexec {json.dumps(sys.executable)} {json.dumps(str(script))} \"$@\"\n",
                encoding="utf-8",
                newline="\n",
            )
            launcher.chmod(0o700)
        paths.append(launcher)
    return paths[0], paths[1]


def _run(command: list[str], *, environment: dict[str, str]) -> dict[str, Any]:
    result = subprocess.run(
        command,
        cwd=STUDIO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return {
        "command": command,
        "returnCode": result.returncode,
        "stdout": result.stdout[-4000:],
        "stderr": result.stderr[-4000:],
    }


def _process_synthetic_captures(root: Path) -> tuple[Path, dict[str, Any], tuple[dict[str, Any], ...]]:
    raw = _inside(root / "synthetic-raw", root)
    processed = _inside(root / "synthetic-daily-sets", root)
    raw.mkdir(parents=True, exist_ok=False)
    processed.mkdir(parents=True, exist_ok=False)
    for slot in range(1, 4):
        for role in ("listener", "runner"):
            (raw / f"901-bank-{slot}-{role}.mp4").write_bytes(
                f"synthetic:{slot}:{role}".encode("ascii")
            )
    failure_raw = _inside(root / "synthetic-processor-failure", root)
    failure_raw.mkdir(parents=True, exist_ok=False)
    for role in ("listener", "runner"):
        (failure_raw / f"902-bank-1-{role}.mp4").write_bytes(
            f"synthetic-failure:{role}".encode("ascii")
        )

    ffmpeg, ffprobe = _write_fake_media_tools(root)
    environment = dict(os.environ)
    environment.update(FFMPEG=str(ffmpeg), FFPROBE=str(ffprobe))
    processor = STUDIO_ROOT / "processor" / "process_capture.py"
    success = _run(
        [
            sys.executable,
            str(processor),
            "--input",
            str(raw),
            "--output",
            str(processed),
            "--offset-ms",
            "0",
            "--duration",
            "4.2",
        ],
        environment=environment,
    )
    if success["returnCode"] != 0:
        raise RehearsalSupportError(
            f"Synthetic processor failed: {success['stderr'] or success['stdout']}"
        )

    failure_environment = dict(environment)
    failure_environment["R6_PROCESSOR_FAULT_INJECTION"] = "YES"
    failure = _run(
        [
            sys.executable,
            str(processor),
            "--input",
            str(failure_raw),
            "--output",
            str(processed),
            "--offset-ms",
            "0",
            "--duration",
            "4.2",
            "--failure-after",
            "evidence_audio",
        ],
        environment=failure_environment,
    )
    failed_output = processed / "902-bank" / "1"
    temporary_outputs = list((processed / "902-bank").glob(".1.processing-*"))
    if failure["returnCode"] == 0 or failed_output.exists() or temporary_outputs:
        raise RehearsalSupportError("Processor fault injection left an importable or temporary capture")
    processor_failure = {
        "injectedAfter": "evidence_audio",
        "returnCode": failure["returnCode"],
        "canonicalOutputAbsent": not failed_output.exists(),
        "temporaryOutputs": len(temporary_outputs),
        "rawInputsPreserved": len(list(failure_raw.glob("*.mp4"))) == 2,
    }
    return processed, processor_failure, (success, failure)


def build_studio_fixture(root: Path, game_root: Path, release_date: str) -> StudioRehearsalFixture:
    """Process, import, author, approve, and preview one synthetic three-round set."""
    resolved_root = root.resolve()
    resolved_game = game_root.resolve()
    if not resolved_root.is_dir():
        raise RehearsalSupportError("The caller must create the isolated rehearsal root")
    if not (resolved_game / "index.html").is_file():
        raise RehearsalSupportError("The game checkout is incomplete")

    processed, processor_failure, commands = _process_synthetic_captures(resolved_root)
    store = Store(_inside(resolved_root / "studio.db", resolved_root))
    app = App(store, resolved_game, (processed,))
    scan = app.importer.scan("901-bank")
    if scan["state"] != "valid" or not scan["canCommit"]:
        raise RehearsalSupportError(f"Synthetic daily-set scan failed: {scan}")
    imported = app.importer.commit(scan["scanId"])
    item = imported["set"]

    map_item = next(
        (candidate for candidate in app.catalog["maps"] if candidate["slug"] == "bank"),
        None,
    )
    if map_item is None or not map_item.get("floors"):
        raise RehearsalSupportError("The real game catalog has no authorable Bank floor")
    floor = map_item["floors"][0]
    operator = next(iter(app.catalog.get("operators") or []), None)
    if operator is None:
        raise RehearsalSupportError("The real game catalog has no operator fixture")
    for index, round_item in enumerate(item["rounds"], start=1):
        round_item.update(
            operatorId=operator["id"],
            listenerPos={"x": 0.20 + index * 0.03, "y": 0.28, "floorKey": floor["key"], "angle": index * 45},
            operatorStartPos={"x": 0.35, "y": 0.42 + index * 0.02, "floorKey": floor["key"]},
            targetPos={"x": 0.62 + index * 0.02, "y": 0.58, "floorKey": floor["key"]},
        )
    item.update(
        name="Phase 10 synthetic rehearsal",
        mapName=map_item["name"],
        mapAssetVersion=app.catalog["assetVersion"],
    )
    authored = store.save_set(item, item["id"])
    authored["status"] = "approved"
    approved = store.save_set(authored, authored["id"])
    if approved["status"] != "approved":
        raise RehearsalSupportError("Synthetic set did not reach approved state")

    preview = app.previews.create(approved["id"], release_date, approved["version"])
    if preview.contract.get("issues"):
        raise RehearsalSupportError(f"Real-game preview reported issues: {preview.contract['issues']}")
    if preview.contract["puzzle"]["date"] != release_date:
        raise RehearsalSupportError("Preview did not preserve the synthetic future display date")
    return StudioRehearsalFixture(
        store,
        app,
        approved,
        preview.contract,
        processed,
        processor_failure,
        commands,
    )


def approve_correction(fixture: StudioRehearsalFixture) -> dict[str, Any]:
    """Create a new approved Studio version without mutating captured media."""
    item = fixture.store.get_set(fixture.set_item["id"])
    if not item:
        raise RehearsalSupportError("Synthetic set disappeared before correction")
    item["rounds"][0]["targetPos"]["x"] = min(
        0.95, float(item["rounds"][0]["targetPos"]["x"]) + 0.05
    )
    item["status"] = "draft"
    revised = fixture.store.save_set(item, item["id"])
    revised["status"] = "approved"
    approved = fixture.store.save_set(revised, revised["id"])
    if approved["version"] <= fixture.set_item["version"]:
        raise RehearsalSupportError("Correction did not create a new Studio set version")
    return approved

