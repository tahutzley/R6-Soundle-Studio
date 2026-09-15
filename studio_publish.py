"""Resumable Phase 6 publisher for the owner-only Studio application."""
from __future__ import annotations

import copy
import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from threading import Lock, Thread
from typing import Any, Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


MEDIA_KIND_MAP = {
    "evidence_still": ("listener_still", "evidence", "stillPath"),
    "evidence_audio": ("listener_audio", "evidence", "audioPath"),
    "replay_video": ("replay_video", "replay", "videoPath"),
}


class PublishClientError(RuntimeError):
    pass


class PublishInterrupted(RuntimeError):
    pass


class PublisherClientProtocol(Protocol):
    def capabilities(self) -> dict[str, Any]: ...
    def prepare(self, payload: Mapping[str, Any], idempotency_key: str) -> dict[str, Any]: ...
    def status(self, attempt_id: str) -> dict[str, Any]: ...
    def upload(self, authorization: Mapping[str, Any], path: Path) -> None: ...
    def verify(self, attempt_id: str) -> dict[str, Any]: ...
    def finalize(self, attempt_id: str) -> dict[str, Any]: ...
    def make_unavailable(
        self, release_date: str, expected_revision: int, reason: str, idempotency_key: str
    ) -> dict[str, Any]: ...
    def list_challenges(self) -> dict[str, Any]: ...
    def remove_challenge(
        self, challenge_id: str, reason: str, idempotency_key: str
    ) -> dict[str, Any]: ...


