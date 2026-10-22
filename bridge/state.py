"""Persistent bridge state and message idempotency for the M1.5 task bus.

The state file is one UTF-8 JSON object::

    {
      "cards": {
        "task-id": {
          "issue": 12,
          "run_uuid": "run-...",
          "last_comment_id": 34,
          "last_status": "in_progress"
        }
      },
      "processed_messages": ["dsh-grokbot-v1:uuid"]
    }

DSH remains authoritative. This module stores only the GitHub issue mapping
and the set of already-handled message keys; it knows nothing about DSH,
GitHub, routine webhooks, or the orchestrator.

Persistence rules:

* A missing file is an empty state: ``{"cards": {}, "processed_messages": []}``.
* A file that exists but cannot be read, is not valid JSON, or does not match
  the state shape raises :class:`StateError`. A failed load never resets,
  rewrites, or deletes the file.
* :meth:`BridgeState.save` writes a temporary file in the destination
  directory, flushes it, and replaces the real file with ``os.replace``. A
  failed save leaves the previous file intact and removes the temporary file.
* Mutations are explicit: call :meth:`BridgeState.save` after changing state.
  Methods never write to disk on their own.

Card fields are allowlisted to the four fields above. Unknown fields, wrong
types, and blank values are rejected instead of being persisted silently.
Task ids and message keys are stripped on input; on load they must already be
free of surrounding whitespace, so reading a file never changes it.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict, List, Optional

__all__ = ["BridgeState", "CARD_FIELDS", "STATE_KEYS", "StateError"]

CARD_FIELDS = ("issue", "run_uuid", "last_comment_id", "last_status")
STATE_KEYS = ("cards", "processed_messages")


class StateError(Exception):
    """Raised when bridge state cannot be read, parsed, validated, or saved."""


def _empty_state() -> Dict[str, Any]:
    return {"cards": {}, "processed_messages": []}


def _normalize_path(path: Any) -> str:
    try:
        raw = os.fspath(path)
    except TypeError as exc:
        raise ValueError(
            "state path must be a string or a path-like object"
        ) from exc
    if not isinstance(raw, str):
        raise ValueError(
            "state path must be a string or a path-like object returning str"
        )
    if not raw.strip():
        raise ValueError("state path must be a non-empty string")
    return raw


def _input_text(value: Any, label: str) -> str:
    """Validate a caller-provided string and return it stripped."""
    if not isinstance(value, str):
        raise ValueError(
            "{} must be a string, got {}".format(label, type(value).__name__)
        )
    text = value.strip()
    if not text:
        raise ValueError("{} must be a non-empty string".format(label))
    return text


def _input_card_value(field: str, value: Any) -> Any:
    """Validate one caller-provided card field value."""
    if field == "issue":
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(
                "issue must be a positive integer, got {!r}".format(value)
            )
        return value
    if field == "last_comment_id":
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(
                "last_comment_id must be a non-negative integer, got {!r}".format(
                    value
                )
            )
        return value
    if field in ("run_uuid", "last_status"):
        return _input_text(value, field)
    raise ValueError("unknown card field: {}".format(field))


def _state_text(value: Any, prefix: str) -> str:
    """Validate a string loaded from disk without normalizing it."""
    if not isinstance(value, str):
        raise StateError(
            "{} must be a string, got {}".format(prefix, type(value).__name__)
        )
    if not value.strip():
        raise StateError("{} must be a non-empty string".format(prefix))
    if value != value.strip():
        raise StateError(
            "{} must not have surrounding whitespace".format(prefix)
        )
    return value


def _validate_card_value(task_id: str, field: str, value: Any, path: str) -> Any:
    prefix = "invalid bridge state at {}: cards[{!r}].{}".format(
        path, task_id, field
    )
    if field == "issue":
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise StateError(
                "{} must be a positive integer, got {!r}".format(prefix, value)
            )
        return value
    if field == "last_comment_id":
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise StateError(
                "{} must be a non-negative integer, got {!r}".format(
                    prefix, value
                )
            )
        return value
    if field in ("run_uuid", "last_status"):
        return _state_text(value, prefix)
    raise StateError("{} is an unknown card field".format(prefix))


def _validate_state(data: Any, path: str) -> Dict[str, Any]:
    """Return a validated deep copy of ``data`` or raise :class:`StateError`."""
    if not isinstance(data, dict):
        raise StateError(
            "invalid bridge state at {}: expected a JSON object, got {}".format(
                path, type(data).__name__
            )
        )

    missing = [key for key in STATE_KEYS if key not in data]
    if missing:
        raise StateError(
            "invalid bridge state at {}: missing key(s): {}".format(
                path, ", ".join(missing)
            )
        )

    unknown = sorted(set(data) - set(STATE_KEYS))
    if unknown:
        raise StateError(
            "invalid bridge state at {}: unknown top-level key(s): {}".format(
                path, ", ".join(unknown)
            )
        )

    raw_cards = data["cards"]
    if not isinstance(raw_cards, dict):
        raise StateError(
            "invalid bridge state at {}: 'cards' must be a JSON object, "
            "got {}".format(path, type(raw_cards).__name__)
        )

    cards: Dict[str, Dict[str, Any]] = {}
    for task_id, raw_card in raw_cards.items():
        if not isinstance(task_id, str):
            raise StateError(
                "invalid bridge state at {}: card task id must be a string, "
                "got {}".format(path, type(task_id).__name__)
            )
        if not task_id or task_id != task_id.strip():
            raise StateError(
                "invalid bridge state at {}: card task id {!r} must be "
                "non-empty without surrounding whitespace".format(path, task_id)
            )
        if not isinstance(raw_card, dict):
            raise StateError(
                "invalid bridge state at {}: cards[{!r}] must be a JSON "
                "object, got {}".format(
                    path, task_id, type(raw_card).__name__
                )
            )
        unknown_card = sorted(set(raw_card) - set(CARD_FIELDS))
        if unknown_card:
            raise StateError(
                "invalid bridge state at {}: cards[{!r}] has unknown "
                "field(s): {}".format(path, task_id, ", ".join(unknown_card))
            )
        card: Dict[str, Any] = {}
        for field in CARD_FIELDS:
            if field in raw_card:
                card[field] = _validate_card_value(
                    task_id, field, raw_card[field], path
                )
        cards[task_id] = card

    raw_processed = data["processed_messages"]
    if not isinstance(raw_processed, list):
        raise StateError(
            "invalid bridge state at {}: 'processed_messages' must be a "
            "JSON array, got {}".format(path, type(raw_processed).__name__)
        )

    processed: List[str] = []
    seen = set()
    for index, key in enumerate(raw_processed):
        prefix = "invalid bridge state at {}: processed_messages[{}]".format(
            path, index
        )
        if not isinstance(key, str):
            raise StateError(
                "{} must be a string, got {}".format(
                    prefix, type(key).__name__
                )
            )
        if not key.strip():
            raise StateError("{} must be a non-empty string".format(prefix))
        if key != key.strip():
            raise StateError(
                "{} must not have surrounding whitespace".format(prefix)
            )
        if key in seen:
            raise StateError(
                "{} is a duplicate key {!r}".format(prefix, key)
            )
        seen.add(key)
        processed.append(key)

    return {"cards": cards, "processed_messages": processed}


def _load_file(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read()
    except FileNotFoundError:
        return _empty_state()
    except (OSError, UnicodeError) as exc:
        raise StateError(
            "cannot read bridge state at {}: {}".format(path, exc)
        ) from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StateError(
            "invalid bridge state at {}: file is not valid JSON ({}); "
            "refusing to reset it".format(path, exc)
        ) from exc

    return _validate_state(data, path)


def _fsync_directory(directory: str) -> None:
    """Best-effort directory fsync so a rename survives a crash."""
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        try:
            os.close(dir_fd)
        except OSError:
            pass


class BridgeState:
    """Bridge state backed by one JSON file.

    The constructor loads the file when it exists and starts from an empty
    state when it does not. Use :meth:`load` for an explicitly named load and
    :meth:`save` to persist changes.
    """

    def __init__(self, path: Any) -> None:
        self.path = _normalize_path(path)
        self._data = _load_file(self.path)

    @classmethod
    def load(cls, path: Any) -> "BridgeState":
        """Load state from ``path``; a missing file yields an empty state."""
        return cls(path)

    def to_dict(self) -> Dict[str, Any]:
        """Return a deep copy of the full state mapping."""
        return _validate_state(self._data, self.path)

    @property
    def cards(self) -> Dict[str, Dict[str, Any]]:
        """Return a copy of the task-id to card mapping."""
        return {
            task_id: dict(card)
            for task_id, card in self._data["cards"].items()
        }

    @property
    def processed_messages(self) -> List[str]:
        """Return a copy of the handled message keys, in insertion order."""
        return list(self._data["processed_messages"])

    def mark_processed(self, key: Any) -> bool:
        """Record ``key`` as handled.

        Returns ``True`` when the key is new and ``False`` when it was already
        processed, so callers can use this as the idempotency check. Call
        :meth:`save` afterwards to persist the new key.
        """
        key = _input_text(key, "message key")
        processed = self._data["processed_messages"]
        if key in processed:
            return False
        processed.append(key)
        return True

    def card_for_task(self, task_id: Any) -> Optional[Dict[str, Any]]:
        """Return a copy of the card mapping for ``task_id``, or ``None``."""
        task_id = _input_text(task_id, "task_id")
        card = self._data["cards"].get(task_id)
        return None if card is None else dict(card)

    def upsert_card(self, task_id: Any, **fields: Any) -> Dict[str, Any]:
        """Create or merge the card mapping for ``task_id``.

        Only the four fields in :data:`CARD_FIELDS` are accepted. Every value
        is validated before the state changes, so a bad field leaves the card
        untouched. Returns a copy of the merged card.
        """
        task_id = _input_text(task_id, "task_id")
        unknown = sorted(set(fields) - set(CARD_FIELDS))
        if unknown:
            raise ValueError(
                "unknown card field(s): {}".format(", ".join(unknown))
            )
        updates = {
            field: _input_card_value(field, fields[field])
            for field in CARD_FIELDS
            if field in fields
        }

        card = self._data["cards"].get(task_id)
        if card is None:
            card = {}
            self._data["cards"][task_id] = card
        card.update(updates)
        return dict(card)

    def save(self) -> None:
        """Atomically write the state to :attr:`path`.

        The destination is either the previous content or the new content,
        never a partial write. Raises :class:`StateError` when the state
        cannot be validated or written.
        """
        data = _validate_state(self._data, self.path)
        try:
            payload = (
                json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n"
            )
        except (TypeError, ValueError) as exc:
            raise StateError(
                "cannot serialize bridge state at {}: {}".format(
                    self.path, exc
                )
            ) from exc

        directory = os.path.dirname(os.path.abspath(self.path))
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as exc:
            raise StateError(
                "cannot create state directory at {}: {}".format(
                    directory, exc
                )
            ) from exc

        fd = None
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                prefix=".bridge-state-", suffix=".tmp", dir=directory
            )
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                fd = None
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.path)
            tmp_path = None
        except (OSError, UnicodeError) as exc:
            raise StateError(
                "cannot save bridge state at {}: {}".format(self.path, exc)
            ) from exc
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        _fsync_directory(directory)
