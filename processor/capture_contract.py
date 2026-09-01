"""Capture-v1 schema and semantic validation shared by processing and indexing."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schemas" / "capture.schema.json"
CAPTURE_SCHEMA_VERSION = 1
PROCESSING_VERSION = 1
PROCESSOR_VERSION = "r6-soundle-capture/1"
MEDIA_BY_KIND = {
    "evidence_still": ("listener.jpg", "image/jpeg"),
    "evidence_audio": ("listener.m4a", "audio/mp4"),
    "replay_video": ("replay.mp4", "video/mp4"),
}
SENSITIVE_KEY = re.compile(
    r"(?:secret|password|passwd|token|credential|authorization|cookie|api[_-]?key)",
    re.IGNORECASE,
)


class CaptureValidationError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def hash_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def file_fingerprint(path: Path, *, role: str, name: str) -> dict[str, Any]:
    size = path.stat().st_size
    if size <= 0:
        raise CaptureValidationError(f"Source {name!r} is empty")
    return {"role": role, "name": name, "byteSize": size, "sha256": hash_file(path)}


def load_capture_schema() -> dict[str, Any]:
    try:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CaptureValidationError(f"Cannot load capture schema: {error}") from error
    if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise CaptureValidationError("capture.schema.json must declare JSON Schema draft 2020-12")
    return schema


def _is_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    raise CaptureValidationError(f"Unsupported schema type {expected!r}")


def _validate_schema_value(value: Any, schema: dict[str, Any], location: str) -> None:
    if "const" in schema and value != schema["const"]:
        raise CaptureValidationError(f"{location} must equal {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise CaptureValidationError(f"{location} is not an allowed value")
    expected = schema.get("type")
    if expected and not _is_type(value, expected):
        raise CaptureValidationError(f"{location} must be {expected}")
    if isinstance(value, dict):
        required = schema.get("required", [])
        missing = [key for key in required if key not in value]
        if missing:
            raise CaptureValidationError(f"{location} is missing {', '.join(missing)}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extras = sorted(set(value) - set(properties))
            if extras:
                raise CaptureValidationError(f"{location} has unknown field(s): {', '.join(extras)}")
        for key, child in value.items():
            if key in properties:
                _validate_schema_value(child, properties[key], f"{location}.{key}")
    elif isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise CaptureValidationError(f"{location} has too few items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise CaptureValidationError(f"{location} has too many items")
        item_schema = schema.get("items")
        if item_schema:
            for index, child in enumerate(value):
                _validate_schema_value(child, item_schema, f"{location}[{index}]")
    elif isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise CaptureValidationError(f"{location} is too short")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise CaptureValidationError(f"{location} is too long")
        if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            raise CaptureValidationError(f"{location} has an invalid format")
        if schema.get("format") == "date-time":
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as error:
                raise CaptureValidationError(f"{location} must be an ISO date-time") from error
            if parsed.tzinfo is None:
                raise CaptureValidationError(f"{location} must include a timezone")
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise CaptureValidationError(f"{location} is below its minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise CaptureValidationError(f"{location} is above its maximum")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            raise CaptureValidationError(f"{location} must be greater than its minimum")


def _portable_path(value: str, location: str) -> PurePosixPath:
    if "\\" in value or re.match(r"^[A-Za-z]:", value):
        raise CaptureValidationError(f"{location} must use a relative portable path")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise CaptureValidationError(f"{location} must be a confined relative filename")
    return path


def resolve_capture_file(capture_dir: Path, relative: str, location: str) -> Path:
    portable = _portable_path(relative, location)
    if capture_dir.is_symlink():
        raise CaptureValidationError("capture directory may not be a symlink")
    root = capture_dir.resolve(strict=True)
    candidate = root.joinpath(*portable.parts)
    current = root
    for part in portable.parts:
        current = current / part
        if current.is_symlink():
            raise CaptureValidationError(f"{location} may not traverse a symlink")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as error:
        raise CaptureValidationError(f"{location} escapes or is missing from the capture directory") from error
    if not resolved.is_file():
        raise CaptureValidationError(f"{location} must identify a regular file")
    return resolved


def _reject_sensitive_keys(value: Any, location: str = "capture") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if SENSITIVE_KEY.search(key):
                raise CaptureValidationError(f"{location} contains prohibited credential field {key!r}")
            _reject_sensitive_keys(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_sensitive_keys(child, f"{location}[{index}]")
    elif isinstance(value, str) and re.search(r"://[^/@\s:]+:[^/@\s]+@", value):
        raise CaptureValidationError(f"{location} contains credentials in a URL")


def validate_capture(manifest: dict[str, Any], capture_dir: Path | None = None) -> None:
    """Validate the schema shape, cross-field rules, and optionally bytes on disk."""
    if not isinstance(manifest, dict):
        raise CaptureValidationError("capture must be a JSON object")
    _validate_schema_value(manifest, load_capture_schema(), "capture")
    _reject_sensitive_keys(manifest)

    source = manifest["source"]
    expected_map_set = f"{source['setNumber']}-{source['mapSlug']}"
    if source["mapSet"] != expected_map_set:
        raise CaptureValidationError("source.mapSet does not match setNumber and mapSlug")
    if manifest["id"] != f"{expected_map_set}/{source['slot']}":
        raise CaptureValidationError("id does not match source mapSet and slot")

    path_fields = {
        "evidence_still": manifest["evidence"]["stillPath"],
        "evidence_audio": manifest["evidence"]["audioPath"],
        "replay_video": manifest["replay"]["videoPath"],
    }
    for location, value in (
        ("evidence.stillPath", path_fields["evidence_still"]),
        ("evidence.audioPath", path_fields["evidence_audio"]),
        ("replay.videoPath", path_fields["replay_video"]),
    ):
        _portable_path(value, location)
    if len(set(path_fields.values())) != 3:
        raise CaptureValidationError("capture media paths must be unique")

    by_kind: dict[str, dict[str, Any]] = {}
    for item in manifest["media"]:
        if item["kind"] in by_kind:
            raise CaptureValidationError(f"duplicate media kind {item['kind']}")
        by_kind[item["kind"]] = item
    if set(by_kind) != set(MEDIA_BY_KIND):
        raise CaptureValidationError("media must contain exactly the three canonical kinds")
    for kind, (canonical_name, mime_type) in MEDIA_BY_KIND.items():
        item = by_kind[kind]
        if item["path"] != path_fields[kind] or item["path"] != canonical_name:
            raise CaptureValidationError(f"{kind} must use canonical path {canonical_name}")
        if item["mimeType"] != mime_type:
            raise CaptureValidationError(f"{kind} has an invalid MIME type")
        if kind != "evidence_still" and item.get("durationSeconds", 0) <= 0:
            raise CaptureValidationError(f"{kind} must have a positive duration")
        if kind in {"evidence_still", "replay_video"}:
            if item.get("width", 0) <= 0 or item.get("height", 0) <= 0:
                raise CaptureValidationError(f"{kind} must include positive dimensions")

    fingerprints = manifest["processing"]["sourceFingerprints"]
    roles = [item["role"] for item in fingerprints]
    if len(roles) != len(set(roles)):
        raise CaptureValidationError("source fingerprint roles must be unique")
    if fingerprints and set(roles) != {"listener", "runner"}:
        raise CaptureValidationError("source fingerprints must include listener and runner")
    review = manifest["review"]
    if review["status"] == "approved" and "reviewedAt" not in review:
        raise CaptureValidationError("approved captures require review.reviewedAt")
    if review["status"] == "needs_review" and "reviewedAt" in review:
        raise CaptureValidationError("needs-review captures cannot have review.reviewedAt")

    if capture_dir is None:
        return
    for kind, item in by_kind.items():
        path = resolve_capture_file(capture_dir, item["path"], f"media[{kind}].path")
        stat = path.stat()
        if stat.st_size != item["byteSize"]:
            raise CaptureValidationError(f"{item['path']} size does not match the manifest")
        if hash_file(path) != item["sha256"]:
            raise CaptureValidationError(f"{item['path']} hash does not match the manifest")


def read_and_validate_capture(manifest_path: Path, *, verify_files: bool = True) -> dict[str, Any]:
    if manifest_path.is_symlink():
        raise CaptureValidationError("capture.json may not be a symlink")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CaptureValidationError(f"Cannot read {manifest_path}: {error}") from error
    validate_capture(manifest, manifest_path.parent if verify_files else None)
    return manifest
