"""Envelope parsing and validation for the M1.5 task bus.

Every machine message is one JSON object. This module knows nothing about
GitHub, DSH, routine webhooks, or persisted bridge state; it only answers
"is this envelope well formed?" and returns a normalized copy.

Envelope (all types)::

    {
      "bridge": "dsh-grokbot-v1",
      "message_id": "uuid",       # stable id; used for dedupe
      "from": "掌柜",              # 掌柜 or Origin
      "to": "Origin",             # the other side
      "card": "TEST-004",
      "task_id": "task-abc",
      "type": "task_request",
      "agent": "掌柜",
      "note": "optional, <= 500 chars"
    }

Type-specific fields:

``task_request`` (掌柜 -> Origin)
    ``title`` (str), ``instructions`` (str), optional ``acceptance``
    (list of non-empty strings).

``task_update`` (Origin -> 掌柜)
    ``state`` is ``in_progress`` or ``blocked``; optional ``evidence``
    (list of non-empty strings). ``done`` belongs to ``task_complete``.

``task_complete`` (Origin -> 掌柜)
    ``state`` must be ``done``; ``evidence`` must contain at least one
    non-empty string.

``ack`` (Origin -> 掌柜)
    ``status`` is ``received``, ``rejected``, or ``error``.

Unknown fields, unknown types/states/statuses, missing required fields, and
prose (anything that is not a JSON object) raise :class:`EnvelopeError`.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

BRIDGE = "dsh-grokbot-v1"
ACTORS = ("掌柜", "Origin")

TASK_REQUEST = "task_request"
TASK_UPDATE = "task_update"
TASK_COMPLETE = "task_complete"
ACK = "ack"
TYPES = (TASK_REQUEST, TASK_UPDATE, TASK_COMPLETE, ACK)

DIRECTIONS = {
    TASK_REQUEST: ("掌柜", "Origin"),
    TASK_UPDATE: ("Origin", "掌柜"),
    TASK_COMPLETE: ("Origin", "掌柜"),
    ACK: ("Origin", "掌柜"),
}

UPDATE_STATES = ("in_progress", "blocked")
ACK_STATUSES = ("received", "rejected", "error")

CARD_RE = re.compile(r"^[A-Z]+-\d+$")
MAX_NOTE_LENGTH = 500

_COMMON_FIELDS = frozenset(
    ("bridge", "message_id", "from", "to", "card", "task_id", "type", "agent", "note")
)
_TYPE_FIELDS = {
    TASK_REQUEST: frozenset(("title", "instructions", "acceptance")),
    TASK_UPDATE: frozenset(("state", "evidence")),
    TASK_COMPLETE: frozenset(("state", "evidence")),
    ACK: frozenset(("status",)),
}

__all__ = [
    "ACK",
    "ACK_STATUSES",
    "ACTORS",
    "BRIDGE",
    "CARD_RE",
    "DIRECTIONS",
    "EnvelopeError",
    "MAX_NOTE_LENGTH",
    "TASK_COMPLETE",
    "TASK_REQUEST",
    "TASK_UPDATE",
    "TYPES",
    "UPDATE_STATES",
    "message_key",
    "parse_task_reply",
    "parse_task_request",
]


class EnvelopeError(ValueError):
    """Raised when a bridge envelope is malformed or incomplete."""


def _as_object(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise EnvelopeError(
            "message must be a JSON object, got {}; prose is not accepted".format(
                type(raw).__name__
            )
        )
    return raw


def _required_string(data: Dict[str, Any], field: str) -> str:
    if field not in data:
        raise EnvelopeError("missing required field: {}".format(field))
    value = data[field]
    if not isinstance(value, str):
        raise EnvelopeError(
            "{} must be a string, got {}".format(field, type(value).__name__)
        )
    value = value.strip()
    if not value:
        raise EnvelopeError("{} must be a non-empty string".format(field))
    return value


def _optional_note(data: Dict[str, Any]) -> str:
    if "note" not in data:
        return ""
    value = data["note"]
    if not isinstance(value, str):
        raise EnvelopeError(
            "note must be a string, got {}".format(type(value).__name__)
        )
    value = value.strip()
    if not value:
        raise EnvelopeError("note must be a non-empty string when present")
    if len(value) > MAX_NOTE_LENGTH:
        raise EnvelopeError(
            "note must be at most {} characters, got {}".format(
                MAX_NOTE_LENGTH, len(value)
            )
        )
    return value


def _string_list(
    data: Dict[str, Any], field: str, required: bool, context: str
) -> List[str]:
    if field not in data:
        if required:
            raise EnvelopeError(
                "{} requires at least one {} item".format(context, field)
            )
        return []

    value = data[field]
    if not isinstance(value, list):
        raise EnvelopeError(
            "{} must be a list of non-empty strings, got {}".format(
                field, type(value).__name__
            )
        )

    if required and not value:
        raise EnvelopeError(
            "{} requires at least one {} item".format(context, field)
        )

    items = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise EnvelopeError(
                "{}[{}] must be a non-empty string, got {}".format(
                    field, index, type(item).__name__
                )
            )
        text = item.strip()
        if not text:
            raise EnvelopeError(
                "{}[{}] must be a non-empty string".format(field, index)
            )
        items.append(text)
    return items


def _parse_request_fields(data: Dict[str, Any], normalized: Dict[str, Any]) -> None:
    normalized["title"] = _required_string(data, "title")
    normalized["instructions"] = _required_string(data, "instructions")
    normalized["acceptance"] = _string_list(
        data, "acceptance", required=False, context=TASK_REQUEST
    )


def _parse_update_fields(data: Dict[str, Any], normalized: Dict[str, Any]) -> None:
    state = _required_string(data, "state")
    if state == "done":
        raise EnvelopeError(
            "task_update must not use state='done'; "
            "send type='task_complete' with evidence"
        )
    if state not in UPDATE_STATES:
        raise EnvelopeError(
            "unknown state {!r} for task_update; expected one of {}".format(
                state, ", ".join(UPDATE_STATES)
            )
        )
    normalized["state"] = state
    normalized["evidence"] = _string_list(
        data, "evidence", required=False, context=TASK_UPDATE
    )


def _parse_complete_fields(data: Dict[str, Any], normalized: Dict[str, Any]) -> None:
    state = _required_string(data, "state")
    if state != "done":
        raise EnvelopeError(
            "task_complete requires state='done', got {!r}".format(state)
        )
    normalized["state"] = state
    normalized["evidence"] = _string_list(
        data, "evidence", required=True, context=TASK_COMPLETE
    )


def _parse_ack_fields(data: Dict[str, Any], normalized: Dict[str, Any]) -> None:
    status = _required_string(data, "status")
    if status not in ACK_STATUSES:
        raise EnvelopeError(
            "unknown status {!r} for ack; expected one of {}".format(
                status, ", ".join(ACK_STATUSES)
            )
        )
    normalized["status"] = status


_FIELD_PARSERS = {
    TASK_REQUEST: _parse_request_fields,
    TASK_UPDATE: _parse_update_fields,
    TASK_COMPLETE: _parse_complete_fields,
    ACK: _parse_ack_fields,
}


def _parse_message(raw: Any) -> Dict[str, Any]:
    data = _as_object(raw)

    kind = _required_string(data, "type")
    if kind not in TYPES:
        raise EnvelopeError(
            "unknown type {!r}; expected one of {}".format(kind, ", ".join(TYPES))
        )

    unknown = sorted(set(data) - _COMMON_FIELDS - _TYPE_FIELDS[kind])
    if unknown:
        raise EnvelopeError(
            "unknown field(s) for {}: {}".format(kind, ", ".join(unknown))
        )

    bridge = _required_string(data, "bridge")
    if bridge != BRIDGE:
        raise EnvelopeError(
            "bridge must be {!r}, got {!r}".format(BRIDGE, bridge)
        )

    message_id = _required_string(data, "message_id")

    sender = _required_string(data, "from")
    if sender not in ACTORS:
        raise EnvelopeError(
            "from must be one of {}, got {!r}".format(
                ", ".join(ACTORS), sender
            )
        )

    recipient = _required_string(data, "to")
    if recipient not in ACTORS:
        raise EnvelopeError(
            "to must be one of {}, got {!r}".format(", ".join(ACTORS), recipient)
        )

    expected_from, expected_to = DIRECTIONS[kind]
    if sender != expected_from:
        raise EnvelopeError(
            "{} must be from {}, got {}".format(kind, expected_from, sender)
        )
    if recipient != expected_to:
        raise EnvelopeError(
            "{} must be to {}, got {}".format(kind, expected_to, recipient)
        )

    card = _required_string(data, "card")
    if not CARD_RE.match(card):
        raise EnvelopeError(
            "card must look like TEST-004, got {!r}".format(card)
        )

    task_id = _required_string(data, "task_id")
    agent = _required_string(data, "agent")
    note = _optional_note(data)

    normalized = {
        "bridge": bridge,
        "message_id": message_id,
        "from": sender,
        "to": recipient,
        "card": card,
        "task_id": task_id,
        "type": kind,
        "agent": agent,
        "note": note,
    }
    _FIELD_PARSERS[kind](data, normalized)
    return normalized


def parse_task_request(raw: Any) -> Dict[str, Any]:
    """Validate a ``task_request`` and return a normalized copy.

    Raises :class:`EnvelopeError` if the message is not a task request, is
    missing required fields, contains unknown fields, or has a wrong
    direction (a request must travel from 掌柜 to Origin).
    """
    normalized = _parse_message(raw)
    if normalized["type"] != TASK_REQUEST:
        raise EnvelopeError(
            "expected type 'task_request', got {!r}".format(normalized["type"])
        )
    return normalized


def parse_task_reply(raw: Any) -> Dict[str, Any]:
    """Validate an Origin reply and return a normalized copy.

    Replies are ``task_update``, ``task_complete``, or ``ack``. A request is
    rejected. ``task_complete`` must carry ``state='done'`` and at least one
    evidence item; ``task_update`` must never use ``state='done'``.
    """
    normalized = _parse_message(raw)
    if normalized["type"] == TASK_REQUEST:
        raise EnvelopeError(
            "task_request is not a reply; expected one of task_update, "
            "task_complete, or ack"
        )
    return normalized


def message_key(raw: Any) -> str:
    """Return a stable key for deduplicating a message.

    ``message_id`` is the identity of a bridge message, so the key is the
    bridge namespace plus the normalized id. Re-reading the same message,
    reordering its JSON fields, or changing optional payload (for example
    ``note``) yields the same key. A missing or blank ``message_id`` raises
    :class:`EnvelopeError`.
    """
    data = _as_object(raw)
    message_id = _required_string(data, "message_id")
    return "{}:{}".format(BRIDGE, message_id)
