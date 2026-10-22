"""HTTP long-poll relay transport for the M1.5 task bus.

The M1.5b relay is a self-hosted mailbox. One numeric channel carries one task
thread, so the channel id is the transport reference:

* ``open`` posts the validated request envelope to
  ``POST /v1/channels/<channel>/messages`` and returns the channel id.
* ``replies`` long-polls
  ``GET /v1/channels/<channel>/messages?after=<seq>&wait=<seconds>`` and
  returns the envelope objects from ``messages``. The relay numbers every
  message with a monotonic ``seq``, so the next cursor is the response's
  ``next`` integer. An empty poll returns the caller's ``after`` unchanged,
  which keeps a stored cursor stable.
* ``close`` is a no-op: relay channels are not task issues and the server has
  no close operation.

Requests carry ``Authorization: Bearer <token>`` and JSON bodies. A non-2xx
response, a network failure, and a malformed JSON document or response shape
raise :class:`RelayError`, a
:class:`~bridge.transport.TransportError`.

The transport asks the relay to hold an empty poll for one second less than
the HTTP timeout, capped at the relay's 25 second maximum, so a normal empty
poll returns before the socket times out.
"""

from __future__ import annotations

import http.client
import json
import math
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple, Union

from bridge.transport import Transport, TransportError

__all__ = ["RelayError", "RelayTransport"]

_MAX_WAIT = 25
_WAIT_MARGIN = 1
_MAX_DETAIL = 200


class RelayError(TransportError):
    """Raised when the relay cannot complete a transport operation."""


def _text(value: Any, label: str) -> str:
    """Validate a caller-provided string and return it stripped."""
    if not isinstance(value, str):
        raise ValueError(
            "{} must be a string, got {}".format(
                label, type(value).__name__
            )
        )
    text = value.strip()
    if not text:
        raise ValueError("{} must be a non-empty string".format(label))
    return text


def _base_url(value: Any) -> str:
    """Validate an absolute http(s) base URL without a trailing slash."""
    text = _text(value, "base_url")
    if any(char.isspace() for char in text):
        raise ValueError("base_url must not contain whitespace")
    text = text.rstrip("/")
    parts = urllib.parse.urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("base_url must be an absolute http(s) URL")
    if parts.query or parts.fragment:
        raise ValueError("base_url must not contain a query or fragment")
    return text


def _channel(value: Any, label: str = "channel") -> str:
    """Validate a positive numeric relay channel id and canonicalize it."""
    text = _text(value, label)
    if not text.isascii() or not text.isdigit():
        raise ValueError(
            "{} must be a positive numeric channel id".format(label)
        )
    number = int(text)
    if number <= 0:
        raise ValueError(
            "{} must be a positive numeric channel id".format(label)
        )
    return str(number)


def _cursor(value: Any, label: str = "after") -> Optional[int]:
    """Return a non-negative integer cursor, accepting numeric strings."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(
            "{} must be a non-negative integer cursor".format(label)
        )
    if isinstance(value, int):
        number = value
    elif isinstance(value, str):
        text = value.strip()
        if not text.isascii() or not text.isdigit():
            raise ValueError(
                "{} must be a non-negative integer cursor".format(label)
            )
        number = int(text)
    else:
        raise ValueError(
            "{} must be a non-negative integer cursor".format(label)
        )
    if number < 0:
        raise ValueError(
            "{} must be a non-negative integer cursor".format(label)
        )
    return number


def _timeout(value: Any) -> float:
    """Validate a positive, finite timeout in seconds."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timeout must be a positive number of seconds")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("timeout must be a positive number of seconds")
    return number


def _token(value: Any) -> str:
    """Validate a bearer token that is safe to place in a header."""
    text = _text(value, "token")
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise ValueError("token must not contain control characters")
    return text


def _wait_seconds(timeout: float) -> int:
    """Return the relay long-poll wait that fits inside the HTTP timeout."""
    return max(0, min(_MAX_WAIT, int(timeout) - _WAIT_MARGIN))


