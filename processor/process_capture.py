#!/usr/bin/env python3
"""Turn named runner/listener recordings into organized daily-set media."""

from __future__ import annotations

import argparse
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import zipfile
from array import array
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "daily sets"
SAMPLE_RATE = 8_000
WINDOW_MS = 10
CAPTURE_NAME = re.compile(
    r"^(?P<set_number>[1-9]\d*)-(?P<map>[a-z0-9]+(?:-[a-z0-9]+)*)-"
    r"(?P<slot>[123])-(?P<role>listener|runner)\.mp4$",
    re.IGNORECASE,
)


class ProcessingError(RuntimeError):
    pass


@dataclass(frozen=True)
class CaptureName:
    mapset: str
    slot: int
    role: str

    @property
    def key(self) -> tuple[str, int]:
        return self.mapset, self.slot


@dataclass(frozen=True)
class CaptureSource:
    path: Path
    archive_entry: str | None = None

    @property
    def name(self) -> str:
        return Path(self.archive_entry).name if self.archive_entry else self.path.name

    @property
    def display_name(self) -> str:
        return f"{self.path}!{self.archive_entry}" if self.archive_entry else str(self.path)

    def materialize(self, destination_root: Path) -> Path:
        if self.archive_entry is None:
            return self.path.resolve()
        destination = destination_root / self.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(self.path) as archive:
            with archive.open(self.archive_entry) as input_stream, destination.open("wb") as output_stream:
                shutil.copyfileobj(input_stream, output_stream)
        return destination


@dataclass(frozen=True)
class CapturePair:
    name: CaptureName
    listener: CaptureSource
    runner: CaptureSource


def parse_capture_name(path: Path) -> CaptureName:
    match = CAPTURE_NAME.fullmatch(path.name)
    if not match:
        raise ProcessingError(
            f"Invalid capture filename {path.name!r}; expected "
            "<mapset-number>-<map>-<slot>-<listener|runner>.mp4"
        )
    return CaptureName(
        mapset=f"{int(match.group('set_number'))}-{match.group('map').lower()}",
        slot=int(match.group("slot")),
        role=match.group("role").lower(),
    )


def pair_captures(paths: list[Path | CaptureSource]) -> list[CapturePair]:
    captures: dict[tuple[str, int], dict[str, CaptureSource]] = {}
    errors: list[str] = []
    for path in paths:
        source = path if isinstance(path, CaptureSource) else CaptureSource(path)
        try:
            name = parse_capture_name(Path(source.name))
        except ProcessingError as error:
            errors.append(str(error))
            continue
        roles = captures.setdefault(name.key, {})
        if name.role in roles:
            errors.append(
                f"Duplicate {name.role} for {name.mapset} slot {name.slot}: "
                f"{roles[name.role].display_name} and {source.display_name}"
            )
        else:
            roles[name.role] = source

    pairs: list[CapturePair] = []
    for (mapset, slot), roles in sorted(
        captures.items(), key=lambda item: (int(item[0][0].split("-", 1)[0]), item[0][0], item[0][1])
    ):
        missing = {"listener", "runner"} - roles.keys()
        if missing:
            errors.append(f"Missing {missing.pop()} for {mapset} slot {slot}")
            continue
        pairs.append(
            CapturePair(
                name=CaptureName(mapset, slot, "pair"),
                listener=roles["listener"],
                runner=roles["runner"],
            )
        )

    if errors:
        raise ProcessingError("Input pairing failed:\n- " + "\n- ".join(errors))
    if not pairs:
        raise ProcessingError("No named MP4 capture pairs were found")
    return pairs


def collect_inputs(inputs: list[Path]) -> tuple[list[CaptureSource], bool]:
    paths: list[CaptureSource] = []
    used_zip = False
    pending = [raw_input.expanduser().resolve() for raw_input in inputs]
    while pending:
        source = pending.pop(0)
        if source.is_dir():
            pending.extend(
                sorted(
                    path for path in source.rglob("*")
                    if path.is_file() and path.suffix.lower() in {".mp4", ".zip"}
                )
            )
        elif source.is_file() and source.suffix.lower() == ".mp4":
            paths.append(CaptureSource(source))
        elif source.is_file() and source.suffix.lower() == ".zip":
            used_zip = True
            with zipfile.ZipFile(source) as archive:
                for entry in archive.infolist():
                    filename = Path(entry.filename).name
                    if entry.is_dir() or not filename.lower().endswith(".mp4"):
                        continue
                    paths.append(CaptureSource(source, entry.filename))
        else:
            raise ProcessingError(f"Input must be an MP4, ZIP, or directory: {source}")
    return paths, used_zip


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