class PublisherClient:
    def __init__(self, base_url: str, bearer_token: str | None = None, timeout_seconds: int = 60) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("Publisher URL must be absolute HTTP(S)")
        parsed = urlsplit(base_url)
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Non-loopback publisher URLs must use HTTPS")
        if not bearer_token:
            raise ValueError("R6_STUDIO_PUBLISHER_TOKEN is required for remote publishing")
        if not 20 <= len(bearer_token) <= 256:
            raise ValueError("R6_STUDIO_PUBLISHER_TOKEN must be between 20 and 256 characters")
        self.base_url = base_url.rstrip("/") + "/"
        self.bearer_token = bearer_token
        self.timeout_seconds = timeout_seconds

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        request_headers = {"Accept": "application/json", **dict(headers or {})}
        if self.bearer_token:
            request_headers["Authorization"] = f"Bearer {self.bearer_token}"
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = Request(urljoin(self.base_url, path.lstrip("/")), data=data, headers=request_headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                value = json.loads(response.read() or b"{}")
        except HTTPError as error:
            try:
                payload = json.loads(error.read() or b"{}")
                detail = payload.get("error", {})
                message = detail.get("message") or detail.get("code")
            except Exception:
                message = None
            raise PublishClientError(message or f"Publisher returned HTTP {error.code}") from error
        except (URLError, TimeoutError, json.JSONDecodeError) as error:
            raise PublishClientError(f"Publisher request failed: {error}") from error
        if not isinstance(value, dict):
            raise PublishClientError("Publisher response was not a JSON object")
        return value

    def capabilities(self) -> dict[str, Any]:
        return self._request("GET", "/admin/v1/capabilities")

    def prepare(self, payload: Mapping[str, Any], idempotency_key: str) -> dict[str, Any]:
        return self._request(
            "POST", "/admin/v1/publish-attempts", body=payload, headers={"Idempotency-Key": idempotency_key}
        )

    def status(self, attempt_id: str) -> dict[str, Any]:
        return self._request("GET", f"/admin/v1/publish-attempts/{attempt_id}")

    def upload(self, authorization: Mapping[str, Any], path: Path) -> None:
        url = urljoin(self.base_url, str(authorization["url"]))
        for attempt, retry_delay in enumerate((0.5, 1.0, None), start=1):
            try:
                with path.open("rb") as body:
                    request = Request(
                        url,
                        data=body,  # type: ignore[arg-type]
                        headers={str(key): str(value) for key, value in dict(authorization.get("headers") or {}).items()},
                        method=str(authorization.get("method") or "PUT"),
                    )
                    with urlopen(request, timeout=self.timeout_seconds) as response:
                        if response.status >= 300:
                            raise PublishClientError(f"Upload returned HTTP {response.status}")
                return
            except HTTPError as error:
                raise PublishClientError(f"Media upload failed: {error}") from error
            except (URLError, TimeoutError) as error:
                if retry_delay is None:
                    raise PublishClientError(
                        f"Media upload failed after {attempt} attempts: {error}"
                    ) from error
                time.sleep(retry_delay)

    def verify(self, attempt_id: str) -> dict[str, Any]:
        return self._request("POST", f"/admin/v1/publish-attempts/{attempt_id}/verify")

    def finalize(self, attempt_id: str) -> dict[str, Any]:
        return self._request("POST", f"/admin/v1/publish-attempts/{attempt_id}/finalize")

    def make_unavailable(
        self, release_date: str, expected_revision: int, reason: str, idempotency_key: str
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/admin/v1/releases/{release_date}/unavailable",
            body={"expectedRevision": expected_revision, "reason": reason},
            headers={"Idempotency-Key": idempotency_key},
        )

    def list_challenges(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/challenges")

    def remove_challenge(
        self, challenge_id: str, reason: str, idempotency_key: str
    ) -> dict[str, Any]:
        encoded_id = quote(challenge_id, safe="")
        return self._request(
            "POST",
            f"/admin/v1/challenges/{encoded_id}/unavailable",
            body={"reason": reason},
            headers={"Idempotency-Key": idempotency_key},
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _public_blueprint_url(value: str) -> str:
    if value.startswith("/game-assets/"):
        return "/assets/" + value[len("/game-assets/"):]
    return value


@dataclass(frozen=True)
class PublishBundle:
    payload: dict[str, Any]
    paths: dict[tuple[int, str], Path]


class StudioPublisher:
    COMPLETED_REMOTE_STATES = frozenset({"released", "scheduled", "emergency_unavailable"})

    def __init__(
        self,
        store: Any,
        catalog: Mapping[str, Any],
        scoring_version: int,
        client: PublisherClientProtocol,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.catalog = catalog
        self.scoring_version = scoring_version
        self.client = client
        self.now = now or (lambda: datetime.now(ZoneInfo("America/New_York")))

    def _bundle(
        self,
        release_date: str | None,
        set_id: str,
        *,
        expected_revision: int | None = None,
        reason: str | None = None,
    ) -> PublishBundle:
        if release_date is not None:
            date.fromisoformat(release_date)
        if (expected_revision is None) != (reason is None):
            raise ValueError("A correction requires both expected_revision and reason")
        if expected_revision is not None and expected_revision <= 0:
            raise ValueError("Correction expected_revision must be positive")
        if reason is not None and not reason.strip():
            raise ValueError("Correction reason must not be empty")
        item = self.store.get_set(set_id)
        if not item:
            raise KeyError("Set not found")
        if item.get("kind") != "daily":
            raise ValueError("How-to examples cannot be published as challenges")
        if item["status"] != "approved":
            raise ValueError("Only an approved set can be published")
        if item["mapAssetVersion"] != self.catalog["assetVersion"]:
            raise ValueError("The approved set uses stale map assets; review and approve it again")
        map_item = next((value for value in self.catalog["maps"] if value["slug"] == item["mapSlug"]), None)
        if map_item is None:
            raise ValueError("The set map is not available in the current catalog")
        captures = {capture["id"]: capture for capture in self.store.list_captures()}
        errors = __import__("studio_server").validate_set(item, captures)
        if errors:
            raise ValueError("; ".join(errors))

        paths: dict[tuple[int, str], Path] = {}
        media: list[dict[str, Any]] = []
        rounds: list[dict[str, Any]] = []
        for position, source in enumerate(item["rounds"], start=1):
            capture = captures[source["captureId"]]
            manifest_media = {entry["kind"]: entry for entry in capture["media"]}
            for capture_kind, (publish_kind, section, field) in MEDIA_KIND_MAP.items():
                path = Path(capture[section][field]).resolve()
                descriptor = manifest_media[capture_kind]
                if not path.is_file() or path.stat().st_size != int(descriptor["byteSize"]):
                    raise ValueError(f"Round {position} {publish_kind} changed after import")
                if _sha256(path) != descriptor["sha256"]:
                    raise ValueError(f"Round {position} {publish_kind} checksum changed after import")
                paths[(position, publish_kind)] = path
                media.append(
                    {
                        "position": position,
                        "kind": publish_kind,
                        "sha256": descriptor["sha256"],
                        "byteSize": descriptor["byteSize"],
                        "mimeType": descriptor["mimeType"],
                    }
                )
            rounds.append(
                {
                    "position": position,
                    "operatorId": source["operatorId"],
                    "evidenceImageUrl": "pending://listener-still",
                    "videoUrl": "pending://listener-audio",
                    "replayVideoUrl": "pending://replay-video",
                    "listenerPos": source["listenerPos"],
                    "operatorStartPos": source["operatorStartPos"],
                    "targetPos": source["targetPos"],
                    "alternateTargetFloorKey": source.get("alternateTargetFloorKey"),
                }
            )
        floors = [
            {
                "key": floor["key"],
                "label": floor["label"],
                "blueprintUrl": _public_blueprint_url(floor["imageUrl"]),
                "pixelsPerMeter": floor.get("pixelsPerMeter", 1),
                "coordinateSize": floor.get("coordinateSize", 2048),
                "coordinateFrame": floor["coordinateFrame"],
            }
            for floor in map_item["floors"]
        ]
        floor_keys = {floor["key"] for floor in floors}
        for position, round_item in enumerate(rounds, start=1):
            alternate_floor = round_item.get("alternateTargetFloorKey")
            if alternate_floor is not None and alternate_floor not in floor_keys:
                raise ValueError(
                    f"Round {position} alternate runner-end floor is not available on this map"
                )
        puzzle = {
            "mapName": item.get("mapName") or map_item["name"],
            "mapSlug": item["mapSlug"],
            "floorKey": floors[0]["key"],
            "floors": floors,
            "rounds": rounds,
        }
        payload = {
            "authoringSetId": item["id"],
            "studioSetVersion": item["version"],
            "schemaVersion": 1 if release_date is not None else 2,
            "scoringVersion": self.scoring_version,
            "mapAssetVersion": item["mapAssetVersion"],
            "snapshot": {"puzzle": puzzle},
            "media": media,
        }
        if release_date is None:
            payload["publishMode"] = "immediate"
        else:
            payload["releaseDate"] = release_date
            puzzle["date"] = release_date
        if expected_revision is not None:
            payload["expectedRevision"] = expected_revision
            payload["reason"] = reason.strip()  # type: ignore[union-attr]
        return PublishBundle(payload, paths)

    def _local_attempt(self, release_date: str | None, set_id: str, bundle: PublishBundle) -> dict[str, Any]:
        with self.store.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM publish_attempts
                   WHERE set_id=? AND set_version=?
                     AND (? IS NULL OR release_date=?)
                   ORDER BY created_at DESC""",
                (set_id, bundle.payload["studioSetVersion"], release_date, release_date),
            ).fetchall()
            for row in rows:
                prior = dict(row)
                persisted = json.loads(prior.get("request_json") or "{}")
                if persisted == bundle.payload:
                    return prior
            now = __import__("studio_server").iso_utc()
            attempt_id = f"publish_{uuid.uuid4().hex[:16]}"
            idempotency_key = f"studio-{uuid.uuid4()}"
            connection.execute(
                """INSERT INTO publish_attempts
                   (id, release_date, set_id, set_version, idempotency_key, state,
                    retry_count, request_json, objects_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'approved', 0, ?, '[]', ?, ?)""",
                (
                    attempt_id,
                    release_date or "immediate",
                    set_id,
                    bundle.payload["studioSetVersion"],
                    idempotency_key,
                    json.dumps(bundle.payload, separators=(",", ":"), sort_keys=True),
                    now,
                    now,
                ),
            )
            row = connection.execute("SELECT * FROM publish_attempts WHERE id=?", (attempt_id,)).fetchone()
            return dict(row)

    def _update_attempt(self, attempt_id: str, **values: Any) -> dict[str, Any]:
        if not values:
            return self.get_attempt(attempt_id)
        values["updated_at"] = __import__("studio_server").iso_utc()
        assignments = ", ".join(f"{key}=?" for key in values)
        encoded = [
            json.dumps(
                [
                    {key: field for key, field in item.items() if key != "upload"}
                    for item in value
                ]
                if key == "objects_json" and isinstance(value, list)
                else value,
                separators=(",", ":"),
                sort_keys=True,
            )
            if key in {"objects_json", "last_response_json"} and not isinstance(value, str)
            else value
            for key, value in values.items()
        ]
        with self.store.connect() as connection:
            connection.execute(
                f"UPDATE publish_attempts SET {assignments} WHERE id=?",
                (*encoded, attempt_id),
            )
        return self.get_attempt(attempt_id)

    def get_attempt(self, attempt_id: str) -> dict[str, Any]:
        with self.store.connect() as connection:
            row = connection.execute("SELECT * FROM publish_attempts WHERE id=?", (attempt_id,)).fetchone()
        if not row:
            raise KeyError("Publish attempt not found")
        return self._decode_attempt(dict(row))

    @staticmethod
    def _decode_attempt(row: dict[str, Any]) -> dict[str, Any]:
        for key in ("request_json", "objects_json", "last_response_json"):
            if row.get(key):
                row[key.removesuffix("_json")] = json.loads(row[key])
        return row

    def list_attempts(self) -> list[dict[str, Any]]:
        with self.store.connect() as connection:
            rows = connection.execute("SELECT * FROM publish_attempts ORDER BY updated_at DESC").fetchall()
        return [self._decode_attempt(dict(row)) for row in rows]

    def publishable_sets(self) -> list[dict[str, Any]]:
        """Approved daily sets whose current version is not live in production.

        Queue order decides production challenge numbering, because production
        allocates each immediate challenge the next internal slot and numbers a
        map's challenges by release date. Sorting by name keeps "Set 1" ahead of
        "Set 2" on the same map.
        """
        completed = {
            (attempt["set_id"], int(attempt["set_version"]))
            for attempt in self.list_attempts()
            if attempt.get("remote_release_version_id")
            and (attempt.get("remote_state") or attempt.get("state")) in self.COMPLETED_REMOTE_STATES
        }
        available = [
            item
            for item in self.store.list_sets()
            if (item.get("kind") or "daily") == "daily"
            and item["status"] == "approved"
            and (item["id"], int(item["version"])) not in completed
        ]
        return sorted(available, key=lambda item: (item["name"], item["id"]))

    def list_remote_challenges(self) -> dict[str, Any]:
        return self.client.list_challenges()

    @staticmethod
    def _challenge_id(attempt: Mapping[str, Any]) -> str | None:
        response = attempt.get("last_response")
        if not isinstance(response, Mapping):
            return None
        remote = response.get("publishAttempt")
        if not isinstance(remote, Mapping):
            remote = response
        value = remote.get("challengeId")
        return str(value) if value else None

    def remove_challenge(self, challenge_id: str, reason: str) -> dict[str, Any]:
        challenge_id = challenge_id.strip()
        reason = reason.strip()
        if not challenge_id:
            raise ValueError("A challenge ID is required")
        if not reason:
            raise ValueError("A reason is required to remove a production challenge")
        response = self.client.remove_challenge(
            challenge_id,
            reason,
            f"studio-remove-{uuid.uuid4()}",
        )
        for attempt in self.list_attempts():
            if self._challenge_id(attempt) != challenge_id:
                continue
            self._update_attempt(
                attempt["id"],
                remote_state=str(response.get("state") or "emergency_unavailable"),
                transition_reason=reason,
                transitioned_at=__import__("studio_server").iso_utc(),
                last_response_json={"challengeId": challenge_id, "release": response},
            )
        return response

    @staticmethod
    def _release_revision(attempt: Mapping[str, Any]) -> int:
        response = attempt.get("last_response")
        if isinstance(response, Mapping):
            release = response.get("release")
            if isinstance(release, Mapping) and release.get("revision") is not None:
                return int(release["revision"])
            if response.get("revision") is not None:
                return int(response["revision"])
        version_id = str(attempt.get("remote_release_version_id") or "")
        marker = version_id.rsplit(":r", 1)
        if len(marker) == 2 and marker[1].isdigit():
            return int(marker[1])
        raise ValueError("The remote release revision is unavailable; refresh or inspect it before changing it")

    def latest_release_attempt(self, release_date: str) -> dict[str, Any] | None:
        for attempt in self.list_attempts():
            if attempt["release_date"] != release_date or not attempt.get("remote_release_version_id"):
                continue
            remote_state = attempt.get("remote_state") or attempt.get("state")
            if remote_state in {"scheduled", "released", "emergency_unavailable"}:
                return attempt
        return None

    def stop_scheduled(self, release_date: str, reason: str) -> dict[str, Any]:
        parsed_date = date.fromisoformat(release_date)
        reason = reason.strip()
        if not reason:
            raise ValueError("A reason is required to stop a scheduled release")
        if parsed_date <= self.now().astimezone(ZoneInfo("America/New_York")).date():
            raise ValueError("Only a future release can be stopped from Studio")
        attempt = self.latest_release_attempt(release_date)
        if not attempt:
            raise KeyError("Scheduled remote release not found")
        remote_state = attempt.get("remote_state") or attempt.get("state")
        if remote_state != "scheduled":
            raise ValueError("Only a currently scheduled release can be stopped")
        response = self.client.make_unavailable(
            release_date,
            self._release_revision(attempt),
            reason,
            f"studio-stop-{uuid.uuid4()}",
        )
        return self._update_attempt(
            attempt["id"],
            remote_state=str(response.get("state") or "emergency_unavailable"),
            transition_reason=reason,
            transitioned_at=__import__("studio_server").iso_utc(),
            last_response_json=response,
        )

    def _complete_local(self, attempt: dict[str, Any], remote: Mapping[str, Any], bundle: PublishBundle) -> dict[str, Any]:
        remote_attempt = remote.get("publishAttempt") if isinstance(remote.get("publishAttempt"), Mapping) else remote
        release = remote.get("release") if isinstance(remote.get("release"), Mapping) else {}
        remote_release_id = remote_attempt.get("remoteReleaseId") or release.get("releaseId")
        remote_version_id = remote_attempt.get("remoteReleaseVersionId") or release.get("releaseVersionId")
        remote_state = str(release.get("state") or remote_attempt.get("state") or "scheduled")
        now = __import__("studio_server").iso_utc()
        release_at = str(
            release.get("releaseAt")
            or __import__("studio_server").release_at_for_date(attempt["release_date"])
        )
        snapshot = copy.deepcopy(bundle.payload["snapshot"]["puzzle"])
        snapshot.setdefault("date", attempt["release_date"])
        with self.store.connect() as connection:
            if bundle.payload.get("expectedRevision") is not None:
                connection.execute(
                    """UPDATE publish_attempts SET remote_state='superseded', transition_reason=?,
                       transitioned_at=?, updated_at=?
                       WHERE release_date=? AND id<>? AND remote_release_version_id IS NOT NULL
                         AND COALESCE(remote_state, state) IN ('scheduled','released','emergency_unavailable')""",
                    (bundle.payload.get("reason"), now, now, attempt["release_date"], attempt["id"]),
                )
            connection.execute(
                """INSERT INTO schedule_entries
                   (release_date, release_at, set_id, set_version, created_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(release_date) DO UPDATE SET
                     release_at=excluded.release_at, set_id=excluded.set_id,
                     set_version=excluded.set_version, created_at=excluded.created_at""",
                (attempt["release_date"], release_at, attempt["set_id"], attempt["set_version"], now),
            )
            connection.execute(
                """INSERT INTO published_releases
                   (release_date, release_at, set_id, set_version, snapshot_json, published_at,
                    remote_release_id, remote_release_version_id, publisher_idempotency_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(release_date) DO UPDATE SET
                     release_at=excluded.release_at, set_id=excluded.set_id,
                     set_version=excluded.set_version, snapshot_json=excluded.snapshot_json,
                     published_at=excluded.published_at, remote_release_id=excluded.remote_release_id,
                     remote_release_version_id=excluded.remote_release_version_id,
                     publisher_idempotency_key=excluded.publisher_idempotency_key""",
                (
                    attempt["release_date"],
                    release_at,
                    attempt["set_id"],
                    attempt["set_version"],
                    json.dumps(snapshot, separators=(",", ":")),
                    now,
                    remote_release_id,
                    remote_version_id,
                    attempt["idempotency_key"],
                ),
            )
            connection.execute(
                """UPDATE publish_attempts SET state='scheduled', remote_state=?, remote_release_id=?,
                   remote_release_version_id=?, last_response_json=?, error_summary=NULL,
                   completed_at=?, updated_at=? WHERE id=?""",
                (
                    remote_state,
                    remote_release_id,
                    remote_version_id,
                    json.dumps(remote, separators=(",", ":"), sort_keys=True),
                    now,
                    now,
                    attempt["id"],
                ),
            )
        return self.get_attempt(attempt["id"])

    def publish(
        self,
        release_date: str | None,
        set_id: str,
        *,
        interrupt_after: int | None = None,
        expected_revision: int | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        bundle = self._bundle(
            release_date,
            set_id,
            expected_revision=expected_revision,
            reason=reason,
        )
        attempt = self._local_attempt(release_date, set_id, bundle)
        decoded = self._decode_attempt(dict(attempt))
        if decoded["state"] == "scheduled":
            return decoded
        try:
            capabilities = self.client.capabilities()
            supported = capabilities.get("publishSchemaVersions", {})
            schema_version = int(bundle.payload["schemaVersion"])
            if int(supported.get("min", 0)) > schema_version or int(supported.get("max", 0)) < schema_version:
                raise PublishClientError(f"Publisher does not support Studio publish schema v{schema_version}")
            if schema_version == 2 and capabilities.get("immediateChallenges") is not True:
                raise PublishClientError("Publisher does not support immediate challenges")
            if decoded.get("remote_publish_attempt_id"):
                remote = self.client.status(decoded["remote_publish_attempt_id"])
            else:
                remote = self.client.prepare(bundle.payload, decoded["idempotency_key"])
                decoded = self._update_attempt(
                    decoded["id"],
                    release_date=str(remote["releaseDate"]),
                    state="uploading",
                    remote_publish_attempt_id=remote["publishAttemptId"],
                    objects_json=remote["objects"],
                )
            if remote.get("state") == "scheduled":
                return self._complete_local(decoded, remote, bundle)
            local_objects = [dict(value) for value in remote["objects"]]
            self._update_attempt(decoded["id"], state="uploading", objects_json=local_objects)
            uploaded = 0
            for item in remote["objects"]:
                if item["status"] == "verified":
                    continue
                authorization = item.get("upload")
                if not authorization:
                    raise PublishClientError("Publisher omitted upload authorization for pending media")
                self.client.upload(authorization, bundle.paths[(int(item["position"]), str(item["kind"]))])
                uploaded += 1
                next(value for value in local_objects if value["id"] == item["id"])["status"] = "uploaded"
                self._update_attempt(decoded["id"], objects_json=local_objects)
                if interrupt_after is not None and uploaded == interrupt_after:
                    raise PublishInterrupted(f"Interrupted after object {uploaded}")
            verified = self.client.verify(remote["publishAttemptId"])
            self._update_attempt(decoded["id"], objects_json=verified["objects"])
            if not all(item["status"] == "verified" for item in verified["objects"]):
                raise PublishClientError("Publisher could not verify all nine objects")
            finalized = self.client.finalize(remote["publishAttemptId"])
            return self._complete_local(decoded, finalized, bundle)
        except PublishInterrupted:
            raise
        except Exception as error:
            with self.store.connect() as connection:
                connection.execute(
                    """UPDATE publish_attempts SET state='upload_failed', retry_count=retry_count+1,
                       error_summary=?, updated_at=? WHERE id=?""",
                    (str(error)[:500], __import__("studio_server").iso_utc(), decoded["id"]),
                )
            raise

    def publish_immediately(
        self,
        set_id: str,
        *,
        interrupt_after: int | None = None,
    ) -> dict[str, Any]:
        return self.publish(None, set_id, interrupt_after=interrupt_after)


class PublishQueue:
    """Serial background queue for uploading approved sets to production.

    Publishes run one at a time on a single worker thread. That is a
    requirement, not a simplification: production assigns every immediate
    challenge the next free internal slot, so concurrent publishes would race
    for the same date. A set that fails is recorded and the queue moves on to
    the next one.
    """

    ACTIVE_STATES = frozenset({"queued", "running"})

    def __init__(self, publisher: StudioPublisher) -> None:
        self.publisher = publisher
        self._lock = Lock()
        self._jobs: list[dict[str, Any]] = []
        self._worker: Thread | None = None

    @property
    def running(self) -> bool:
        with self._lock:
            return any(job["state"] == "running" for job in self._jobs)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            jobs = [dict(job) for job in self._jobs]
        return {
            "running": any(job["state"] == "running" for job in jobs),
            "pending": sum(1 for job in jobs if job["state"] in self.ACTIVE_STATES),
            "jobs": jobs,
        }

    def enqueue(self, set_ids: list[str] | None = None) -> dict[str, Any]:
        if set_ids is None:
            candidates = self.publisher.publishable_sets()
        else:
            publishable = {item["id"]: item for item in self.publisher.publishable_sets()}
            candidates = []
            for set_id in set_ids:
                item = publishable.get(set_id)
                if item is None:
                    raise ValueError(f"{set_id} is not an approved set awaiting production")
                candidates.append(item)
        now = __import__("studio_server").iso_utc()
        with self._lock:
            active = {
                job["setId"] for job in self._jobs if job["state"] in self.ACTIVE_STATES
            }
            added = 0
            for item in candidates:
                if item["id"] in active:
                    continue
                active.add(item["id"])
                added += 1
                self._jobs.append(
                    {
                        "setId": item["id"],
                        "setVersion": int(item["version"]),
                        "name": item["name"],
                        "state": "queued",
                        "error": None,
                        "challengeId": None,
                        "queuedAt": now,
                        "startedAt": None,
                        "finishedAt": None,
                    }
                )
            if added and (self._worker is None or not self._worker.is_alive()):
                self._worker = Thread(target=self._drain, name="studio-publish-queue", daemon=True)
                self._worker.start()
            jobs = [dict(job) for job in self._jobs]
        return {
            "running": any(job["state"] == "running" for job in jobs),
            "pending": sum(1 for job in jobs if job["state"] in self.ACTIVE_STATES),
            "added": added,
            "jobs": jobs,
        }

    def clear(self) -> dict[str, Any]:
        """Drop queued work and finished rows; a running publish is left alone."""
        with self._lock:
            self._jobs = [job for job in self._jobs if job["state"] == "running"]
        return self.snapshot()

    def _next_queued(self) -> dict[str, Any] | None:
        with self._lock:
            job = next((item for item in self._jobs if item["state"] == "queued"), None)
            if job is None:
                # Cleared under the same lock enqueue uses, so a set queued a
                # moment ago cannot be stranded without a worker.
                self._worker = None
                return None
            job["state"] = "running"
            job["startedAt"] = __import__("studio_server").iso_utc()
            return job

    def _drain(self) -> None:
        while True:
            job = self._next_queued()
            if job is None:
                return
            try:
                attempt = self.publisher.publish_immediately(job["setId"])
            except Exception as error:  # noqa: BLE001 - one bad set must not stop the queue
                with self._lock:
                    job["state"] = "failed"
                    job["error"] = str(error)[:500]
                    job["finishedAt"] = __import__("studio_server").iso_utc()
            else:
                with self._lock:
                    job["state"] = "succeeded"
                    job["challengeId"] = StudioPublisher._challenge_id(attempt)
                    job["finishedAt"] = __import__("studio_server").iso_utc()
