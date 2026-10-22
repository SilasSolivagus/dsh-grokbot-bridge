"""HTTP client for the Grok routine webhook used by the M1.5 bridge.

The bridge wakes an already configured Grok routine by POSTing one JSON object
to the routine webhook URL with ``Authorization: Bearer <key>``::

    POST <webhook_url>  {task_request}
    ->  {"runUuid": "..."}

The webhook only acknowledges that a run was accepted. There is **no
completion callback**: the orchestrator must poll the GitHub issue for replies
and treat a missing reply as unfinished work. ``wake`` sends exactly one
request; it does not retry, poll, or interpret the task request.

A missing or invalid webhook URL/key, a non-2xx response, malformed JSON, or a
response without a non-empty ``runUuid`` raises :class:`RoutineError`. The key
is never included in error messages or ``repr`` output, including when a
server echoes it back inside an error body.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

__all__ = ["RoutineClient", "RoutineError"]

DEFAULT_TIMEOUT = 15.0
REDACTED = "[redacted]"


class RoutineError(Exception):
    """Raised when the routine webhook cannot be called or rejects a wake.

    ``status`` is the HTTP status code for an HTTP error response, or ``None``
    when the request never produced one (bad config, connection refused,
    timeout). ``text`` is the decoded response body, or the transport reason.
    ``method`` and ``url`` identify the failed request when one was made.
    ``reason`` is a short description of what went wrong.
    """

    def __init__(
        self,
        status: Optional[int] = None,
        text: str = "",
        method: Optional[str] = None,
        url: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> None:
        self.status = status
        self.text = text
        self.method = method
        self.url = url
        self.reason = reason

        request = method or "routine wake"
        if url:
            request = "{} {}".format(request, url)
        if status is None:
            summary = "{} failed".format(request)
        else:
            summary = "{} failed with HTTP {}".format(request, status)
        if reason:
            summary = "{}: {}".format(summary, reason)
        if text.strip():
            summary = "{} - {}".format(summary, text.strip())
        super().__init__(summary)


def _decode(raw: bytes) -> str:
    """Decode a response body without ever failing on bad bytes."""
    return raw.decode("utf-8", "replace")


def _require_text(value: Any, label: str) -> str:
    """Validate a caller-provided string and return it stripped.

    The rejected value is deliberately not echoed: it may be a secret.
    """
    if not isinstance(value, str):
        raise RoutineError(
            reason="{} must be a string, got {}".format(
                label, type(value).__name__
            )
        )
    text = value.strip()
    if not text:
        raise RoutineError(
            reason="{} must be a non-empty string".format(label)
        )
    return text


def _require_key(value: Any) -> str:
    """Validate a bearer key that is safe to send in a header."""
    key = _require_text(value, "key")
    if any(
        ord(character) < 0x21 or ord(character) > 0x7E
        for character in key
    ):
        raise RoutineError(
            reason="key must be printable ASCII without spaces"
        )
    return key


def _require_webhook_url(value: Any) -> str:
    """Validate and return the absolute http(s) routine webhook URL."""
    url = _require_text(value, "webhook_url")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise RoutineError(
            reason="webhook_url must be an absolute http(s) URL"
        )
    if parts.fragment:
        raise RoutineError(
            reason="webhook_url must not contain a fragment"
        )
    return url


def _require_timeout(value: Any) -> float:
    """Validate the per-request timeout in seconds."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or value <= 0
    ):
        raise RoutineError(reason="timeout must be a positive number")
    return float(value)


class RoutineClient:
    """Minimal Grok routine webhook client.

    ``webhook_url`` is the routine webhook endpoint, ``key`` is its bearer
    token, and ``timeout`` applies to the single POST made by :meth:`wake`.
    """

    def __init__(
        self,
        webhook_url: Any,
        key: Any,
        timeout: Any = DEFAULT_TIMEOUT,
    ) -> None:
        self.webhook_url = _require_webhook_url(webhook_url)
        self.key = _require_key(key)
        self.timeout = _require_timeout(timeout)

    def __repr__(self) -> str:
        return "RoutineClient(webhook_url={!r})".format(
            self._scrub(self.webhook_url)
        )

    # -- public API -----------------------------------------------------

    def wake(self, request: Dict[str, Any]) -> Dict[str, str]:
        """POST ``request`` to the routine webhook and return its run UUID.

        The returned mapping is always ``{"runUuid": <non-empty str>}``; any
        other fields in the webhook response are ignored because the bridge
        only needs the run id for diagnostics.
        """
        if not isinstance(request, dict):
            raise RoutineError(
                reason="request must be a dict, got {}".format(
                    type(request).__name__
                )
            )
        try:
            data = json.dumps(request, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise RoutineError(
                reason="request is not JSON serializable: {}".format(
                    self._scrub(str(exc))
                )
            ) from None

        headers = {
            "Authorization": "Bearer " + self.key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            http_request = urllib.request.Request(
                self.webhook_url,
                data=data,
                headers=headers,
                method="POST",
            )
        except ValueError as exc:
            raise self._error(
                None, self._scrub(str(exc)), reason="invalid webhook_url"
            ) from None

        try:
            with urllib.request.urlopen(
                http_request, timeout=self.timeout
            ) as response:
                status = response.getcode()
                raw = response.read()
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
            except (OSError, http.client.HTTPException):
                raw = b""
            raise self._error(exc.code, _decode(raw)) from None
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            raise self._error(
                None,
                self._scrub(
                    str(reason) if reason is not None else str(exc)
                ),
            ) from None
        except (http.client.HTTPException, OSError, ValueError) as exc:
            raise self._error(
                None, self._scrub(str(exc))
            ) from None

        text = _decode(raw)
        if not text.strip():
            raise self._error(
                status, text, reason="empty response body"
            )
        try:
            parsed = json.loads(text)
        except ValueError:
            raise self._error(
                status, text, reason="response is not valid JSON"
            ) from None
        if not isinstance(parsed, dict):
            raise self._error(
                status, text, reason="response is not a JSON object"
            )

        run_uuid = parsed.get("runUuid")
        if not isinstance(run_uuid, str) or not run_uuid.strip():
            raise self._error(
                status,
                text,
                reason="response is missing a non-empty runUuid",
            )
        return {"runUuid": run_uuid.strip()}

    # -- internals ------------------------------------------------------

    def _scrub(self, text: str) -> str:
        """Remove the configured key from text destined for an error."""
        if not text or not self.key:
            return text
        return text.replace(self.key, REDACTED)

    def _error(
        self,
        status: Optional[int],
        text: str,
        reason: Optional[str] = None,
    ) -> RoutineError:
        """Build a RoutineError with the key scrubbed from every field."""
        return RoutineError(
            status=status,
            text=self._scrub(text),
            method="POST",
            url=self._scrub(self.webhook_url),
            reason=self._scrub(reason) if reason else None,
        )