def process_pair(
    pair: CapturePair,
    output_root: Path,
    args: argparse.Namespace,
    ffmpeg: str,
    ffprobe: str,
) -> Path:
    print(f"\nProcessing {pair.name.mapset} slot {pair.name.slot}")
    with tempfile.TemporaryDirectory(prefix="r6-soundle-pair-") as temporary:
        listener = pair.listener.materialize(Path(temporary))
        runner = pair.runner.materialize(Path(temporary))
        if not listener.is_file() or not runner.is_file():
            raise ProcessingError("Both listener and runner recordings must exist")
        if listener == runner:
            raise ProcessingError("Listener and runner recordings must be different files")

        output = (output_root / pair.name.mapset / str(pair.name.slot)).resolve()
        output.mkdir(parents=True, exist_ok=True)
        listener_duration = probe_duration(listener, ffprobe)
        runner_duration = probe_duration(runner, ffprobe)

        if args.offset_ms is None:
            print("Measuring audio alignment...")
            offset_seconds, confidence, _correlation = estimate_offset(
                decode_mono(listener, ffmpeg), decode_mono(runner, ffmpeg), args.max_offset
            )
        else:
            offset_seconds = args.offset_ms / 1000
            confidence = 1.0

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
        still_time = min(listener_duration - 0.001, listener_start)

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

        if args.remove_raw:
            # Removal is last, after all three outputs succeeded.
            listener.unlink()
            runner.unlink()
            print("Removed both raw recordings.")

        print(f"Daily-set assets ready: {output}")
        return output


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
    parsed = parse_capture_name(Path("1-Clubhouse-3-listener.mp4"))
    assert parsed == CaptureName("1-clubhouse", 3, "listener"), parsed
    pairs = pair_captures([
        Path("2-oregon-1-runner.mp4"),
        Path("2-oregon-1-listener.mp4"),
    ])
    assert pairs[0].name.mapset == "2-oregon"
    assert pairs[0].name.slot == 1
    print("Capture processor self-test passed.")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--listener", type=Path, help="Stationary listener POV recording")
    result.add_argument("--runner", type=Path, help="Runner POV recording")
    result.add_argument(
        "--input",
        type=Path,
        nargs="+",
        help="Batch input MP4 files, ZIP archives, or directories (searched recursively)",
    )
    result.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    result.add_argument("--offset-ms", type=float, help="Manual runner offset; positive means runner event occurs later")
    result.add_argument("--max-offset", type=float, default=3.0)
    result.add_argument("--duration", type=float)
    result.add_argument("--audio-bitrate", default="192k")
    result.add_argument("--crf", type=int, default=18)
    result.add_argument("--preset", default="medium")
    result.add_argument("--remove-raw", action="store_true")
    result.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate and display the processing queue without running FFmpeg",
    )
    result.add_argument("--self-test", action="store_true")
    return result


if __name__ == "__main__":
    argument_parser = parser()
    arguments = argument_parser.parse_args()
    if arguments.self_test:
        self_test()
    else:
        try:
            explicit_pair = arguments.listener is not None or arguments.runner is not None
            if arguments.input and explicit_pair:
                argument_parser.error("Use either --input or --listener/--runner, not both")
            if explicit_pair and (arguments.listener is None or arguments.runner is None):
                argument_parser.error("--listener and --runner must be supplied together")
            if arguments.validate_only and arguments.remove_raw:
                argument_parser.error("--validate-only and --remove-raw cannot be used together")

            if arguments.input:
                paths, used_zip = collect_inputs(arguments.input)
                pairs = pair_captures(paths)
            elif explicit_pair:
                used_zip = False
                pairs = pair_captures([arguments.listener, arguments.runner])
            else:
                default_input = ROOT / "videos"
                print(f"No input specified; scanning {default_input}")
                paths, used_zip = collect_inputs([default_input])
                pairs = pair_captures(paths)

            if arguments.remove_raw and used_zip:
                raise ProcessingError("--remove-raw cannot remove recordings stored inside ZIP archives")

            output_root = arguments.output.expanduser().resolve()
            print(f"Validated {len(pairs)} runner/listener pair(s):")
            for pair in pairs:
                relative_output = Path(pair.name.mapset) / str(pair.name.slot)
                print(
                    f"  {relative_output} <- "
                    f"{pair.listener.name} + {pair.runner.name}"
                )

            if arguments.validate_only:
                print(f"\nQueue is valid. Outputs will be written under {output_root}")
            else:
                ffmpeg = executable("ffmpeg")
                ffprobe = executable("ffprobe")
                for pair in pairs:
                    process_pair(pair, output_root, arguments, ffmpeg, ffprobe)
                print(f"\nProcessed {len(pairs)} pair(s) into {output_root}")
        except ProcessingError as error:
            print(f"ERROR: {error}", file=sys.stderr)
            raise SystemExit(1)
