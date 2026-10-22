"""One poll cycle of the M1.5 task bus.

The orchestrator joins the injected pieces and owns every policy decision:

* which DSH tasks become a ``task_request`` (assigned to ``builder_agent``,
  still active, not mapped yet);
* which transport replies are allowed (allowlisted author, exactly one JSON
  object, valid envelope, matching card/task/agent);
* which DSH writes a reply causes, with DSH remaining authoritative;
* what is persisted and when, so a crash cannot repeat an applied reply.

All dependencies are injected. This module deliberately imports no DSH,
GitHub, or routine adapter module, so the same orchestrator can run over a
GitHub issue thread, an HTTP relay, or an in-memory fake. Replies may be GitHub
comments (``{id, body, author, createdAt}``) or raw relay envelopes; both are
normalized to one internal shape before validation.

Cycle contract:

1. ``dsh.bootstrap()``. A missing/mistyped ``projects`` or ``tasks`` list is
   fatal for the cycle (fail closed) and raises :class:`OrchestratorError`.
2. Every active task assigned to ``builder_agent`` that has no card mapping is
   opened once: one ``task_request`` -> one transport thread -> one routine
   wake -> one persisted card.
3. Every card mapping is polled. Accepted replies are applied in order and the
   state file is saved immediately after each applied reply. A reply that
   fails a DSH write is left unprocessed so a later cycle retries it; the
   cursor is not advanced past it.
4. A card is marked ``done`` only by an applied, validated ``task_complete``
   envelope (which the envelope module already requires to carry evidence).

Transport references and cursors are treated as opaque. The only assumption is
the state file's documented shape: a transport reference must be a positive
integer so it can be stored in the ``issue`` card field, and a comment id or
cursor must be storable as ``last_comment_id`` (both adapters supply integers).
Cursors are never cast; only the issue reference is normalized for the state
file. Relay replies have no per-message state id, so their cursor is persisted
only after the batch is fully handled.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from bridge.envelope import (
    ACK,
    BRIDGE,
    TASK_COMPLETE,
    TASK_REQUEST,
    TASK_UPDATE,
    EnvelopeError,
    message_key,
    parse_task_reply,
    parse_task_request,
)

__all__ = ["Orchestrator", "OrchestratorError"]

LOGGER = logging.getLogger(__name__)
LOGGER.addHandler(logging.NullHandler())

TERMINAL_STATUSES = ("done", "canceled", "cancelled")

_FENCED_JSON = re.compile(
    r"```[ \t]*json[ \t]*\r?\n(.*?)(?:\r?\n)?```",
    re.IGNORECASE | re.DOTALL,
)


class OrchestratorError(ValueError):
    """Raised when a cycle cannot start because DSH returned a bad shape."""


# ---------------------------------------------------------------------------
# Small validation helpers
# ---------------------------------------------------------------------------


def _text(value: Any, label: str) -> str:
    """Return a stripped non-empty string or raise :class:`ValueError`."""
    if not isinstance(value, str):
        raise ValueError(
            "{} must be a string, got {}".format(label, type(value).__name__)
        )
    text = value.strip()
    if not text:
        raise ValueError("{} must be a non-empty string".format(label))
    return text


def _optional_text(value: Any) -> str:
    """Return a stripped string, or ``""`` when the value is not a string."""
    return value.strip() if isinstance(value, str) else ""


def _login_set(values: Iterable[Any]) -> frozenset:
    """Normalize an allowed-login collection for case-insensitive lookup."""
    if isinstance(values, (str, bytes)):
        raise ValueError(
            "allowed_logins must be an iterable of login strings, not a string"
        )
    try:
        items = list(values)
    except TypeError as exc:
        raise ValueError("allowed_logins must be an iterable") from exc

    logins = set()
    for item in items:
        if not isinstance(item, str):
            raise ValueError(
                "allowed_logins entries must be strings, got {}".format(
                    type(item).__name__
                )
            )
        login = item.strip().lower()
        if not login:
            raise ValueError("allowed_logins entries must be non-empty")
        logins.add(login)
    return frozenset(logins)


def _author(value: Any) -> str:
    """Normalize a transport author (``str`` or ``{login: ...}``)."""
    if isinstance(value, dict):
        value = value.get("login")
    if not isinstance(value, str):
        return ""
    return value.strip().lower()


def _assignee(value: Any) -> str:
    """Return the agent id of a DSH assignee (``str`` or ``{id: ...}``)."""
    if isinstance(value, dict):
        value = value.get("id")
    if not isinstance(value, str):
        return ""
    return value.strip()


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_old(comment_id: Any, after: Any) -> bool:
    """Return whether ``comment_id`` is at or before the ``after`` cursor.

    Comparison is only possible when both values are the same primitive kind.
    Mixed or opaque values are left to the transport, whose contract already
    says replies arrive after ``after``.
    """
    if after is None:
        return False
    if _is_integer(comment_id) and _is_integer(after):
        return comment_id <= after
    if isinstance(comment_id, str) and isinstance(after, str):
        return comment_id <= after
    return comment_id == after


def _is_newer(candidate: Any, current: Any) -> bool:
    """Return whether a cursor moved forward without casting it."""
    if current is None:
        return True
    if _is_integer(candidate) and _is_integer(current):
        return candidate > current
    if isinstance(candidate, str) and isinstance(current, str):
        return candidate > current
    return candidate != current


def _issue_number(ref: Any) -> int:
    """Return the positive issue number carried by an opaque reference.

    ``GitHubTransport.open`` returns the issue number as a string while
    ``BridgeState`` stores a positive integer, so this is the one place where
    the bridge translates between the two. The value is not otherwise changed.
    """
    if _is_integer(ref) and ref >= 1:
        return ref
    if isinstance(ref, str):
        text = ref.strip()
        if text.isdigit():
            number = int(text)
            if number >= 1:
                return number
    raise OrchestratorError(
        "transport reference must be a positive issue number, got {!r}".format(ref)
    )


def _ref_for_issue(issue: Any) -> Optional[str]:
    """Return the transport reference for a persisted card issue."""
    if _is_integer(issue) and issue >= 1:
        return str(issue)
    if isinstance(issue, str) and issue.strip():
        return issue.strip()
    return None


def _detail_task(detail: Any) -> Dict[str, Any]:
    """Return the task object from a ``dsh.get_task`` payload."""
    if not isinstance(detail, dict):
        raise OrchestratorError(
            "dsh.get_task must return a JSON object, got {}".format(
                type(detail).__name__
            )
        )
    task = detail.get("task")
    if not isinstance(task, dict):
        raise OrchestratorError("dsh.get_task response has no task object")
    return task


def _task_version(task: Dict[str, Any]) -> int:
    """Return a validated optimistic-concurrency version."""
    version = task.get("version")
    if not _is_integer(version) or version < 1:
        raise OrchestratorError(
            "dsh task version must be a positive integer, got {!r}".format(
                version
            )
        )
    return version


# ---------------------------------------------------------------------------
# Reply body extraction
# ---------------------------------------------------------------------------


def extract_json_object(body: str) -> Dict[str, Any]:
    """Return exactly one JSON object from a reply body.

    A fenced ``json`` block wins when present. Otherwise the body may be one
    bare object or prose containing one object. Zero objects, multiple
    objects, and a JSON value that is not an object raise
    :class:`~bridge.envelope.EnvelopeError`, so an ambiguous comment is never
    interpreted.
    """
    if not isinstance(body, str):
        raise EnvelopeError("message body must be a string")

    fenced = _FENCED_JSON.findall(body)
    if len(fenced) > 1:
        raise EnvelopeError("message body contains multiple json fences")
    if fenced:
        try:
            parsed = json.loads(fenced[0].strip())
        except (ValueError, RecursionError) as exc:
            raise EnvelopeError(
                "json fence does not contain valid JSON: {}".format(exc)
            ) from None
        if not isinstance(parsed, dict):
            raise EnvelopeError("json fence must contain a JSON object")
        return parsed

    text = body.strip()
    try:
        parsed = json.loads(text)
    except (ValueError, RecursionError):
        parsed = None
    if parsed is not None:
        if not isinstance(parsed, dict):
            raise EnvelopeError("message must be a JSON object")
        return parsed

    decoder = json.JSONDecoder()
    objects: List[Dict[str, Any]] = []
    index = text.find("{")
    while index != -1:
        try:
            value, end = decoder.raw_decode(text, index)
        except (ValueError, RecursionError):
            index = text.find("{", index + 1)
            continue
        if isinstance(value, dict):
            objects.append(value)
            index = text.find("{", end)
        else:
            index = text.find("{", index + 1)

    if not objects:
        raise EnvelopeError("message body does not contain a JSON object")
    if len(objects) > 1:
        raise EnvelopeError("message body contains multiple JSON objects")
    return objects[0]


def _update_comment(envelope: Dict[str, Any]) -> str:
    """Build the DSH comment for an applied ``task_update``."""
    note = envelope.get("note") or ""
    evidence = envelope.get("evidence") or []
    lines: List[str] = []

    if envelope["state"] == "blocked":
        reason = note or "; ".join(evidence) or "no reason provided"
        lines.append("Blocked: " + reason)
        if note and evidence:
            lines.append("Evidence:")
            lines.extend("- " + item for item in evidence)
    else:
        if note:
            lines.append(note)
        if evidence:
            lines.append("Evidence:")
            lines.extend("- " + item for item in evidence)
        if not lines:
            lines.append("Status: in_progress.")

    return "\n".join(lines)


def _complete_comment(envelope: Dict[str, Any]) -> str:
    """Build the DSH comment for an applied ``task_complete``."""
    lines: List[str] = []
    note = envelope.get("note") or ""
    if note:
        lines.append(note)
    lines.append("Evidence:")
    lines.extend("- " + item for item in envelope.get("evidence") or [])
    return "\n".join(lines)


def _normalize_reply(message: Any) -> Dict[str, Any]:
    """Normalize a GitHub comment or a raw relay envelope.

    GitHub transports return ``{id, body, author, createdAt}``; relay
    transports return the envelope object itself. A relay envelope is
    identified by its ``bridge`` and ``type`` fields. Its author is the
    envelope's ``agent``, its id is the envelope's ``message_id`` (useful for
    logging and dedupe), and its body is the serialized envelope. Relay
    messages have no per-comment state id: their position is the transport
    cursor, so ``state_id`` is ``None``.
    """
    if not isinstance(message, dict):
        raise ValueError("transport message is not an object")

    if "bridge" in message and "type" in message:
        message_id = message.get("message_id")
        if message_id is None:
            raise ValueError("relay envelope has no message_id")
        try:
            body = json.dumps(message, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "relay envelope is not JSON serializable: {}".format(exc)
            ) from None
        return {
            "id": message_id,
            "body": body,
            "author": message.get("agent"),
            "state_id": None,
            "relay": True,
        }

    comment_id = message.get("id")
    if comment_id is None:
        raise ValueError("transport message has no id")
    return {
        "id": comment_id,
        "body": message.get("body"),
        "author": message.get("author"),
        "state_id": comment_id,
        "relay": False,
    }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class Orchestrator:
    """Drive one bridge cycle over injected clients and persisted state.

    ``dsh`` exposes ``bootstrap``/``get_task``/``update_status``/``comment``;
    ``transport`` exposes ``open``/``replies``/``close``; ``routine`` exposes
    ``wake``. ``state`` is a :class:`~bridge.state.BridgeState`.
    ``builder_agent`` is the configured actor id carried in every request and
    required on every reply. ``allowed_logins`` are the transport accounts
    allowed to author replies (matched case-insensitively). ``new_id`` creates
    a fresh envelope ``message_id`` and defaults to ``uuid4``.
    """

    def __init__(
        self,
        dsh: Any,
        transport: Any,
        routine: Any,
        state: Any,
        *,
        builder_agent: Any,
        allowed_logins: Iterable[str],
        new_id: Optional[Callable[[], str]] = None,
    ) -> None:
        self.dsh = dsh
        self.transport = transport
        self.routine = routine
        self.state = state
        self.builder_agent = _text(builder_agent, "builder_agent")
        self.allowed_logins = _login_set(allowed_logins)

        if new_id is None:
            new_id = lambda: str(uuid.uuid4())  # noqa: E731
        elif not callable(new_id):
            raise ValueError("new_id must be callable")
        self.new_id = new_id

        # Keep the exact transport reference for cards opened in this process
        # so a poll never depends on the state file's integer normalization.
        self._refs: Dict[str, str] = {}

    # -- public API -----------------------------------------------------

    def run_once(self) -> Dict[str, int]:
        """Run one poll cycle and return ``{opened, applied, skipped, errors}``.

        ``skipped`` counts reply messages deliberately ignored (wrong author,
        malformed body/envelope, duplicate, stale, or an already-terminal DSH
        task). ``errors`` counts failed adapter operations and malformed DSH
        payloads. Bootstrap failures raise instead of returning a partial
        cycle, because no write is safe without an authoritative board.
        """
        counts = {"opened": 0, "applied": 0, "skipped": 0, "errors": 0}

        board = self.dsh.bootstrap()
        self._check_board(board)

        identifiers: Dict[str, Optional[str]] = {}
        for task in board["tasks"]:
            if not isinstance(task, dict):
                continue
            task_id = _optional_text(task.get("id"))
            if not task_id:
                continue
            identifier = _optional_text(task.get("identifier"))
            identifiers[task_id] = identifier or None

        for task in board["tasks"]:
            self._open_task(task, counts)

        for task_id, card in self.state.cards.items():
            self._poll_card(task_id, card, identifiers, counts)

        return counts

    # -- bootstrap and card opening ------------------------------------

    @staticmethod
    def _check_board(board: Any) -> None:
        """Fail closed when DSH bootstrap is not the documented shape."""
        if not isinstance(board, dict):
            raise OrchestratorError(
                "dsh.bootstrap must return a JSON object, got {}".format(
                    type(board).__name__
                )
            )
        for key in ("projects", "tasks"):
            if key not in board:
                raise OrchestratorError(
                    "dsh.bootstrap response is missing {!r}".format(key)
                )
            if not isinstance(board[key], list):
                raise OrchestratorError(
                    "dsh.bootstrap {!r} must be a list, got {}".format(
                        key, type(board[key]).__name__
                    )
                )

    def _open_task(
        self,
        task: Any,
        counts: Dict[str, int],
    ) -> None:
        """Open one issue/webhook/card mapping for a new active task."""
        if not isinstance(task, dict):
            LOGGER.warning("skipping non-object DSH task")
            return

        task_id = _optional_text(task.get("id"))
        if not task_id:
            LOGGER.warning("skipping DSH task without an id")
            return

        if self.state.card_for_task(task_id) is not None:
            return

        if _assignee(task.get("assignee")) != self.builder_agent:
            return

        status = _optional_text(task.get("status")).lower()
        if status in TERMINAL_STATUSES:
            return
        if task.get("archivedAt") is not None:
            return

        try:
            detail = self.dsh.get_task(task_id)
            detail_task = _detail_task(detail)
        except Exception as exc:  # adapter or DSH shape failure
            LOGGER.error("cannot read DSH task %s: %s", task_id, exc)
            counts["errors"] += 1
            return

        try:
            identifier = _optional_text(
                detail_task.get("identifier")
            ) or _optional_text(task.get("identifier"))
            if not identifier:
                raise OrchestratorError("task has no identifier")
            title = (
                _optional_text(detail_task.get("title"))
                or _optional_text(task.get("title"))
                or identifier
            )
            description = _optional_text(
                detail_task.get("description")
            ) or _optional_text(task.get("description"))
            instructions = description or "Complete {}: {}.".format(
                identifier, title
            )

            message_id = self.new_id()
            if not isinstance(message_id, str) or not message_id.strip():
                raise OrchestratorError(
                    "new_id must return a non-empty string"
                )

            request = self._task_request(
                message_id=message_id.strip(),
                identifier=identifier,
                task_id=task_id,
                title=title,
                instructions=instructions,
                acceptance=detail_task.get("acceptance")
                if "acceptance" in detail_task
                else task.get("acceptance"),
            )
            # Validate our own output before it reaches the wire.
            parse_task_request(request)
        except (EnvelopeError, OrchestratorError) as exc:
            LOGGER.error("cannot build task_request for %s: %s", task_id, exc)
            counts["errors"] += 1
            return

        try:
            ref = self.transport.open(request)
            issue = _issue_number(ref)
        except Exception as exc:  # transport failure or unusable reference
            LOGGER.error("cannot open transport for %s: %s", task_id, exc)
            counts["errors"] += 1
            return

        try:
            result = self.routine.wake({"issue": ref, "request": request})
        except Exception as exc:  # adapter failure; do not retry blindly
            LOGGER.error("cannot wake routine for %s: %s", task_id, exc)
            counts["errors"] += 1
            return

        run_uuid = result.get("runUuid") if isinstance(result, dict) else None
        if not isinstance(run_uuid, str) or not run_uuid.strip():
            LOGGER.error("routine wake for %s returned no runUuid", task_id)
            counts["errors"] += 1
            return

        self._refs[task_id] = ref
        self.state.upsert_card(
            task_id,
            issue=issue,
            run_uuid=run_uuid.strip(),
            last_comment_id=0,
            last_status="submitted",
        )
        self.state.save()
        counts["opened"] += 1

    def _task_request(
        self,
        *,
        message_id: str,
        identifier: str,
        task_id: str,
        title: str,
        instructions: str,
        acceptance: Any,
    ) -> Dict[str, Any]:
        """Build a ``task_request`` using only the allowed envelope fields."""
        request: Dict[str, Any] = {
            "bridge": BRIDGE,
            "message_id": message_id,
            "type": TASK_REQUEST,
            "from": "dsh",
            "to": "grok",
            "agent": self.builder_agent,
            "card": identifier,
            "task_id": task_id,
            "title": title,
            "instructions": instructions,
        }
        items = self._acceptance(acceptance)
        if items:
            request["acceptance"] = items
        return request

    @staticmethod
    def _acceptance(value: Any) -> List[str]:
        """Return a clean optional acceptance list from a DSH task."""
        if not isinstance(value, list):
            return []
        items = []
        for item in value:
            text = _optional_text(item)
            if text:
                items.append(text)
        return items

    # -- reply polling --------------------------------------------------

    def _poll_card(
        self,
        task_id: str,
        card: Dict[str, Any],
        identifiers: Dict[str, Optional[str]],
        counts: Dict[str, int],
    ) -> None:
        """Poll one card mapping and apply every acceptable reply in order."""
        ref = self._refs.get(task_id)
        if ref is None:
            ref = _ref_for_issue(card.get("issue"))
        if ref is None:
            LOGGER.error("card %s has no usable issue reference", task_id)
            counts["errors"] += 1
            return

        after = card.get("last_comment_id", 0)
        try:
            messages, cursor = self.transport.replies(ref, after=after)
        except Exception as exc:  # transport failure should not kill the cycle
            LOGGER.error("cannot read replies for %s: %s", task_id, exc)
            counts["errors"] += 1
            return

        if not isinstance(messages, list):
            LOGGER.error("transport.replies for %s did not return a list", task_id)
            counts["errors"] += 1
            return

        failed = False
        for message in messages:
            outcome, payload = self._apply_message(
                task_id, card, message, identifiers
            )
            if outcome == "applied":
                state_id = payload["state_id"]
                fields = {"last_status": payload["status"]}
                if state_id is not None:
                    fields["last_comment_id"] = state_id
                self.state.upsert_card(task_id, **fields)
                self.state.mark_processed(payload["key"])
                self.state.save()
                if state_id is not None:
                    card["last_comment_id"] = state_id
                card["last_status"] = payload["status"]
                counts["applied"] += 1
                if payload.get("close"):
                    self._close_transport(ref, task_id, counts)
            elif outcome == "skipped":
                counts["skipped"] += 1
                LOGGER.info(
                    "skipping reply for %s: %s", task_id, payload
                )
            else:
                counts["errors"] += 1
                failed = True
                LOGGER.error("reply for %s failed: %s", task_id, payload)
                # Do not advance past a reply that may still be retried.
                break

        if not failed and cursor is not None and _is_newer(cursor, after):
            self.state.upsert_card(task_id, last_comment_id=cursor)
            self.state.save()

    def _apply_message(
        self,
        task_id: str,
        card: Dict[str, Any],
        message: Any,
        identifiers: Dict[str, Optional[str]],
    ) -> Tuple[str, Any]:
        """Classify and apply one transport message.

        Accepts a GitHub comment or a raw relay envelope. Returns
        ``("applied", payload)``, ``("skipped", reason)``, or
        ``("error", reason)``. State is never mutated here; the caller saves
        only after an ``applied`` result.
        """
        try:
            normalized = _normalize_reply(message)
        except ValueError as exc:
            return "skipped", str(exc)

        message_id = normalized["id"]
        if not normalized["relay"] and _is_old(
            message_id, card.get("last_comment_id")
        ):
            return "skipped", "comment is at or before last_comment_id"

        author = _author(normalized["author"])
        if author not in self.allowed_logins:
            return "skipped", "author {!r} is not allowlisted".format(author)

        body = normalized["body"]
        if not isinstance(body, str):
            return "skipped", "transport message has no text body"

        try:
            envelope = parse_task_reply(extract_json_object(body))
        except EnvelopeError as exc:
            return "skipped", "invalid envelope: {}".format(exc)

        key = message_key(envelope)
        if key in self.state.processed_messages:
            return "skipped", "duplicate message"

        try:
            expected_card = self._expected_card(task_id, identifiers)
        except Exception as exc:  # DSH read failed or bad payload
            return "error", "cannot verify card for {}: {}".format(task_id, exc)

        if expected_card is None:
            return "error", "cannot verify card for {}".format(task_id)
        if envelope["card"] != expected_card:
            return "skipped", "card does not match mapping"
        if envelope["task_id"] != task_id:
            return "skipped", "task_id does not match mapping"
        if envelope["agent"] != self.builder_agent:
            return "skipped", "agent does not match builder_agent"

        kind = envelope["type"]
        if kind == ACK:
            return "applied", {
                "state_id": normalized["state_id"],
                "key": key,
                "status": card.get("last_status") or "submitted",
            }

        try:
            detail = self.dsh.get_task(task_id)
            current = _detail_task(detail)
            version = _task_version(current)
        except Exception as exc:  # DSH failure or unusable payload
            return "error", "cannot read current task: {}".format(exc)

        current_status = _optional_text(current.get("status")).lower()
        if current_status in TERMINAL_STATUSES:
            return "skipped", "DSH task is already {}".format(current_status)

        if kind == TASK_UPDATE:
            target = "in_progress"
            body_text = _update_comment(envelope)
            close = False
        elif kind == TASK_COMPLETE:
            target = "done"
            body_text = _complete_comment(envelope)
            close = True
        else:  # parse_task_reply only returns the three reply types
            return "skipped", "unsupported reply type {!r}".format(kind)

        try:
            self.dsh.update_status(task_id, version, target)
            self.dsh.comment(task_id, body_text)
        except Exception as exc:  # do not mark an ambiguous write processed
            return "error", "DSH write failed: {}".format(exc)

        return "applied", {
            "state_id": normalized["state_id"],
            "key": key,
            "status": target,
            "close": close,
        }

    def _close_transport(
        self, ref: str, task_id: str, counts: Dict[str, int]
    ) -> None:
        """Close a completed task's thread without undoing the DSH write."""
        try:
            self.transport.close(ref)
        except Exception as exc:  # the card is already done; keep it that way
            counts["errors"] += 1
            LOGGER.error(
                "cannot close transport for %s after task_complete: %s",
                task_id,
                exc,
            )

    def _expected_card(
        self, task_id: str, identifiers: Dict[str, Optional[str]]
    ) -> Optional[str]:
        """Return the card identifier for a mapped task.

        Bootstrap normally carries every task, including terminal ones. A
        mapping whose task is no longer in the board is resolved once through
        ``dsh.get_task`` so a reply can still be verified.
        """
        if task_id in identifiers:
            return identifiers[task_id]

        detail = self.dsh.get_task(task_id)
        task = _detail_task(detail)
        identifier = _optional_text(task.get("identifier")) or None
        identifiers[task_id] = identifier
        return identifier
