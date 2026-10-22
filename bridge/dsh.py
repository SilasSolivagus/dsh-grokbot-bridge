"""HTTP client for the DSH Kanban API used by the M1.5 bridge.

The bridge talks to a locally running DSH Kanban API (for example
``http://127.0.0.1:61849/kanban-api``) with a builder agent token. Every
request carries ``Authorization: Bearer <token>``; request bodies are JSON.

Endpoints (``base_url`` already includes the ``/kanban-api`` prefix)::

    GET   /bootstrap                       -> {projects, tasks}
    GET   /tasks/{id}                      -> {task, comments, activity, attachments}
    PATCH /tasks/{id}  {version, status}   -> task
    POST  /tasks/{id}/comments {body}      -> comment

DSH remains authoritative. This client does not retry, cache, interpret task
state, or decide whether a transition is allowed; it forwards one request and
returns the decoded JSON object. A non-2xx response, an unreachable server, or
a success response that is not a JSON object raises :class:`DshError`.

Caller errors (a blank id, a non-positive version, an empty comment, a bad
base URL) raise :class:`ValueError` before any network request is made. The
token is never included in error messages or ``repr`` output.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

__all__ = ["DshClient", "DshError"]

DEFAULT_TIMEOUT = 30.0


class DshError(Exception):
    """Raised when a DSH request fails.

    ``status`` is the HTTP status code for an HTTP error response, or ``None``
    when the request never produced one (connection refused, timeout, bad
    URL). ``text`` is the decoded response body, or the transport reason.
    """

    def __init__(
        self,
        status: Optional[int],
        text: str,
        method: Optional[str] = None,
        url: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> None:
        self.status = status
        self.text = text
        self.method = method
        self.url = url
        self.reason = reason

        request = method or "DSH request"
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
    """Validate a caller-provided string and return it stripped."""
    if not isinstance(value, str):
        raise ValueError(
            "{} must be a string, got {}".format(label, type(value).__name__)
        )
    text = value.strip()
    if not text:
        raise ValueError("{} must be a non-empty string".format(label))
    return text


def _require_version(value: Any) -> int:
    """Validate an optimistic-concurrency task version."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(
            "version must be a positive integer, got {!r}".format(value)
        )
    return value


class DshClient:
    """Minimal DSH Kanban HTTP client.

    ``base_url`` is the API root including the ``/kanban-api`` suffix;
    trailing slashes are ignored. ``token`` is the builder agent token.
    ``timeout`` applies to each individual request.
    """

    def __init__(
        self,
        base_url: Any,
        token: Any,
        timeout: Any = DEFAULT_TIMEOUT,
    ) -> None:
        base = _require_text(base_url, "base_url").rstrip("/")
        parts = urllib.parse.urlsplit(base)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError(
                "base_url must be an absolute http(s) URL, got {!r}".format(
                    base_url
                )
            )
        if parts.query or parts.fragment:
            raise ValueError(
                "base_url must not contain a query or fragment"
            )
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or timeout <= 0
        ):
            raise ValueError("timeout must be a positive number")

        self.base_url = base
        self.token = _require_text(token, "token")
        self.timeout = float(timeout)

    def __repr__(self) -> str:
        return "DshClient(base_url={!r})".format(self.base_url)

    # -- public API -----------------------------------------------------

    def bootstrap(self) -> Dict[str, Any]:
        """Return the projects and tasks visible to this agent."""
        return self._request("GET", "/bootstrap")

    def get_task(self, task_id: Any) -> Dict[str, Any]:
        """Return ``{task, comments, activity, attachments}`` for one task."""
        return self._request("GET", "/tasks/" + self._task_id(task_id))

    def update_status(
        self, task_id: Any, version: Any, status: Any
    ) -> Dict[str, Any]:
        """Patch a task's status using optimistic concurrency."""
        task_id = self._task_id(task_id)
        payload = {
            "version": _require_version(version),
            "status": _require_text(status, "status"),
        }
        return self._request("PATCH", "/tasks/" + task_id, payload)

    def comment(self, task_id: Any, body: Any) -> Dict[str, Any]:
        """Add a comment authored by the token's agent; return the comment."""
        task_id = self._task_id(task_id)
        payload = {"body": _require_text(body, "comment body")}
        return self._request(
            "POST", "/tasks/{}/comments".format(task_id), payload
        )

    # -- internals ------------------------------------------------------

    @staticmethod
    def _task_id(task_id: Any) -> str:
        """Validate and percent-encode a task id for use in a URL path."""
        return urllib.parse.quote(_require_text(task_id, "task id"), safe="")

    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = self.base_url + path
        data = None
        headers = {
            "Authorization": "Bearer " + self.token,
            "Accept": "application/json",
        }
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(
            url, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout
            ) as response:
                status = response.getcode()
                raw = response.read()
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
            except (OSError, http.client.HTTPException):
                raw = b""
            raise DshError(
                exc.code, _decode(raw), method=method, url=url
            ) from None
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            raise DshError(
                None,
                str(reason) if reason is not None else str(exc),
                method=method,
                url=url,
            ) from None
        except (http.client.HTTPException, OSError) as exc:
            raise DshError(
                None, str(exc), method=method, url=url
            ) from None

        text = _decode(raw)
        if not text.strip():
            raise DshError(
                status,
                text,
                method=method,
                url=url,
                reason="empty response body",
            )
        try:
            parsed = json.loads(text)
        except ValueError:
            raise DshError(
                status,
                text,
                method=method,
                url=url,
                reason="response is not valid JSON",
            ) from None
        if not isinstance(parsed, dict):
            raise DshError(
                status,
                text,
                method=method,
                url=url,
                reason="response is not a JSON object",
            )
        return parsed
