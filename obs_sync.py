#!/usr/bin/env python3
"""Synchronize two OBS recordings over a LAN or private VPN.

This is intentionally a single-file, standard-library tool so it can be copied
to another Windows computer and launched with Python 3.10 or newer.

Both computers connect to their own local OBS WebSocket server. One computer
also hosts a tiny coordination server. Start and stop commands are announced
for a shared future time, which removes ordinary network latency from the
recording boundary.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import queue
import secrets
import socket
import struct
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse


APP_NAME = "R6 Soundle OBS Sync"
APP_VERSION = "1.2"
PROFILE_NAME = "R6 Soundle 1080p60"
DEFAULT_OBS_URL = "ws://127.0.0.1:4455"
DEFAULT_COORDINATOR_PORT = 8765
START_LEAD_SECONDS = 3.0
STOP_LEAD_SECONDS = 2.0
OUTPUT_EVENTS = 1 << 6
NO_HOTKEY_BINDINGS = json.dumps({"bindings": []}, separators=(",", ":"))


class SyncError(RuntimeError):
    """A user-facing synchronization or OBS error."""


def utc_session_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(2)}"


def default_capture_directory() -> Path:
    configured = os.environ.get("R6_SOUNDLE_CAPTURE_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parent / "media" / "raw"


def wait_until_unix(target_unix: float) -> float:
    """Wait against a monotonic deadline and return the actual Unix time."""
    remaining = target_unix - time.time()
    deadline = time.perf_counter() + max(0.0, remaining)
    while True:
        left = deadline - time.perf_counter()
        if left <= 0:
            return time.time()
        if left > 0.050:
            time.sleep(left - 0.025)
        elif left > 0.005:
            time.sleep(left / 2)
        else:
            time.sleep(0.0005)


class WindowsGlobalHotkey:
    """Register Ctrl+` without requiring the recorder window to have focus."""

    WM_HOTKEY = 0x0312
    WM_QUIT = 0x0012
    MOD_CONTROL = 0x0002
    MOD_NOREPEAT = 0x4000
    VK_OEM_3 = 0xC0
    HOTKEY_ID = 0x5236

    def __init__(self, callback: Callable[[], None]):
        self.callback = callback
        self.error = ""
        self._ready = threading.Event()
        self._registered = False
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None

    def start(self) -> bool:
        if os.name != "nt":
            self.error = "global Ctrl+` is only available on Windows"
            return False
        self._thread = threading.Thread(target=self._message_loop, name="obs-sync-hotkey", daemon=True)
        self._thread.start()
        if not self._ready.wait(2.0):
            self.error = "timed out while registering Ctrl+`"
            return False
        return self._registered

    def _message_loop(self) -> None:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._thread_id = int(kernel32.GetCurrentThreadId())
        modifiers = self.MOD_CONTROL | self.MOD_NOREPEAT
        if not user32.RegisterHotKey(None, self.HOTKEY_ID, modifiers, self.VK_OEM_3):
            error_code = ctypes.get_last_error()
            self.error = f"Windows could not register Ctrl+` (error {error_code}); another app may be using it"
            self._ready.set()
            return
        self._registered = True
        self._ready.set()
        message = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == self.WM_HOTKEY and message.wParam == self.HOTKEY_ID:
                    self.callback()
        finally:
            user32.UnregisterHotKey(None, self.HOTKEY_ID)
            self._registered = False

    def close(self) -> None:
        if os.name != "nt" or self._thread_id is None:
            return
        import ctypes

        ctypes.windll.user32.PostThreadMessageW(self._thread_id, self.WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(0.5)
        self._thread_id = None


class ObsWebSocket:
    """Minimal OBS WebSocket 5.x client using only the Python standard library."""

    def __init__(self, url: str, password: str = "", timeout: float = 6.0):
        self.url = url
        self.password = password
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self._receive_buffer = bytearray()
        self._call_lock = threading.Lock()

    def connect(self) -> dict[str, Any]:
        self.close()
        parsed = urlparse(self.url)
        if parsed.scheme != "ws" or not parsed.hostname:
            raise SyncError("OBS address must look like ws://127.0.0.1:4455")
        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path += f"?{parsed.query}"
        sock = socket.create_connection((parsed.hostname, port), self.timeout)
        sock.settimeout(self.timeout)
        websocket_key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {websocket_key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(request.encode("ascii"))
        response, remainder = self._read_http_headers(sock)
        first_line = response.split("\r\n", 1)[0]
        if " 101 " not in first_line:
            sock.close()
            raise SyncError(f"OBS WebSocket upgrade failed: {first_line}")
        expected_accept = base64.b64encode(
            hashlib.sha1(
                (websocket_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
            ).digest()
        ).decode("ascii")
        headers = {}
        for line in response.split("\r\n")[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        if headers.get("sec-websocket-accept") != expected_accept:
            sock.close()
            raise SyncError("OBS returned an invalid WebSocket handshake")
        self.sock = sock
        self._receive_buffer = bytearray(remainder)

        hello = self._receive_json()
        if hello.get("op") != 0:
            raise SyncError("OBS did not send the expected WebSocket greeting")
        authentication = hello.get("d", {}).get("authentication")
        identify: dict[str, Any] = {
            "rpcVersion": 1,
            "eventSubscriptions": OUTPUT_EVENTS,
        }
        if authentication:
            if not self.password:
                raise SyncError("OBS WebSocket requires the password shown in Tools > WebSocket Server Settings")
            secret = base64.b64encode(
                hashlib.sha256(
                    (self.password + authentication["salt"]).encode("utf-8")
                ).digest()
            ).decode("ascii")
            identify["authentication"] = base64.b64encode(
                hashlib.sha256(
                    (secret + authentication["challenge"]).encode("utf-8")
                ).digest()
            ).decode("ascii")
        self._send_json({"op": 1, "d": identify})
        identified = self._receive_json()
        if identified.get("op") != 2:
            raise SyncError("OBS rejected the WebSocket connection or password")
        return hello.get("d", {})

    @staticmethod
    def _read_http_headers(sock: socket.socket) -> tuple[str, bytes]:
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                raise SyncError("Connection closed during WebSocket handshake")
            data.extend(chunk)
            if len(data) > 65536:
                raise SyncError("Oversized WebSocket handshake")
        headers, remainder = bytes(data).split(b"\r\n\r\n", 1)
        return headers.decode("iso-8859-1") + "\r\n\r\n", remainder

    def _recv_exact(self, size: int) -> bytes:
        if self.sock is None:
            raise SyncError("Not connected to OBS")
        data = bytearray()
        if self._receive_buffer:
            take = min(size, len(self._receive_buffer))
            data.extend(self._receive_buffer[:take])
            del self._receive_buffer[:take]
        while len(data) < size:
            chunk = self.sock.recv(size - len(data))
            if not chunk:
                raise SyncError("OBS WebSocket connection closed")
            data.extend(chunk)
        return bytes(data)

    def _send_frame(self, payload: bytes, opcode: int = 0x1) -> None:
        if self.sock is None:
            raise SyncError("Not connected to OBS")
        first = 0x80 | opcode
        length = len(payload)
        mask = os.urandom(4)
        if length < 126:
            header = bytes((first, 0x80 | length))
        elif length <= 0xFFFF:
            header = bytes((first, 0x80 | 126)) + struct.pack("!H", length)
        else:
            header = bytes((first, 0x80 | 127)) + struct.pack("!Q", length)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def _receive_frame(self) -> tuple[int, bool, bytes]:
        if self.sock is None:
            raise SyncError("Not connected to OBS")
        first, second = self._recv_exact(2)
        final = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length)
        if masked:
            payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        return opcode, final, payload

    def _send_json(self, message: dict[str, Any]) -> None:
        self._send_frame(json.dumps(message, separators=(",", ":")).encode("utf-8"))

    def _receive_json(self) -> dict[str, Any]:
        fragments = bytearray()
        text_started = False
        while True:
            opcode, final, payload = self._receive_frame()
            if opcode == 0x8:
                raise SyncError("OBS closed the WebSocket connection")
            if opcode == 0x9:
                self._send_frame(payload, opcode=0xA)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x1:
                fragments = bytearray(payload)
                text_started = True
            elif opcode == 0x0 and text_started:
                fragments.extend(payload)
            else:
                continue
            if final:
                try:
                    return json.loads(fragments.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise SyncError(f"OBS returned invalid JSON: {error}") from error

    def call(self, request_type: str, request_data: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        payload: dict[str, Any] = {
            "requestType": request_type,
            "requestId": request_id,
        }
        if request_data is not None:
            payload["requestData"] = request_data
        with self._call_lock:
            self._send_json({"op": 6, "d": payload})
            while True:
                message = self._receive_json()
                if message.get("op") != 7:
                    continue
                data = message.get("d", {})
                if data.get("requestId") != request_id:
                    continue
                status = data.get("requestStatus", {})
                if not status.get("result"):
                    comment = status.get("comment") or f"OBS error {status.get('code', 'unknown')}"
                    raise SyncError(f"{request_type}: {comment}")
                return data.get("responseData", {})

    def close(self) -> None:
        sock, self.sock = self.sock, None
        self._receive_buffer.clear()
        if sock is None:
            return
        try:
            mask = os.urandom(4)
            sock.sendall(bytes((0x88, 0x80)) + mask)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass


class ObsController:
    def __init__(self, url: str, password: str, log: Callable[[str], None], capture_root: Path | None = None):
        self.url = url
        self.password = password
        self.log = log
        self.ws: ObsWebSocket | None = None
        self.available_requests: set[str] = set()
        self.obs_version = (0, 0, 0)
        self.capture_root = (capture_root or default_capture_directory()).resolve()
        self.capture_directory = self.capture_root
        self._lock = threading.RLock()

    def connect(self) -> dict[str, Any]:
        with self._lock:
            if self.ws is not None:
                self.ws.close()
            self.ws = ObsWebSocket(self.url, self.password)
            greeting = self.ws.connect()
            version = self.ws.call("GetVersion")
            self.available_requests = set(version.get("availableRequests", []))
            version_text = str(version.get("obsVersion", greeting.get("obsStudioVersion", "0")))
            numbers = []
            for part in version_text.split(".")[:3]:
                digits = "".join(character for character in part if character.isdigit())
                numbers.append(int(digits or 0))
            self.obs_version = tuple((numbers + [0, 0, 0])[:3])
            self.log(
                f"Connected to OBS {version.get('obsVersion', greeting.get('obsStudioVersion', '?'))} "
                f"(WebSocket {version.get('obsWebSocketVersion', greeting.get('obsWebSocketVersion', '?'))})"
            )
            return version

    def _call(self, request_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            if self.ws is None:
                self.connect()
            assert self.ws is not None
            try:
                return self.ws.call(request_type, data)
            except (OSError, TimeoutError):
                self.log("OBS connection dropped; reconnecting once")
                self.connect()
                assert self.ws is not None
                return self.ws.call(request_type, data)

    def install_profile(self) -> dict[str, Any]:
        with self._lock:
            self.capture_directory.mkdir(parents=True, exist_ok=True)
            status = self._call("GetRecordStatus")
            if status.get("outputActive"):
                raise SyncError("Stop the current OBS recording before installing the profile")
            profiles = self._call("GetProfileList")
            if PROFILE_NAME in profiles.get("profiles", []):
                if profiles.get("currentProfileName") != PROFILE_NAME:
                    self._call("SetCurrentProfile", {"profileName": PROFILE_NAME})
                action = "Updated"
            else:
                self._call("CreateProfile", {"profileName": PROFILE_NAME})
                action = "Created"

            self._call(
                "SetVideoSettings",
                {
                    "fpsNumerator": 60,
                    "fpsDenominator": 1,
                    "baseWidth": 1920,
                    "baseHeight": 1080,
                    "outputWidth": 1920,
                    "outputHeight": 1080,
                },
            )
            recording_format = "hybrid_mp4" if self.obs_version >= (30, 2, 0) else "mkv"
            if recording_format == "mkv":
                self.log("OBS is older than 30.2; using crash-safe MKV instead of Hybrid MP4")
            parameters = (
                ("Output", "Mode", "Simple"),
                ("Output", "FilenameFormatting", "R6-Soundle-%CCYY-%MM-%DD_%hh-%mm-%ss"),
                ("SimpleOutput", "RecQuality", "HQ"),
                ("SimpleOutput", "RecFormat2", recording_format),
                ("SimpleOutput", "RecTracks", "1"),
                ("Audio", "SampleRate", "48000"),
                ("Audio", "ChannelSetup", "Stereo"),
                ("Video", "ColorFormat", "NV12"),
                ("Video", "ColorSpace", "709"),
                ("Video", "ColorRange", "Partial"),
                ("Hotkeys", "OBSBasic.StartRecording", NO_HOTKEY_BINDINGS),
                ("Hotkeys", "OBSBasic.StopRecording", NO_HOTKEY_BINDINGS),
            )
            for category, name, value in parameters:
                self._call(
                    "SetProfileParameter",
                    {
                        "parameterCategory": category,
                        "parameterName": name,
                        "parameterValue": value,
                    },
                )
            self._set_record_directory()
            video = self._call("GetVideoSettings")
            expected = {
                "fpsNumerator": 60,
                "fpsDenominator": 1,
                "baseWidth": 1920,
                "baseHeight": 1080,
                "outputWidth": 1920,
                "outputHeight": 1080,
            }
            if any(video.get(key) != value for key, value in expected.items()):
                raise SyncError(f"OBS did not retain the requested 1080p60 settings: {video}")
            self.log(f"{action} OBS profile '{PROFILE_NAME}'; the sync recorder now owns Ctrl+`")
            return video

    def _set_record_directory(self) -> None:
        self.capture_directory.mkdir(parents=True, exist_ok=True)
        if "SetRecordDirectory" in self.available_requests:
            self._call(
                "SetRecordDirectory",
                {"recordDirectory": str(self.capture_directory)},
            )
        else:
            self._call(
                "SetProfileParameter",
                {
                    "parameterCategory": "SimpleOutput",
                    "parameterName": "FilePath",
                    "parameterValue": str(self.capture_directory),
                },
            )

    def prepare_session(self, session_id: str) -> None:
        with self._lock:
            profiles = self._call("GetProfileList")
            if PROFILE_NAME not in profiles.get("profiles", []):
                self.install_profile()
            elif profiles.get("currentProfileName") != PROFILE_NAME:
                self._call("SetCurrentProfile", {"profileName": PROFILE_NAME})
            status = self._call("GetRecordStatus")
            if status.get("outputActive"):
                raise SyncError("OBS is already recording")
            safe_id = "".join(character for character in session_id if character.isalnum() or character in "-_")
            self.capture_directory = self.capture_root / safe_id
            self._set_record_directory()
            self._call(
                "SetProfileParameter",
                {
                    "parameterCategory": "Output",
                    "parameterName": "FilenameFormatting",
                    "parameterValue": f"R6-Soundle-{safe_id}",
                },
            )

    def start_recording(self) -> dict[str, Any]:
        requested_at = time.time()
        self._call("StartRecord")
        acknowledged_at = time.time()
        deadline = time.perf_counter() + 3.0
        status: dict[str, Any] = {}
        while time.perf_counter() < deadline:
            status = self._call("GetRecordStatus")
            if status.get("outputActive"):
                break
            time.sleep(0.050)
        else:
            raise SyncError("OBS did not enter the recording state within 3 seconds")
        return {
            "requestedAt": requested_at,
            "acknowledgedAt": acknowledged_at,
            "obsDurationMs": status.get("outputDuration"),
        }

    def stop_recording(self) -> dict[str, Any]:
        before = self._call("GetRecordStatus")
        requested_at = time.time()
        if before.get("outputActive"):
            response = self._call("StopRecord")
        else:
            response = {}
        acknowledged_at = time.time()
        return {
            "requestedAt": requested_at,
            "acknowledgedAt": acknowledged_at,
            "obsDurationMs": before.get("outputDuration"),
            "outputPath": response.get("outputPath"),
            "wasActive": bool(before.get("outputActive")),
        }

    def recording_status(self) -> dict[str, Any]:
        return self._call("GetRecordStatus")

    def close(self) -> None:
        with self._lock:
            if self.ws is not None:
                self.ws.close()
                self.ws = None


class JsonLineSocket:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.reader = sock.makefile("rb")
        self._send_lock = threading.Lock()

    def send(self, message: dict[str, Any]) -> None:
        payload = json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
        with self._send_lock:
            self.sock.sendall(payload)

    def receive(self) -> dict[str, Any] | None:
        line = self.reader.readline(1_048_577)
        if not line:
            return None
        if len(line) > 1_048_576:
            raise SyncError("Coordinator message exceeded 1 MB")
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SyncError(f"Invalid coordinator message: {error}") from error
        if not isinstance(value, dict):
            raise SyncError("Coordinator message must be a JSON object")
        return value

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.reader.close()
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class SyncCoordinator:
    """Two-participant rendezvous server. Intended for LAN/private VPN use."""

    def __init__(self, bind_host: str, port: int, room_code: str, log: Callable[[str], None]):
        self.bind_host = bind_host
        self.port = port
        self.room_code = room_code
        self.log = log
        self.server: socket.socket | None = None
        self.clients: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._closing = threading.Event()
        self.session_id: str | None = None
        self.start_at: float | None = None
        self.stop_at: float | None = None
        self.stopped_clients: set[str] = set()

    def start(self) -> int:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self.bind_host, self.port))
        server.listen(4)
        server.settimeout(0.5)
        self.server = server
        self.port = server.getsockname()[1]
        threading.Thread(target=self._accept_loop, name="obs-sync-coordinator", daemon=True).start()
        self.log(f"Coordinator listening on {self.bind_host}:{self.port}")
        return self.port

    def _accept_loop(self) -> None:
        assert self.server is not None
        while not self._closing.is_set():
            try:
                sock, address = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            sock.settimeout(10.0)
            threading.Thread(
                target=self._client_loop,
                args=(sock, address),
                name=f"obs-sync-peer-{address[0]}",
                daemon=True,
            ).start()

    def _client_loop(self, sock: socket.socket, address: tuple[str, int]) -> None:
        connection = JsonLineSocket(sock)
        client_id: str | None = None
        try:
            hello = connection.receive()
            if not hello or hello.get("type") != "hello":
                raise SyncError("First coordinator message must be hello")
            if str(hello.get("room", "")) != self.room_code:
                connection.send({"type": "error", "message": "Room code does not match"})
                return
            with self._lock:
                if len(self.clients) >= 2:
                    connection.send({"type": "error", "message": "This room already has two players"})
                    return
                client_id = secrets.token_hex(4)
                self.clients[client_id] = {
                    "connection": connection,
                    "name": str(hello.get("name") or "Player")[:40],
                    "ready": False,
                    "address": address[0],
                }
                connection.send(
                    {
                        "type": "welcome",
                        "clientId": client_id,
                        "serverTime": time.time(),
                        "version": APP_VERSION,
                    }
                )
                self._broadcast_state_locked()
            sock.settimeout(None)
            while not self._closing.is_set():
                message = connection.receive()
                if message is None:
                    break
                self._handle_message(client_id, message)
        except (OSError, SyncError) as error:
            self.log(f"Coordinator peer {address[0]} disconnected: {error}")
        finally:
            if client_id is not None:
                self._remove_client(client_id)
            connection.close()

    def _handle_message(self, client_id: str, message: dict[str, Any]) -> None:
        message_type = message.get("type")
        if message_type == "sync_ping":
            with self._lock:
                client = self.clients.get(client_id)
                if client:
                    client["connection"].send(
                        {
                            "type": "sync_pong",
                            "id": message.get("id"),
                            "serverTime": time.time(),
                        }
                    )
            return
        with self._lock:
            client = self.clients.get(client_id)
            if client is None:
                return
            if message_type == "ready":
                if self.session_id is None:
                    client["ready"] = bool(message.get("ready", True))
                    self._broadcast_state_locked()
                    self._maybe_schedule_start_locked()
            elif message_type == "stop_request":
                self._schedule_stop_locked(message.get("reason") or f"requested by {client['name']}")
            elif message_type == "started":
                self._broadcast_locked(
                    {
                        "type": "peer_started",
                        "clientId": client_id,
                        "name": client["name"],
                        "actualAt": message.get("actualAt"),
                    }
                )
            elif message_type == "stopped":
                self.stopped_clients.add(client_id)
                self._broadcast_locked(
                    {
                        "type": "peer_stopped",
                        "clientId": client_id,
                        "name": client["name"],
                        "durationMs": message.get("durationMs"),
                    }
                )
                if self.session_id and self.stopped_clients.issuperset(self.clients):
                    completed_session = self.session_id
                    for entry in self.clients.values():
                        entry["ready"] = False
                    self.session_id = None
                    self.start_at = None
                    self.stop_at = None
                    self.stopped_clients.clear()
                    self._broadcast_locked({"type": "session_complete", "sessionId": completed_session})
                    self._broadcast_state_locked()
            elif message_type == "recording_stopped_early":
                reason = str(message.get("reason") or "OBS reported that recording became inactive")
                self._schedule_stop_locked(f"{client['name']}: {reason}", emergency=True)

    def _maybe_schedule_start_locked(self) -> None:
        if self.session_id is not None or len(self.clients) != 2:
            return
        if not all(client["ready"] for client in self.clients.values()):
            return
        self.session_id = utc_session_id()
        self.start_at = time.time() + START_LEAD_SECONDS
        self.stop_at = None
        self.stopped_clients.clear()
        self._broadcast_locked(
            {
                "type": "start",
                "sessionId": self.session_id,
                "startAt": self.start_at,
                "leadSeconds": START_LEAD_SECONDS,
            }
        )
        self.log(f"Scheduled session {self.session_id} to start in {START_LEAD_SECONDS:.0f}s")

    def _schedule_stop_locked(self, reason: str, emergency: bool = False) -> None:
        if self.session_id is None or self.stop_at is not None:
            return
        lead = 0.15 if emergency else STOP_LEAD_SECONDS
        self.stop_at = time.time() + lead
        self._broadcast_locked(
            {
                "type": "stop",
                "sessionId": self.session_id,
                "stopAt": self.stop_at,
                "reason": reason,
                "emergency": emergency,
            }
        )
        self.log(f"Scheduled stop in {lead:.2f}s: {reason}")

    def _broadcast_state_locked(self) -> None:
        players = [
            {
                "id": client_id,
                "name": client["name"],
                "ready": client["ready"],
            }
            for client_id, client in self.clients.items()
        ]
        self._broadcast_locked(
            {
                "type": "state",
                "players": players,
                "sessionId": self.session_id,
                "startAt": self.start_at,
                "stopAt": self.stop_at,
            }
        )

    def _broadcast_locked(self, message: dict[str, Any]) -> None:
        dead = []
        for client_id, client in self.clients.items():
            try:
                client["connection"].send(message)
            except OSError:
                dead.append(client_id)
        for client_id in dead:
            self.clients.pop(client_id, None)

    def _remove_client(self, client_id: str) -> None:
        with self._lock:
            client = self.clients.pop(client_id, None)
            self.stopped_clients.discard(client_id)
            if client:
                self.log(f"{client['name']} left the room")
            if self.session_id and self.stop_at is None:
                self._schedule_stop_locked("a player disconnected", emergency=True)
            self._broadcast_state_locked()

    def close(self) -> None:
        self._closing.set()
        if self.server is not None:
            try:
                self.server.close()
            except OSError:
                pass
            self.server = None
        with self._lock:
            for client in self.clients.values():
                client["connection"].close()
            self.clients.clear()


class SyncClient:
    def __init__(
        self,
        name: str,
        room_code: str,
        obs: ObsController,
        log: Callable[[str], None],
        event: Callable[[str, dict[str, Any]], None],
    ):
        self.name = name
        self.room_code = room_code
        self.obs = obs
        self.log = log
        self.event = event
        self.connection: JsonLineSocket | None = None
        self._send_lock = threading.Lock()
        self._closing = threading.Event()
        self._sync_pending: dict[str, float] = {}
        self._sync_samples: list[tuple[float, float]] = []
        self.clock_offset = 0.0
        self.client_id: str | None = None
        self.current_session: str | None = None
        self.stop_scheduled = False
        self.record_info: dict[str, Any] = {}

    def connect(self, host: str, port: int) -> None:
        sock = socket.create_connection((host, port), 8.0)
        sock.settimeout(None)
        self.connection = JsonLineSocket(sock)
        self._send(
            {
                "type": "hello",
                "name": self.name,
                "room": self.room_code,
                "version": APP_VERSION,
            }
        )
        threading.Thread(target=self._reader_loop, name="obs-sync-reader", daemon=True).start()
        threading.Thread(target=self._clock_sync_loop, name="obs-sync-clock", daemon=True).start()

    def _send(self, message: dict[str, Any]) -> None:
        if self.connection is None:
            raise SyncError("Not connected to a sync room")
        with self._send_lock:
            self.connection.send(message)

    def _reader_loop(self) -> None:
        try:
            assert self.connection is not None
            while not self._closing.is_set():
                message = self.connection.receive()
                if message is None:
                    break
                self._handle_message(message)
        except (OSError, SyncError) as error:
            if not self._closing.is_set():
                self.log(f"Coordinator connection ended: {error}")
        finally:
            self.event("disconnected", {})

    def _clock_sync_loop(self) -> None:
        for _ in range(9):
            if self._closing.is_set():
                return
            ping_id = secrets.token_hex(4)
            sent = time.time()
            self._sync_pending[ping_id] = sent
            try:
                self._send({"type": "sync_ping", "id": ping_id})
            except (OSError, SyncError):
                return
            time.sleep(0.18)

    def _handle_message(self, message: dict[str, Any]) -> None:
        message_type = message.get("type")
        if message_type == "error":
            self.log(f"Coordinator rejected connection: {message.get('message', 'unknown error')}")
            self.event("error", message)
        elif message_type == "welcome":
            self.client_id = str(message.get("clientId") or "") or None
            self.log("Joined synchronization room")
            self.event("connected", message)
        elif message_type == "sync_pong":
            received = time.time()
            sent = self._sync_pending.pop(str(message.get("id")), None)
            if sent is not None:
                round_trip = received - sent
                offset = float(message.get("serverTime", received)) - ((sent + received) / 2)
                self._sync_samples.append((round_trip, offset))
                best = sorted(self._sync_samples)[:3]
                offsets = sorted(sample[1] for sample in best)
                self.clock_offset = offsets[len(offsets) // 2]
                self.event(
                    "clock",
                    {
                        "offsetMs": self.clock_offset * 1000,
                        "roundTripMs": min(sample[0] for sample in self._sync_samples) * 1000,
                    },
                )
        elif message_type == "state":
            self.event("state", message)
        elif message_type == "start":
            threading.Thread(
                target=self._run_start,
                args=(message,),
                name="obs-sync-start",
                daemon=True,
            ).start()
        elif message_type == "stop":
            self.stop_scheduled = True
            threading.Thread(
                target=self._run_stop,
                args=(message,),
                name="obs-sync-stop",
                daemon=True,
            ).start()
        elif message_type in {"peer_started", "peer_stopped", "session_complete"}:
            self.event(message_type, message)

    def set_ready(self) -> None:
        self.obs.install_profile()
        deadline = time.time() + 2.0
        while len(self._sync_samples) < 5 and time.time() < deadline and not self._closing.is_set():
            time.sleep(0.05)
        if len(self._sync_samples) < 2:
            raise SyncError("Could not measure the coordinator clock; check the room connection")
        self._send({"type": "ready", "ready": True})
        self.log("Ready; waiting for the other player")

    def request_stop(self) -> None:
        self._send({"type": "stop_request", "reason": f"Stop Both pressed by {self.name}"})

    def _host_to_local_time(self, host_timestamp: float) -> float:
        return host_timestamp - self.clock_offset

    def _run_start(self, message: dict[str, Any]) -> None:
        session_id = str(message["sessionId"])
        host_start_at = float(message["startAt"])
        local_start_at = self._host_to_local_time(host_start_at)
        self.current_session = session_id
        self.stop_scheduled = False
        self.record_info = {
            "tool": APP_NAME,
            "version": APP_VERSION,
            "sessionId": session_id,
            "player": self.name,
            "hostStartAt": host_start_at,
            "localStartAt": local_start_at,
            "clockOffsetMs": self.clock_offset * 1000,
        }
        self.event("start_scheduled", {**message, "localStartAt": local_start_at})
        try:
            self.obs.prepare_session(session_id)
            actual_wait_at = wait_until_unix(local_start_at)
            result = self.obs.start_recording()
            actual_at = float(result["requestedAt"])
            self.record_info["start"] = {
                **result,
                "schedulerReleasedAt": actual_wait_at,
                "errorMs": (actual_at - local_start_at) * 1000,
            }
            self._write_sidecar()
            self._send(
                {
                    "type": "started",
                    "sessionId": session_id,
                    "actualAt": actual_at + self.clock_offset,
                    "errorMs": (actual_at - local_start_at) * 1000,
                }
            )
            self.event("recording", self.record_info["start"])
            threading.Thread(target=self._monitor_recording, name="obs-sync-monitor", daemon=True).start()
        except Exception as error:
            self.log(f"Could not start synchronized recording: {error}")
            self.event("error", {"message": str(error)})
            try:
                self._send({"type": "recording_stopped_early", "reason": str(error)})
            except Exception:
                pass

    def _run_stop(self, message: dict[str, Any]) -> None:
        host_stop_at = float(message["stopAt"])
        local_stop_at = self._host_to_local_time(host_stop_at)
        self.record_info["hostStopAt"] = host_stop_at
        self.record_info["localStopAt"] = local_stop_at
        self.record_info["stopReason"] = message.get("reason")
        self.event("stop_scheduled", {**message, "localStopAt": local_stop_at})
        try:
            actual_wait_at = wait_until_unix(local_stop_at)
            result = self.obs.stop_recording()
            actual_at = float(result["requestedAt"])
            self.record_info["stop"] = {
                **result,
                "schedulerReleasedAt": actual_wait_at,
                "errorMs": (actual_at - local_stop_at) * 1000,
            }
            self._write_sidecar()
            self._send(
                {
                    "type": "stopped",
                    "sessionId": self.current_session,
                    "actualAt": actual_at + self.clock_offset,
                    "durationMs": result.get("obsDurationMs"),
                    "errorMs": (actual_at - local_stop_at) * 1000,
                }
            )
            self.event("stopped", result)
            self.current_session = None
        except Exception as error:
            self.log(f"Could not stop synchronized recording: {error}")
            self.event("error", {"message": str(error)})

    def _monitor_recording(self) -> None:
        consecutive_failures = 0
        consecutive_inactive = 0
        while self.current_session and not self._closing.wait(0.5):
            if self.stop_scheduled:
                return
            try:
                active = bool(self.obs.recording_status().get("outputActive"))
                consecutive_failures = 0
            except Exception:
                consecutive_failures += 1
                if consecutive_failures < 3:
                    continue
                return
            if active:
                consecutive_inactive = 0
                continue
            consecutive_inactive += 1
            if consecutive_inactive < 3:
                continue
            reason = "OBS reported recording inactive for three consecutive checks"
            self.log(f"{reason}; sending an emergency stop")
            try:
                self._send({"type": "recording_stopped_early", "reason": reason})
            except Exception:
                pass
            return

    def _write_sidecar(self) -> None:
        if not self.current_session:
            return
        self.obs.capture_directory.mkdir(parents=True, exist_ok=True)
        path = self.obs.capture_directory / f"R6-Soundle-{self.current_session}-sync.json"
        path.write_text(json.dumps(self.record_info, indent=2), encoding="utf-8")

    def close(self) -> None:
        self._closing.set()
        if self.connection is not None:
            self.connection.close()
            self.connection = None


def local_ipv4_addresses() -> list[str]:
    addresses = {"127.0.0.1"}
    try:
        for result in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(result[4][0])
    except OSError:
        pass
    return sorted(addresses, key=lambda value: value.startswith("127."))


def parse_host_port(value: str) -> tuple[str, int]:
    value = value.strip()
    if not value:
        raise SyncError("Enter the host computer's address")
    if value.count(":") == 1:
        host, port_text = value.rsplit(":", 1)
        try:
            return host.strip(), int(port_text)
        except ValueError as error:
            raise SyncError("Coordinator port must be a number") from error
    return value, DEFAULT_COORDINATOR_PORT


class SyncApp:
    def __init__(self, capture_root: Path) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.minsize(720, 640)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.coordinator: SyncCoordinator | None = None
        self.client: SyncClient | None = None
        self.obs: ObsController | None = None
        self.capture_root = capture_root.resolve()
        self.countdown_target: float | None = None
        self.countdown_label_text = ""
        self.capture_phase = "idle"
        self.hotkey = WindowsGlobalHotkey(lambda: self.events.put(("capture_hotkey", {})))

        self.name_var = tk.StringVar(value=os.environ.get("USERNAME") or os.environ.get("USER") or "Player")
        self.obs_url_var = tk.StringVar(value=DEFAULT_OBS_URL)
        self.obs_password_var = tk.StringVar()
        self.room_var = tk.StringVar(value=secrets.token_hex(3).upper())
        self.join_var = tk.StringVar(value=f"127.0.0.1:{DEFAULT_COORDINATOR_PORT}")
        self.obs_status_var = tk.StringVar(value="OBS: not tested")
        self.room_status_var = tk.StringVar(value="Room: disconnected")
        self.players_var = tk.StringVar(value="Players: 0 / 2")
        self.clock_var = tk.StringVar(value="Clock sync: waiting")
        self.countdown_var = tk.StringVar(value="")

        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text=APP_NAME, font=("Segoe UI", 18, "bold")).pack(anchor="w")
        ttk.Label(
            outer,
            text="Both players run this file. OBS stays local; only timing messages cross the network.",
        ).pack(anchor="w", pady=(2, 12))
        ttk.Label(
            outer,
            text=f"Raw captures: {self.capture_root}",
        ).pack(anchor="w", pady=(0, 12))

        obs_frame = ttk.LabelFrame(outer, text="1. Local OBS", padding=10)
        obs_frame.pack(fill="x")
        self._labeled_entry(obs_frame, "Display name", self.name_var, row=0)
        self._labeled_entry(obs_frame, "OBS address", self.obs_url_var, row=1)
        self._labeled_entry(obs_frame, "OBS password", self.obs_password_var, row=2, show="•")
        obs_buttons = ttk.Frame(obs_frame)
        obs_buttons.grid(row=3, column=1, sticky="w", pady=(8, 0))
        ttk.Button(obs_buttons, text="Test OBS", command=self.test_obs).pack(side="left")
        ttk.Button(obs_buttons, text="Install / Update 1080p60 Profile", command=self.install_profile).pack(
            side="left", padx=(8, 0)
        )
        ttk.Label(obs_frame, textvariable=self.obs_status_var).grid(row=4, column=1, sticky="w", pady=(6, 0))
        ttk.Label(
            obs_frame,
            text="Ctrl+` works globally: press once to send READY, then press again while recording to STOP BOTH.",
            wraplength=600,
        ).grid(row=5, column=1, sticky="w", pady=(6, 0))

        room_frame = ttk.LabelFrame(outer, text="2. Synchronization room", padding=10)
        room_frame.pack(fill="x", pady=(12, 0))
        self._labeled_entry(room_frame, "Room code", self.room_var, row=0)
        self._labeled_entry(room_frame, "Host address", self.join_var, row=1)
        room_buttons = ttk.Frame(room_frame)
        room_buttons.grid(row=2, column=1, sticky="w", pady=(8, 0))
        ttk.Button(room_buttons, text="Host Room", command=self.host_room).pack(side="left")
        ttk.Button(room_buttons, text="Join Room", command=self.join_room).pack(side="left", padx=(8, 0))
        ttk.Button(room_buttons, text="Disconnect", command=self.disconnect_room).pack(side="left", padx=(8, 0))
        status = ttk.Frame(room_frame)
        status.grid(row=3, column=1, sticky="w", pady=(6, 0))
        ttk.Label(status, textvariable=self.room_status_var).pack(anchor="w")
        ttk.Label(status, textvariable=self.players_var).pack(anchor="w")
        ttk.Label(status, textvariable=self.clock_var).pack(anchor="w")

        controls = ttk.LabelFrame(outer, text="3. Capture", padding=12)
        controls.pack(fill="x", pady=(12, 0))
        self.ready_button = ttk.Button(controls, text="READY", command=self.ready)
        self.ready_button.pack(side="left", fill="x", expand=True)
        self.stop_button = ttk.Button(controls, text="STOP BOTH", command=self.stop_both)
        self.stop_button.pack(side="left", fill="x", expand=True, padx=(10, 0))
        self.stop_button.state(["disabled"])
        ttk.Label(controls, textvariable=self.countdown_var, font=("Segoe UI", 13, "bold")).pack(
            side="left", padx=(14, 0)
        )

        log_frame = ttk.LabelFrame(outer, text="Activity", padding=8)
        log_frame.pack(fill="both", expand=True, pady=(12, 0))
        self.log_text = tk.Text(log_frame, height=10, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True)

        ttk.Label(
            outer,
            text="Use on the same LAN or over a private VPN such as Tailscale. Do not expose OBS port 4455 to the internet.",
        ).pack(anchor="w", pady=(8, 0))
        self.root.after(50, self._drain_events)
        self.root.after(50, self._tick_countdown)
        if self.hotkey.start():
            self._queue_log("Global Ctrl+` registered: READY when idle, STOP BOTH while recording")
        else:
            self.root.bind_all("<Control-grave>", lambda _event: self.events.put(("capture_hotkey", {})))
            self._queue_log(f"{self.hotkey.error}; Ctrl+` will work only while this window has focus")

    def _labeled_entry(self, parent: Any, label: str, variable: Any, row: int, show: str | None = None) -> None:
        self.ttk.Label(parent, text=label).grid(row=row, column=0, sticky="e", padx=(0, 8), pady=3)
        entry = self.ttk.Entry(parent, textvariable=variable, width=48, show=show or "")
        entry.grid(row=row, column=1, sticky="ew", pady=3)
        parent.columnconfigure(1, weight=1)

    def _queue_log(self, text: str) -> None:
        self.events.put(("log", text))

    def _queue_event(self, event_type: str, data: dict[str, Any]) -> None:
        self.events.put((event_type, data))

    def _new_obs(self) -> ObsController:
        if self.obs is not None:
            self.obs.close()
        self.obs = ObsController(
            self.obs_url_var.get().strip(),
            self.obs_password_var.get(),
            self._queue_log,
            self.capture_root,
        )
        return self.obs

    def _background(self, action: Callable[[], None]) -> None:
        def runner() -> None:
            try:
                action()
            except Exception as error:
                self.events.put(("error", {"message": str(error)}))

        threading.Thread(target=runner, daemon=True).start()

    def test_obs(self) -> None:
        def action() -> None:
            obs = self._new_obs()
            version = obs.connect()
            self.events.put(("obs_ok", version))

        self.obs_status_var.set("OBS: connecting…")
        self._background(action)

    def install_profile(self) -> None:
        def action() -> None:
            obs = self._new_obs()
            obs.connect()
            video = obs.install_profile()
            self.events.put(("profile_ok", video))

        self.obs_status_var.set("OBS: installing profile…")
        self._background(action)

    def _make_client(self) -> SyncClient:
        obs = self._new_obs()
        obs.connect()
        return SyncClient(
            self.name_var.get().strip() or "Player",
            self.room_var.get().strip(),
            obs,
            self._queue_log,
            self._queue_event,
        )

    def host_room(self) -> None:
        def action() -> None:
            self._close_network()
            room = self.room_var.get().strip()
            if not room:
                raise SyncError("Enter a room code")
            self.coordinator = SyncCoordinator("0.0.0.0", DEFAULT_COORDINATOR_PORT, room, self._queue_log)
            port = self.coordinator.start()
            self.client = self._make_client()
            self.client.connect("127.0.0.1", port)
            addresses = ", ".join(f"{address}:{port}" for address in local_ipv4_addresses() if address != "127.0.0.1")
            self._queue_log(f"Give player 2 this address: {addresses or f'YOUR-IP:{port}'} and room code {room}")

        self.room_status_var.set("Room: starting host…")
        self._background(action)

    def join_room(self) -> None:
        def action() -> None:
            self._close_network()
            host, port = parse_host_port(self.join_var.get())
            self.client = self._make_client()
            self.client.connect(host, port)

        self.room_status_var.set("Room: connecting…")
        self._background(action)

    def ready(self) -> None:
        if self.client is None:
            self._queue_event("error", {"message": "Host or join a room first"})
            return
        if self.capture_phase != "idle":
            self._queue_log("Already ready or capturing; wait for the current session to finish")
            return
        self.capture_phase = "ready"
        self.ready_button.state(["disabled"])

        def action() -> None:
            assert self.client is not None
            self.client.set_ready()
            self.events.put(("ready_sent", {}))

        self._background(action)

    def stop_both(self) -> None:
        try:
            if self.client is None:
                raise SyncError("Not connected to a room")
            if self.capture_phase != "recording":
                raise SyncError("STOP BOTH is available after synchronized recording starts")
            self.capture_phase = "stopping"
            self.stop_button.state(["disabled"])
            self.client.request_stop()
        except Exception as error:
            if self.capture_phase == "stopping":
                self.capture_phase = "recording"
                self.stop_button.state(["!disabled"])
            self._queue_event("error", {"message": str(error)})

    def _capture_hotkey(self) -> None:
        if self.capture_phase == "idle":
            self.ready()
        elif self.capture_phase == "recording":
            self.stop_both()
        elif self.capture_phase == "ready":
            self._queue_log("Ctrl+`: already READY; waiting for the other player")
        elif self.capture_phase == "countdown":
            self._queue_log("Ctrl+`: recording is starting; wait for RECORDING before stopping")
        else:
            self._queue_log("Ctrl+`: STOP BOTH is already scheduled")

    def disconnect_room(self) -> None:
        self._close_network()
        self.room_status_var.set("Room: disconnected")
        self.players_var.set("Players: 0 / 2")
        self.capture_phase = "idle"
        self.ready_button.state(["!disabled"])
        self.stop_button.state(["disabled"])

    def _close_network(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None
        if self.coordinator is not None:
            self.coordinator.close()
            self.coordinator = None

    def _drain_events(self) -> None:
        try:
            while True:
                event_type, data = self.events.get_nowait()
                if event_type == "log":
                    self._append_log(str(data))
                elif event_type == "error":
                    message = data.get("message", "Unknown error")
                    self._append_log(f"ERROR: {message}")
                    self.obs_status_var.set("OBS: check activity log")
                    if self.capture_phase in {"idle", "ready"}:
                        self.capture_phase = "idle"
                        self.ready_button.state(["!disabled"])
                elif event_type == "obs_ok":
                    self.obs_status_var.set(f"OBS: connected ({data.get('obsVersion', '?')})")
                elif event_type == "profile_ok":
                    self.obs_status_var.set("OBS: R6 Soundle 1080p60 profile active; recorder owns Ctrl+`")
                elif event_type == "connected":
                    self.room_status_var.set("Room: connected")
                elif event_type == "disconnected":
                    self.room_status_var.set("Room: disconnected")
                    self.capture_phase = "idle"
                    self.ready_button.state(["!disabled"])
                    self.stop_button.state(["disabled"])
                elif event_type == "clock":
                    self.clock_var.set(
                        f"Clock sync: offset {data['offsetMs']:+.1f} ms, best RTT {data['roundTripMs']:.1f} ms"
                    )
                elif event_type == "state":
                    players = data.get("players", [])
                    ready = sum(1 for player in players if player.get("ready"))
                    names = ", ".join(
                        f"{player['name']}{' ✓' if player.get('ready') else ''}" for player in players
                    )
                    self.players_var.set(f"Players: {len(players)} / 2; ready {ready} / 2{'; ' + names if names else ''}")
                    if not data.get("sessionId"):
                        local_client_id = self.client.client_id if self.client is not None else None
                        local_ready = any(
                            player.get("id") == local_client_id and player.get("ready") for player in players
                        )
                        self.capture_phase = "ready" if local_ready else "idle"
                        self.ready_button.state(["disabled"] if local_ready else ["!disabled"])
                        self.stop_button.state(["disabled"])
                elif event_type == "ready_sent":
                    self._append_log("Ready status sent")
                elif event_type == "start_scheduled":
                    self.capture_phase = "countdown"
                    self.countdown_target = float(data["localStartAt"])
                    self.countdown_label_text = "Recording starts in"
                    self._append_log(f"Session {data['sessionId']} scheduled")
                elif event_type == "recording":
                    self.capture_phase = "recording"
                    self.countdown_target = None
                    self.countdown_var.set("RECORDING")
                    self.stop_button.state(["!disabled"])
                    self._append_log(f"Recording started; scheduler error {data.get('errorMs', 0):+.1f} ms")
                elif event_type == "stop_scheduled":
                    self.capture_phase = "stopping"
                    self.countdown_target = float(data["localStopAt"])
                    self.countdown_label_text = "Stops in"
                    self.stop_button.state(["disabled"])
                    self._append_log(f"Stop scheduled: {data.get('reason', '')}")
                elif event_type == "stopped":
                    self.countdown_target = None
                    self.countdown_var.set("STOPPED")
                    output_path = data.get("outputPath") or "OBS output folder"
                    self._append_log(f"Recording saved: {output_path}")
                elif event_type == "peer_started":
                    self._append_log(f"{data.get('name')} confirmed recording started")
                elif event_type == "peer_stopped":
                    duration = data.get("durationMs")
                    suffix = f" ({duration / 1000:.3f}s)" if isinstance(duration, (int, float)) else ""
                    self._append_log(f"{data.get('name')} confirmed recording stopped{suffix}")
                elif event_type == "session_complete":
                    self._append_log(f"Session {data.get('sessionId')} complete; both players can ready again")
                    self.capture_phase = "idle"
                    self.ready_button.state(["!disabled"])
                    self.stop_button.state(["disabled"])
                elif event_type == "capture_hotkey":
                    self._capture_hotkey()
        except queue.Empty:
            pass
        self.root.after(50, self._drain_events)

    def _tick_countdown(self) -> None:
        if self.countdown_target is not None:
            remaining = max(0.0, self.countdown_target - time.time())
            self.countdown_var.set(f"{self.countdown_label_text} {remaining:0.1f}s")
        self.root.after(50, self._tick_countdown)

    def _append_log(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{stamp}] {text}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def run(self) -> None:
        self._append_log("Open OBS and enable Tools > WebSocket Server Settings before connecting")
        self.root.mainloop()

    def close(self) -> None:
        self.hotkey.close()
        self._close_network()
        if self.obs is not None:
            self.obs.close()
        self.root.destroy()


def self_test() -> None:
    global START_LEAD_SECONDS, STOP_LEAD_SECONDS

    host, port = parse_host_port("example.test:9999")
    assert (host, port) == ("example.test", 9999)
    host, port = parse_host_port("example.test")
    assert (host, port) == ("example.test", DEFAULT_COORDINATOR_PORT)

    captured: list[str] = []
    coordinator = SyncCoordinator("127.0.0.1", 0, "ABC123", captured.append)
    port = coordinator.start()
    peers = []
    try:
        for name in ("Listener", "Runner"):
            raw = socket.create_connection(("127.0.0.1", port), 2.0)
            raw.settimeout(2.0)
            peer = JsonLineSocket(raw)
            peer.send({"type": "hello", "name": name, "room": "ABC123"})
            messages = [peer.receive(), peer.receive()]
            assert any(message and message.get("type") == "welcome" for message in messages)
            peers.append(peer)
        for peer in peers:
            peer.send({"type": "ready", "ready": True})
        starts = []
        deadline = time.time() + 2.0
        for peer in peers:
            while time.time() < deadline:
                message = peer.receive()
                if message and message.get("type") == "start":
                    starts.append(message)
                    break
        assert len(starts) == 2
        assert starts[0]["sessionId"] == starts[1]["sessionId"]
        assert abs(starts[0]["startAt"] - starts[1]["startAt"]) < 0.000001
    finally:
        for peer in peers:
            peer.close()
        coordinator.close()
    print("PASS: coordinator pairs two clients and broadcasts one shared future start timestamp")

    test_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    test_server.bind(("127.0.0.1", 0))
    test_server.listen(1)
    test_port = test_server.getsockname()[1]
    server_result: queue.Queue[Exception | None] = queue.Queue()

    def websocket_server_frame(message: dict[str, Any]) -> bytes:
        payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
        if len(payload) < 126:
            return bytes((0x81, len(payload))) + payload
        return bytes((0x81, 126)) + struct.pack("!H", len(payload)) + payload

    def receive_client_json(conn: socket.socket) -> dict[str, Any]:
        def exact(size: int) -> bytes:
            data = bytearray()
            while len(data) < size:
                chunk = conn.recv(size - len(data))
                if not chunk:
                    raise AssertionError("test WebSocket client disconnected")
                data.extend(chunk)
            return bytes(data)

        first, second = exact(2)
        assert first & 0x0F == 0x1
        assert second & 0x80
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", exact(8))[0]
        mask = exact(4)
        payload = exact(length)
        decoded = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        return json.loads(decoded.decode("utf-8"))

    def run_fake_obs() -> None:
        try:
            conn, _ = test_server.accept()
            with conn:
                headers = bytearray()
                while b"\r\n\r\n" not in headers:
                    headers.extend(conn.recv(4096))
                header_text = headers.decode("iso-8859-1")
                websocket_key = next(
                    line.split(":", 1)[1].strip()
                    for line in header_text.split("\r\n")
                    if line.lower().startswith("sec-websocket-key:")
                )
                accept_value = base64.b64encode(
                    hashlib.sha1(
                        (websocket_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
                    ).digest()
                ).decode("ascii")
                hello = {
                    "op": 0,
                    "d": {
                        "obsStudioVersion": "32.1.0",
                        "obsWebSocketVersion": "5.7.2",
                        "rpcVersion": 1,
                        "authentication": {"salt": "test-salt", "challenge": "test-challenge"},
                    },
                }
                handshake = (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept_value}\r\n\r\n"
                ).encode("ascii")
                # Deliberately coalesce the HTTP response and first frame. This
                # catches clients that accidentally discard post-header bytes.
                conn.sendall(handshake + websocket_server_frame(hello))
                identify = receive_client_json(conn)
                secret = base64.b64encode(hashlib.sha256(b"test-passwordtest-salt").digest()).decode("ascii")
                expected_auth = base64.b64encode(
                    hashlib.sha256((secret + "test-challenge").encode("utf-8")).digest()
                ).decode("ascii")
                assert identify["op"] == 1
                assert identify["d"]["authentication"] == expected_auth
                conn.sendall(websocket_server_frame({"op": 2, "d": {"negotiatedRpcVersion": 1}}))
                request = receive_client_json(conn)
                assert request["d"]["requestType"] == "GetVersion"
                conn.sendall(
                    websocket_server_frame(
                        {
                            "op": 7,
                            "d": {
                                "requestType": "GetVersion",
                                "requestId": request["d"]["requestId"],
                                "requestStatus": {"result": True, "code": 100},
                                "responseData": {"obsVersion": "32.1.0"},
                            },
                        }
                    )
                )
            server_result.put(None)
        except Exception as error:
            server_result.put(error)

    fake_thread = threading.Thread(target=run_fake_obs, daemon=True)
    fake_thread.start()
    obs_ws = ObsWebSocket(f"ws://127.0.0.1:{test_port}", "test-password")
    try:
        obs_ws.connect()
        response = obs_ws.call("GetVersion")
        assert response["obsVersion"] == "32.1.0"
        fake_thread.join(2.0)
        error = server_result.get(timeout=1.0)
        if error:
            raise error
    finally:
        obs_ws.close()
        test_server.close()
    print("PASS: OBS WebSocket handshake, authentication, coalesced greeting, and requests")

    class FakeProfileController(ObsController):
        def __init__(self) -> None:
            super().__init__("ws://unused", "", captured.append)
            self.available_requests = {"SetRecordDirectory"}
            self.obs_version = (32, 1, 0)
            self.profile_exists = False
            self.current_profile = "Untitled"
            self.video: dict[str, Any] = {}
            self.parameters: dict[tuple[str, str], str] = {}

        def _call(self, request_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
            data = data or {}
            if request_type == "GetRecordStatus":
                return {"outputActive": False}
            if request_type == "GetProfileList":
                return {
                    "currentProfileName": self.current_profile,
                    "profiles": [PROFILE_NAME] if self.profile_exists else ["Untitled"],
                }
            if request_type == "CreateProfile":
                self.profile_exists = True
                self.current_profile = data["profileName"]
            elif request_type == "SetCurrentProfile":
                self.current_profile = data["profileName"]
            elif request_type == "SetVideoSettings":
                self.video = dict(data)
            elif request_type == "GetVideoSettings":
                return dict(self.video)
            elif request_type == "SetProfileParameter":
                self.parameters[(data["parameterCategory"], data["parameterName"])] = data["parameterValue"]
            return {}

    import tempfile

    with tempfile.TemporaryDirectory(prefix="r6-obs-sync-") as temp_directory:
        fake_profile = FakeProfileController()
        fake_profile.capture_directory = Path(temp_directory) / "captures"
        video = fake_profile.install_profile()
        assert video["outputWidth"] == 1920 and video["fpsNumerator"] == 60
        assert fake_profile.current_profile == PROFILE_NAME
        assert fake_profile.parameters[("SimpleOutput", "RecFormat2")] == "hybrid_mp4"
        assert fake_profile.parameters[("Hotkeys", "OBSBasic.StartRecording")] == NO_HOTKEY_BINDINGS
        assert fake_profile.parameters[("Hotkeys", "OBSBasic.StopRecording")] == NO_HOTKEY_BINDINGS
    print("PASS: standardized OBS profile is 1080p60 Hybrid MP4 without conflicting OBS hotkeys")

    class FakeDelayedStartController(ObsController):
        def __init__(self) -> None:
            super().__init__("ws://unused", "", captured.append)
            self.status_checks = 0

        def _call(self, request_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
            if request_type == "StartRecord":
                return {}
            if request_type == "GetRecordStatus":
                self.status_checks += 1
                return {
                    "outputActive": self.status_checks >= 3,
                    "outputDuration": 0,
                }
            raise AssertionError(f"Unexpected delayed-start request: {request_type}")

    delayed_start = FakeDelayedStartController()
    delayed_result = delayed_start.start_recording()
    assert delayed_start.status_checks == 3
    assert delayed_result["obsDurationMs"] == 0
    print("PASS: OBS startup verification tolerates delayed outputActive state")

    class FakeTimingController:
        def __init__(self, capture_directory: Path):
            self.capture_directory = capture_directory
            self.active = False
            self.started_at: float | None = None
            self.stopped_at: float | None = None

        def install_profile(self) -> dict[str, Any]:
            return {"outputWidth": 1920, "outputHeight": 1080, "fpsNumerator": 60}

        def prepare_session(self, session_id: str) -> None:
            assert session_id

        def start_recording(self) -> dict[str, Any]:
            self.started_at = time.time()
            self.active = True
            return {
                "requestedAt": self.started_at,
                "acknowledgedAt": self.started_at,
                "obsDurationMs": 0,
            }

        def stop_recording(self) -> dict[str, Any]:
            self.stopped_at = time.time()
            self.active = False
            assert self.started_at is not None
            return {
                "requestedAt": self.stopped_at,
                "acknowledgedAt": self.stopped_at,
                "obsDurationMs": round((self.stopped_at - self.started_at) * 1000),
                "outputPath": str(self.capture_directory / "fake.mp4"),
                "wasActive": True,
            }

        def recording_status(self) -> dict[str, Any]:
            return {"outputActive": self.active}

    original_start_lead = START_LEAD_SECONDS
    original_stop_lead = STOP_LEAD_SECONDS
    START_LEAD_SECONDS = 0.35
    STOP_LEAD_SECONDS = 0.25
    timing_coordinator = SyncCoordinator("127.0.0.1", 0, "TIMING", captured.append)
    timing_port = timing_coordinator.start()
    timing_clients: list[SyncClient] = []
    timing_controllers: list[FakeTimingController] = []
    timing_events: list[queue.Queue[tuple[str, dict[str, Any]]]] = []
    try:
        with tempfile.TemporaryDirectory(prefix="r6-obs-timing-") as temp_directory:
            for index, name in enumerate(("Listener", "Runner")):
                controller = FakeTimingController(Path(temp_directory) / str(index))
                events: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
                client = SyncClient(name, "TIMING", controller, captured.append, lambda kind, data, q=events: q.put((kind, data)))
                client.connect("127.0.0.1", timing_port)
                timing_controllers.append(controller)
                timing_events.append(events)
                timing_clients.append(client)

            sync_deadline = time.time() + 3.0
            while time.time() < sync_deadline and any(len(client._sync_samples) < 5 for client in timing_clients):
                time.sleep(0.025)
            assert all(len(client._sync_samples) >= 5 for client in timing_clients)
            for client in timing_clients:
                client.set_ready()

            recording_deadline = time.time() + 2.0
            while time.time() < recording_deadline and any(controller.started_at is None for controller in timing_controllers):
                time.sleep(0.01)
            assert all(controller.started_at is not None for controller in timing_controllers)
            assert abs(timing_controllers[0].started_at - timing_controllers[1].started_at) < 0.030

            timing_clients[0].request_stop()
            stopped_deadline = time.time() + 2.0
            while time.time() < stopped_deadline and any(controller.stopped_at is None for controller in timing_controllers):
                time.sleep(0.01)
            assert all(controller.stopped_at is not None for controller in timing_controllers)
            assert abs(timing_controllers[0].stopped_at - timing_controllers[1].stopped_at) < 0.030
    finally:
        for client in timing_clients:
            client.close()
        timing_coordinator.close()
        START_LEAD_SECONDS = original_start_lead
        STOP_LEAD_SECONDS = original_stop_lead
    print("PASS: two clients execute shared future start and stop boundaries within 30 ms")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true", help="run dependency-free protocol checks")
    parser.add_argument(
        "--capture-directory",
        type=Path,
        help="directory for raw recordings and synchronization sidecars (default: Studio media\\raw)",
    )
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    try:
        SyncApp(args.capture_directory or default_capture_directory()).run()
    except ModuleNotFoundError as error:
        raise SystemExit(f"Tkinter is required for the graphical interface: {error}") from error


if __name__ == "__main__":
    main()
