"""Self-hosted HTTP long-poll relay for the M1.5 task bus.

One relay process serves many bot channels. A channel is created with the
shared admin token; each channel then has its own bearer token and an
append-only in-memory message queue. Bots post envelopes and long-poll for
messages newer than a cursor.

The wire API is::

    POST /v1/channels                     (admin token) -> channel + token + url
    POST /v1/channels/<id>/messages       (channel token) -> {"seq": int}
    GET  /v1/channels/<id>/messages?after=<seq>&wait=<seconds>
                                          (channel token) -> {"messages", "next"}

The relay is deliberately small and ephemeral: state lives in memory, there is
no TLS (a reverse proxy terminates it), no persistence, no GitHub, and no DSH
dependency. Channel ids are positive integers rendered as strings, and the
queues are guarded by a single :class:`threading.Condition`, which also wakes
long-polling readers when a message arrives or the server stops.
"""

from __future__ import annotations

import hmac
import json
import math
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

__all__ = ["RelayServer"]

MAX_PAYLOAD_BYTES = 64 * 1024
MAX_WAIT_SECONDS = 25.0

Envelope = Dict[str, Any]
_Message = Tuple[int, Envelope]


def _token_matches(candidate: str, expected: str) -> bool:
    """Compare tokens in constant time, including non-ASCII input."""
    return hmac.compare_digest(
        candidate.encode("utf-8"), expected.encode("utf-8")
    )


def _parse_channel_id(text: str) -> Optional[str]:
    """Return a canonical positive channel id, or ``None`` when invalid."""
    if not text.isdigit():
        return None
    value = int(text)
    if value <= 0 or str(value) != text:
        return None
    return text


class _Channel:
    """One bot's queue. All mutable state is guarded by the server lock."""

    __slots__ = ("channel_id", "token", "next_seq", "messages")

    def __init__(self, channel_id: str, token: str) -> None:
        self.channel_id = channel_id
        self.token = token
        self.next_seq = 0
        self.messages: List[_Message] = []