def _json_object(raw: bytes, source: str) -> Dict[str, Any]:
    """Decode one JSON object response or raise :class:`RelayError`."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RelayError(
            "{} returned invalid UTF-8: {}".format(source, exc)
        ) from None
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise RelayError(
            "{} returned invalid JSON: {}".format(source, exc)
        ) from None
    if not isinstance(document, dict):
        raise RelayError(
            "{} returned {}, expected a JSON object".format(
                source, type(document).__name__
            )
        )
    return document


def _seq_field(
    document: Dict[str, Any], field: str, source: str
) -> int:
    """Read a non-negative integer sequence field from a response."""
    if field not in document:
        raise RelayError("{} is missing {!r}".format(source, field))
    value = document[field]
    if isinstance(value, bool) or not isinstance(value, int):
        raise RelayError(
            "{} {!r} must be an integer, got {}".format(
                source, field, type(value).__name__
            )
        )
    if value < 0:
        raise RelayError(
            "{} {!r} must not be negative".format(source, field)
        )
    return value


class RelayTransport(Transport):
    """Transport backed by one self-hosted relay channel.

    ``base_url`` is the relay origin (an optional path prefix is kept),
    ``channel`` is the positive numeric channel id, and ``token`` is the
    channel token sent as a bearer token. ``timeout`` bounds each HTTP request
    and also determines how long the relay may hold an empty poll.
    """

    def __init__(
        self,
        base_url: Any,
        channel: Any,
        token: Any,
        timeout: Any = 30.0,
    ) -> None:
        self.base_url = _base_url(base_url)
        self.channel = _channel(channel)
        self.timeout = _timeout(timeout)
        self._token = _token(token)

    def __repr__(self) -> str:
        return "RelayTransport(base_url={!r}, channel={!r})".format(
            self.base_url, self.channel
        )

    # -- public API -----------------------------------------------------

    def open(self, request: Dict[str, Any]) -> str:
        """Post the request envelope and return the channel reference."""
        if not isinstance(request, dict):
            raise ValueError("request must be a dict")
        try:
            data = json.dumps(
                request, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "request must be JSON-serializable: {}".format(exc)
            ) from None

        source = self._source("POST", self.channel)
        raw = self._request(
            "POST", self._messages_url(self.channel), data=data
        )
        document = _json_object(raw, "relay " + source)
        _seq_field(document, "seq", "relay " + source)
        return self.channel

    def replies(
        self,
        ref: str,
        after: Optional[Union[int, str]] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[Union[int, str]]]:
        """Long-poll for reply envelopes after ``after`` and return them."""
        channel = _channel(ref, "channel reference")
        cursor = _cursor(after)
        query_after = 0 if cursor is None else cursor
        source = self._source("GET", channel)
        url = self._messages_url(
            channel,
            after=query_after,
            wait=_wait_seconds(self.timeout),
        )
        raw = self._request("GET", url)
        document = _json_object(raw, "relay " + source)

        raw_messages = document.get("messages")
        if not isinstance(raw_messages, list):
            raise RelayError(
                "relay {} response has no messages list".format(source)
            )
        next_cursor = _seq_field(document, "next", "relay " + source)

        envelopes: List[Dict[str, Any]] = []
        for index, item in enumerate(raw_messages):
            location = "relay {} messages[{}]".format(source, index)
            if not isinstance(item, dict):
                raise RelayError("{} is not an object".format(location))
            envelope = item.get("envelope")
            if not isinstance(envelope, dict):
                raise RelayError(
                    "{}.envelope is not an object".format(location)
                )
            _seq_field(item, "seq", location)
            envelopes.append(envelope)

        if not envelopes:
            return [], after
        return envelopes, next_cursor

    def close(self, ref: str) -> None:
        """Validate the reference; relay channels need no cleanup."""
        _channel(ref, "channel reference")

    # -- internals ------------------------------------------------------

    def _messages_url(
        self,
        channel: str,
        after: Optional[int] = None,
        wait: Optional[int] = None,
    ) -> str:
        """Build the messages URL for one channel."""
        url = "{}/v1/channels/{}/messages".format(self.base_url, channel)
        params = {}
        if after is not None:
            params["after"] = str(after)
        if wait is not None:
            params["wait"] = str(wait)
        if params:
            url = "{}?{}".format(url, urllib.parse.urlencode(params))
        return url

    def _source(self, method: str, channel: str) -> str:
        """Return a short request label for diagnostics."""
        return "{} /v1/channels/{}/messages".format(method, channel)

    def _request(
        self, method: str, url: str, data: Optional[bytes] = None
    ) -> bytes:
        """Perform one JSON request and return the raw response body."""
        headers = {
            "Authorization": "Bearer {}".format(self._token),
            "Accept": "application/json",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            url, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout
            ) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise self._http_error(method, url, exc) from None
        except urllib.error.URLError as exc:
            raise RelayError(
                "relay {} {} failed: {}".format(method, url, exc.reason)
            ) from None
        except (socket.timeout, TimeoutError):
            raise RelayError(
                "relay {} {} timed out after {:g}s".format(
                    method, url, self.timeout
                )
            ) from None
        except http.client.HTTPException as exc:
            raise RelayError(
                "relay {} {} returned an invalid HTTP response: {}".format(
                    method, url, exc
                )
            ) from None
        except OSError as exc:
            raise RelayError(
                "relay {} {} failed: {}".format(method, url, exc)
            ) from None
        except ValueError as exc:
            raise RelayError(
                "relay {} {} failed: {}".format(method, url, exc)
            ) from None

    @staticmethod
    def _http_error(method: str, url: str, exc: Any) -> RelayError:
        """Translate an ``HTTPError`` without leaking the bearer token."""
        status = exc.code
        detail = ""
        try:
            raw = exc.read()
        except Exception:  # the error body is best-effort diagnostics
            raw = b""
        finally:
            exc.close()
        if raw:
            try:
                detail = raw.decode("utf-8").strip()
            except UnicodeDecodeError:
                detail = ""
        message = "relay {} {} failed with HTTP {}".format(
            method, url, status
        )
        if detail:
            message = "{}: {}".format(message, detail[:_MAX_DETAIL])
        return RelayError(message)
