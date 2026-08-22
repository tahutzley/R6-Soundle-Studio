#!/usr/bin/env python3
"""Turn two synchronized OBS recordings into compact R6 Soundle media."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import uuid
from array import array
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "media" / "processed"
SAMPLE_RATE = 8_000
WINDOW_MS = 10


class ProcessingError(RuntimeError):
    pass


def executable(name: str) -> str:
    override = os.environ.get(name.upper())
    candidate = override or shutil.which(name)
    if not candidate:
        raise ProcessingError(f"{name} was not found. Install FFmpeg or set the {name.upper()} environment variable.")
    return candidate


def run(command: list[str], capture: bool = False) -> bytes:
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise ProcessingError(detail[-1] if detail else f"Command failed: {command[0]}")
    return result.stdout if capture else b""


def probe_duration(path: Path, ffprobe: str) -> float:
    raw = run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        capture=True,
    )
    try:
        return float(raw.decode().strip())
    except ValueError as error:
        raise ProcessingError(f"Could not read duration for {path}") from error


def decode_mono(path: Path, ffmpeg: str) -> array:
    raw = run(
        [ffmpeg, "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"],
        capture=True,
    )
    samples = array("h")
    samples.frombytes(raw)
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        raise ProcessingError(f"No audio stream was found in {path}")
    return samples


def envelope(samples: array, window_samples: int | None = None) -> list[float]:
    size = window_samples or SAMPLE_RATE * WINDOW_MS // 1000
    values = []
    for start in range(0, len(samples) - size + 1, size):
        chunk = samples[start:start + size]
        values.append(math.sqrt(sum(value * value for value in chunk) / size))
    if len(values) < 10:
        raise ProcessingError("Recording is too short to align")
    return values


def standardized(values: list[float]) -> list[float]:
    center = statistics.fmean(values)
    spread = math.sqrt(statistics.fmean((value - center) ** 2 for value in values))
    if spread < 1e-9:
        raise ProcessingError("Audio has no usable level changes for automatic alignment")
    return [(value - center) / spread for value in values]


def correlation_at(listener: list[float], runner: list[float], lag: int) -> float:
    # Compare listener[i] to runner[i + lag]. Positive lag means the same event
    # appears later in the runner recording.
    listener_start = max(0, -lag)
    runner_start = max(0, lag)
    length = min(len(listener) - listener_start, len(runner) - runner_start)
    if length < 25:
        return -1.0
    return sum(
        listener[listener_start + index] * runner[runner_start + index]
        for index in range(length)
    ) / length


def estimate_offset(listener_samples: array, runner_samples: array, max_offset_seconds: float = 3.0) -> tuple[float, float, float]:
    listener = standardized(envelope(listener_samples))
    runner = standardized(envelope(runner_samples))
    max_lag = max(1, round(max_offset_seconds * 1000 / WINDOW_MS))
    scored = [(correlation_at(listener, runner, lag), lag) for lag in range(-max_lag, max_lag + 1)]
    scored.sort(reverse=True)
    best_score, best_lag = scored[0]
    second_score = next((score for score, lag in scored[1:] if abs(lag - best_lag) > 3), scored[1][0])
    separation = max(0.0, best_score - second_score)
    confidence = max(0.0, min(1.0, (best_score + separation * 2 - 0.15) / 0.75))
    return best_lag * WINDOW_MS / 1000, confidence, best_score


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_capture_id(value: str | None) -> str:
    candidate = value or f"capture_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:6]}"
    safe = "".join(character for character in candidate if character.isalnum() or character in "-_")
    if not safe:
        raise ProcessingError("Capture ID must contain a letter or number")
    return safe


def process(args: argparse.Namespace) -> Path:
    ffmpeg = executable("ffmpeg")
    ffprobe = executable("ffprobe")
    listener = args.listener.resolve()
    runner = args.runner.resolve()
    if not listener.is_file() or not runner.is_file():
        raise ProcessingError("Both listener and runner recordings must exist")
    if listener == runner:
        raise ProcessingError("Listener and runner recordings must be different files")

    capture_id = safe_capture_id(args.capture_id)
    output = (args.output / capture_id).resolve()
    output.mkdir(parents=True, exist_ok=True)
    listener_duration = probe_duration(listener, ffprobe)
    runner_duration = probe_duration(runner, ffprobe)

    if args.offset_ms is None:
        print("Measuring audio alignment...")
        offset_seconds, confidence, correlation = estimate_offset(
            decode_mono(listener, ffmpeg), decode_mono(runner, ffmpeg), args.max_offset
        )
    else:
        offset_seconds = args.offset_ms / 1000
        confidence, correlation = 1.0, None

    listener_start = max(0.0, -offset_seconds)
    runner_start = max(0.0, offset_seconds)
    duration = min(listener_duration - listener_start, runner_duration - runner_start)
    if args.duration is not None:
        duration = min(duration, args.duration)
    if duration <= 0.25:
        raise ProcessingError("The aligned recordings do not have enough overlapping media")

    still_path = output / "listener.jpg"
    audio_path = output / "listener.m4a"
    replay_path = output / "replay.mp4"
    manifest_path = output / "capture.json"
    still_time = min(listener_duration - 0.001, listener_start + max(0.0, args.still_at))

    print(f"Runner offset: {offset_seconds * 1000:+.1f} ms (confidence {confidence:.0%})")
    print(f"Aligned duration: {duration:.3f} s")
    run([
        ffmpeg, "-y", "-v", "error", "-ss", f"{still_time:.6f}", "-i", str(listener),
        "-frames:v", "1", "-q:v", "2", str(still_path),
    ])
    run([
        ffmpeg, "-y", "-v", "error", "-ss", f"{listener_start:.6f}", "-i", str(listener),
        "-t", f"{duration:.6f}", "-vn", "-c:a", "aac", "-b:a", args.audio_bitrate,
        "-movflags", "+faststart", str(audio_path),
    ])
    run([
        ffmpeg, "-y", "-v", "error", "-ss", f"{runner_start:.6f}", "-i", str(runner),
        "-ss", f"{listener_start:.6f}", "-i", str(listener), "-t", f"{duration:.6f}",
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-preset", args.preset,
        "-crf", str(args.crf), "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", args.audio_bitrate,
        "-movflags", "+faststart", "-shortest", str(replay_path),
    ])

    for candidate in (still_path, audio_path, replay_path):
        if not candidate.is_file() or candidate.stat().st_size == 0:
            raise ProcessingError(f"FFmpeg did not create {candidate.name}")

    manifest: dict[str, Any] = {
        "schemaVersion": 1,
        "id": capture_id,
        "status": "needs_review",
        "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "durationSeconds": round(duration, 6),
        "evidence": {"stillPath": "listener.jpg", "audioPath": "listener.m4a"},
        "replay": {"videoPath": "replay.mp4"},
        "alignment": {
            "runnerOffsetMs": round(offset_seconds * 1000, 3),
            "listenerTrimStartSeconds": round(listener_start, 6),
            "runnerTrimStartSeconds": round(runner_start, 6),
            "confidence": round(confidence, 4),
            "correlation": round(correlation, 4) if correlation is not None else None,
            "method": "manual" if args.offset_ms is not None else "audio-envelope-correlation",
        },
        "sources": {"listener": str(listener), "runner": str(runner)},
        "sha256": {
            "listener.jpg": sha256(still_path),
            "listener.m4a": sha256(audio_path),
            "replay.mp4": sha256(replay_path),
        },
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    if args.register:
        sys.path.insert(0, str(ROOT))
        from studio_server import Store
        Store(args.database.resolve()).import_capture(manifest_path)
        print(f"Registered in {args.database}")

    if args.remove_raw:
        # Removal is last, after all outputs, hashes, and optional registration succeeded.
        listener.unlink()
        runner.unlink()
        print("Removed both raw recordings.")

    print(f"Capture ready for review: {manifest_path}")
    return manifest_path


def self_test() -> None:
    size = SAMPLE_RATE * 4
    listener = array("h", [0] * size)
    runner = array("h", [0] * size)
    for start, amplitude in ((6_000, 12_000), (13_000, 22_000), (21_000, 15_000)):
        for index in range(start, start + 320):
            listener[index] = amplitude
            runner[index + 640] = amplitude
    offset, confidence, score = estimate_offset(listener, runner, 1.0)
    assert abs(offset - 0.08) < 1e-9, offset
    assert confidence > 0.6, confidence
    assert score > 0.8, score
    assert safe_capture_id("test name!? ") == "testname"
    print("Capture processor self-test passed.")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--listener", type=Path, help="Stationary listener POV recording")
    result.add_argument("--runner", type=Path, help="Runner POV recording")
    result.add_argument("--capture-id")
    result.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    result.add_argument("--offset-ms", type=float, help="Manual runner offset; positive means runner event occurs later")
    result.add_argument("--max-offset", type=float, default=3.0)
    result.add_argument("--duration", type=float)
    result.add_argument("--still-at", type=float, default=0.0, help="Seconds after aligned listener start for the still")
    result.add_argument("--audio-bitrate", default="192k")
    result.add_argument("--crf", type=int, default=18)
    result.add_argument("--preset", default="medium")
    result.add_argument("--register", action="store_true", help="Import the result into the Studio database")
    result.add_argument("--database", type=Path, default=ROOT / "studio.db")
    result.add_argument("--remove-raw", action="store_true")
    result.add_argument("--self-test", action="store_true")
    return result


if __name__ == "__main__":
    arguments = parser().parse_args()
    if arguments.self_test:
        self_test()
    elif not arguments.listener or not arguments.runner:
        parser().error("--listener and --runner are required unless --self-test is used")
    else:
        try:
            process(arguments)
        except ProcessingError as error:
            print(f"ERROR: {error}", file=sys.stderr)
            raise SystemExit(1)