class _RelayRequestHandler(BaseHTTPRequestHandler):
    """HTTP surface for :class:`RelayServer`."""

    protocol_version = "HTTP/1.0"
    server_version = "dsh-relay/1"
    sys_version = ""

    # Keep test output and production logs free of per-request noise.
    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_PUT(self) -> None:
        self._dispatch()

    def do_PATCH(self) -> None:
        self._dispatch()

    def do_DELETE(self) -> None:
        self._dispatch()

    def do_HEAD(self) -> None:
        self._dispatch()

    def do_OPTIONS(self) -> None:
        self._dispatch()

    # -- routing ---------------------------------------------------------

    def _dispatch(self) -> None:
        try:
            self._route()
        except (BrokenPipeError, ConnectionResetError):
            # The client stopped waiting; nothing useful left to send.
            self.close_connection = True

    @property
    def _relay(self) -> "RelayServer":
        return self.server.relay  # type: ignore[attr-defined]

    def _route(self) -> None:
        parsed = urlsplit(self.path)
        parts = [part for part in parsed.path.split("/") if part]

        if parts[:2] != ["v1", "channels"]:
            self._send_error(404, "not found")
            return

        if len(parts) == 2:
            if self.command != "POST":
                self._send_error(
                    405, "method not allowed", {"Allow": "POST"}
                )
                return
            self._create_channel()
            return

        if len(parts) == 4 and parts[3] == "messages":
            channel_id = _parse_channel_id(parts[2])
            if self.command not in ("GET", "POST"):
                self._send_error(
                    405, "method not allowed", {"Allow": "GET, POST"}
                )
                return
            token = self._bearer_token()
            if token is None:
                self._send_error(
                    401, "unauthorized", {"WWW-Authenticate": "Bearer"}
                )
                return
            channel = self._relay._channel(channel_id) if channel_id else None
            if channel is None:
                self._send_error(404, "unknown channel")
                return
            if not _token_matches(token, channel.token):
                self._send_error(
                    401, "unauthorized", {"WWW-Authenticate": "Bearer"}
                )
                return
            if self.command == "POST":
                self._receive_message(channel)
            else:
                self._poll_messages(channel, parsed.query)
            return

        self._send_error(404, "not found")

    # -- endpoints -------------------------------------------------------

    def _create_channel(self) -> None:
        token = self._bearer_token()
        if token is None or not self._relay._authorize_admin(token):
            self._send_error(
                401, "unauthorized", {"WWW-Authenticate": "Bearer"}
            )
            return
        channel_id, channel_token = self._relay._create_channel()
        self._send_json(
            201,
            {
                "channel": channel_id,
                "token": channel_token,
                "url": "{}/v1/channels/{}".format(
                    self._relay.base_url, channel_id
                ),
            },
        )

    def _receive_message(self, channel: _Channel) -> None:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self._send_error(400, "Content-Length is required")
            return
        try:
            length = int(raw_length)
        except ValueError:
            self._send_error(400, "invalid Content-Length")
            return
        if length < 0:
            self._send_error(400, "invalid Content-Length")
            return
        if length > MAX_PAYLOAD_BYTES:
            # Do not read an oversized body; close instead of draining it.
            self.close_connection = True
            self._send_error(413, "payload too large")
            return

        raw = self.rfile.read(length)
        if len(raw) != length:
            self._send_error(400, "incomplete body")
            return
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._send_error(400, "body must be a JSON object")
            return
        if not isinstance(envelope, dict):
            self._send_error(400, "body must be a JSON object")
            return

        seq = self._relay._append_message(channel, envelope)
        self._send_json(201, {"seq": seq})

    def _poll_messages(self, channel: _Channel, query: str) -> None:
        params = parse_qs(query, keep_blank_values=True)
        try:
            after = _parse_after(params)
            wait = _parse_wait(params)
        except ValueError as exc:
            self._send_error(400, str(exc))
            return

        messages, next_seq = self._relay._poll_channel(channel, after, wait)
        self._send_json(
            200,
            {
                "messages": [
                    {"seq": seq, "envelope": envelope}
                    for seq, envelope in messages
                ],
                "next": next_seq,
            },
        )

    # -- request/response helpers ----------------------------------------

    def _bearer_token(self) -> Optional[str]:
        header = self.headers.get("Authorization", "")
        scheme, separator, value = header.partition(" ")
        if not separator or scheme.lower() != "bearer":
            return None
        token = value.strip()
        return token or None

    def _send_json(
        self,
        status: int,
        payload: Dict[str, Any],
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_error(
        self,
        status: int,
        message: str,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self._send_json(status, {"error": message}, headers)


def _parse_after(params: Dict[str, List[str]]) -> int:
    raw = params.get("after", ["0"])[-1]
    if not raw.isdigit():
        raise ValueError("after must be a non-negative integer")
    return int(raw)


def _parse_wait(params: Dict[str, List[str]]) -> float:
    raw = params.get("wait", ["0"])[-1]
    if not raw:
        raise ValueError("wait must be a non-negative number")
    try:
        value = float(raw)
    except ValueError:
        raise ValueError("wait must be a non-negative number") from None
    if not math.isfinite(value) or value < 0:
        raise ValueError("wait must be a non-negative number")
    return min(value, MAX_WAIT_SECONDS)


class _RelayHTTPServer(ThreadingHTTPServer):
    """Threading HTTP server with a back-reference to its relay."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self, server_address: Tuple[str, int], relay: "RelayServer"
    ) -> None:
        self.relay = relay
        super().__init__(server_address, _RelayRequestHandler)


class RelayServer:
    """In-memory relay server with an explicit start/stop lifecycle.

    ``RelayServer(admin_token)`` creates the object; :meth:`start` binds the
    socket (loopback port 0 by default), launches the serving thread, and
    returns the base URL. :meth:`stop` wakes any long-polls and releases the
    listener. Tests use ``start()``/``stop()`` per test.
    """

    payload_limit = MAX_PAYLOAD_BYTES
    max_wait_seconds = MAX_WAIT_SECONDS

    def __init__(
        self,
        admin_token: str,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        if not isinstance(admin_token, str) or not admin_token:
            raise ValueError("admin_token must be a non-empty string")
        self._admin_token = admin_token
        self._host = host
        self._port = port
        self._condition = threading.Condition()
        self._channels: Dict[str, _Channel] = {}
        self._next_channel_id = 1
        self._stopping = False
        self._httpd: Optional[_RelayHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._base_url: Optional[str] = None

    @property
    def base_url(self) -> str:
        """Return the bound base URL, for example ``http://127.0.0.1:54321``."""
        if self._base_url is None:
            raise RuntimeError("relay server is not running")
        return self._base_url

    def start(self) -> str:
        """Bind the listener, start serving in the background, return the URL."""
        with self._condition:
            if self._httpd is not None:
                raise RuntimeError("relay server is already running")
            httpd = _RelayHTTPServer((self._host, self._port), self)
            thread = threading.Thread(
                target=httpd.serve_forever,
                kwargs={"poll_interval": 0.05},
                name="relay-server",
                daemon=True,
            )
            self._stopping = False
            self._httpd = httpd
            self._thread = thread
            bound_host, bound_port = httpd.server_address[:2]
            self._base_url = "http://{}:{}".format(bound_host, bound_port)
            thread.start()
            return self._base_url

    def stop(self) -> None:
        """Stop serving and release the listener. Safe to call twice."""
        with self._condition:
            httpd = self._httpd
            thread = self._thread
            if httpd is None:
                return
            self._stopping = True
            self._condition.notify_all()

        httpd.shutdown()
        httpd.server_close()
        if thread is not None:
            thread.join(timeout=5.0)

        with self._condition:
            self._httpd = None
            self._thread = None
            self._base_url = None

    # -- state used by the HTTP handler ----------------------------------

    def _authorize_admin(self, token: str) -> bool:
        return _token_matches(token, self._admin_token)

    def _create_channel(self) -> Tuple[str, str]:
        """Allocate one channel and return ``(channel_id, channel_token)``."""
        with self._condition:
            channel_id = str(self._next_channel_id)
            self._next_channel_id += 1
            token = secrets.token_urlsafe(32)
            self._channels[channel_id] = _Channel(channel_id, token)
            return channel_id, token

    def _channel(self, channel_id: str) -> Optional[_Channel]:
        with self._condition:
            return self._channels.get(channel_id)

    def _append_message(self, channel: _Channel, envelope: Envelope) -> int:
        with self._condition:
            channel.next_seq += 1
            seq = channel.next_seq
            channel.messages.append((seq, envelope))
            self._condition.notify_all()
            return seq

    def _poll_channel(
        self, channel: _Channel, after: int, wait: float
    ) -> Tuple[List[_Message], int]:
        """Wait up to ``wait`` for messages newer than ``after``."""
        deadline = time.monotonic() + wait
        with self._condition:
            while True:
                pending = [
                    (seq, envelope)
                    for seq, envelope in channel.messages
                    if seq > after
                ]
                if pending:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._stopping:
                    break
                self._condition.wait(remaining)
            next_seq = pending[-1][0] if pending else after
            return pending, next_seq
