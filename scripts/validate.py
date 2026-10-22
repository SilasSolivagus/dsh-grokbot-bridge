#!/usr/bin/env python3
"""Validate DSH <-> Grok Bot bridge messages using only the standard library."""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

BRIDGE = "dsh-grokbot-v1"
ACTORS = {"掌柜", "Origin"}
TYPES = {"ping", "pong", "decision", "decision_result", "ack", "status"}
DIRECTIONS = {
    "ping": ("掌柜", "Origin"),
    "pong": ("Origin", "掌柜"),
    "decision": ("Origin", "掌柜"),
    "decision_result": ("掌柜", "Origin"),
    "ack": ("Origin", "掌柜"),
    "status": ("Origin", "掌柜"),
}
CARD_RE = re.compile(r"^[A-Z]+-\d+$")


def is_nonempty_str(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def check(condition: bool, errors: list[str], message: str) -> None:
    if not condition:
        errors.append(message)


def parse_deadline(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def validate_message(data, path: Path) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["root must be a JSON object"]

    for field in ("bridge", "from", "to", "card", "type", "agent"):
        check(field in data, errors, f"missing required field: {field}")

    check(data.get("bridge") == BRIDGE, errors, f"bridge must be {BRIDGE!r}")
    check(data.get("from") in ACTORS, errors, "from must be 掌柜 or Origin")
    check(data.get("to") in ACTORS, errors, "to must be 掌柜 or Origin")
    check(is_nonempty_str(data.get("agent")), errors, "agent must be a non-empty string")

    card = data.get("card")
    check(is_nonempty_str(card) and bool(CARD_RE.match(card or "")), errors, "card must look like TEST-003")

    kind = data.get("type")
    check(kind in TYPES, errors, f"type must be one of {sorted(TYPES)}")
    if kind not in TYPES:
        return errors

    expected_from, expected_to = DIRECTIONS[kind]
    check(data.get("from") == expected_from, errors, f"{kind} must be from {expected_from}")
    check(data.get("to") == expected_to, errors, f"{kind} must be to {expected_to}")

    note = data.get("note")
    if note is not None:
        check(is_nonempty_str(note) and len(note) <= 500, errors, "note must be a non-empty string <= 500 chars")

    if kind == "decision":
        needs_silas = data.get("needs_silas")
        check(isinstance(needs_silas, bool), errors, "decision.needs_silas must be a boolean")
        if needs_silas is True:
            for field in ("decision", "recommendation", "deadline"):
                check(is_nonempty_str(data.get(field)), errors, f"decision.needs_silas=true requires {field}")

            options = data.get("options")
            check(isinstance(options, list) and 2 <= len(options) <= 3, errors, "decision.options must contain 2-3 items")
            if isinstance(options, list):
                for index, option in enumerate(options, 1):
                    check(is_nonempty_str(option), errors, f"decision.options[{index}] must be a non-empty string")

            options_short = data.get("options_short")
            if options_short is not None:
                check(
                    isinstance(options_short, list) and 2 <= len(options_short) <= 3,
                    errors,
                    "decision.options_short must contain 2-3 items",
                )
                if isinstance(options_short, list):
                    for index, option in enumerate(options_short, 1):
                        check(
                            is_nonempty_str(option) and len(option) <= 40,
                            errors,
                            f"decision.options_short[{index}] must be a non-empty string <= 40 chars",
                        )

            deadline = data.get("deadline")
            if is_nonempty_str(deadline):
                check(parse_deadline(deadline) is not None, errors, "deadline must be ISO 8601 with timezone offset")

    if kind == "decision_result":
        for field in ("choice", "choice_label", "decided_by", "decided_at"):
            check(is_nonempty_str(data.get(field)), errors, f"decision_result requires {field}")
        decided_at = data.get("decided_at")
        if is_nonempty_str(decided_at):
            check(parse_deadline(decided_at) is not None, errors, "decided_at must be ISO 8601 with timezone offset")
        check(data.get("decided_by") == "Silas", errors, "decision_result.decided_by must be Silas")

    if kind == "ack":
        check(data.get("status") in {"received", "rejected", "error"}, errors, "ack.status must be received/rejected/error")

    if kind == "status":
        check(is_nonempty_str(data.get("note")), errors, "status requires a note")

    return errors


def main(argv: list[str]) -> int:
    paths = [Path(arg) for arg in argv[1:]]
    if not paths:
        print("usage: python3 scripts/validate.py examples/*.json", file=sys.stderr)
        return 2

    failed = 0
    for path in paths:
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            print(f"FAIL {path}: {error}")
            failed += 1
            continue

        errors = validate_message(data, path)
        if errors:
            failed += 1
            print(f"FAIL {path}")
            for error in errors:
                print(f"  - {error}")
        else:
            print(f"OK   {path}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
