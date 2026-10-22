"""Tests for the M1.5 task-bus orchestrator.

Every dependency is an in-memory fake: no DSH server, GitHub repository, or
Grok routine is contacted. The real :class:`~bridge.state.BridgeState` is used
because atomic saving and reload behavior are part of the contract.

The test fakes record calls instead of performing I/O, so the assertions pin
the orchestrator's decisions: what it opens, what it writes to DSH, what it
persists, and what it refuses to do.
"""

import ast
import copy
import json
import os
import tempfile
import unittest

import bridge.orchestrator as orchestrator_module
from bridge.dsh import DshError
from bridge.envelope import parse_task_request
from bridge.orchestrator import Orchestrator, OrchestratorError
from bridge.routine import RoutineError
from bridge.state import BridgeState
from bridge.transport import TransportError

PROJECT = {"id": "dsh-kanban", "name": "DSH Kanban"}
TASK_ID = "440e9040-1657-45bf-a945-0cadc6cabec0"
OTHER_TASK_ID = "550e9040-1657-45bf-a945-0cadc6cabec1"
CARD = "TEST-004"
OTHER_CARD = "TEST-005"
BUILDER = "builder"
GROK_LOGIN = "grok-bot"
ISSUE_REF = "12"
RUN_UUID = "9f8c1f4e-2b7a-4d55-9a8e-3c6b0e1d2f3a"
MESSAGE_ID_1 = "11111111-1111-4111-8111-111111111111"
MESSAGE_ID_2 = "22222222-2222-4222-8222-222222222222"
MESSAGE_ID_3 = "33333333-3333-4333-8333-333333333333"

ALLOWED_REQUEST_FIELDS = {
    "bridge",
    "message_id",
    "type",
    "from",
    "to",
    "agent",
    "card",
    "task_id",
    "title",
    "instructions",
    "acceptance",
}


def task(**overrides):
    """Return one bootstrap-shaped DSH task; ``overrides`` win."""
    value = {
        "id": TASK_ID,
        "identifier": CARD,
        "projectId": "dsh-kanban",
        "title": "Add the orchestrator",
        "description": "Implement one poll cycle.",
        "status": "inbox",
        "assignee": {"type": "agent", "id": BUILDER},
        "archivedAt": None,
        "version": 1,
    }
    value.update(overrides)
    return value


def board(tasks=None):
    return {
        "projects": [PROJECT],
        "tasks": list(tasks if tasks is not None else [task()]),
    }


def reply(kind, **overrides):
    """Return one valid grok reply envelope."""
    value = {
        "bridge": "dsh-grokbot-v1",
        "message_id": MESSAGE_ID_1,
        "from": "grok",
        "to": "dsh",
        "card": CARD,
        "task_id": TASK_ID,
        "type": kind,
        "agent": BUILDER,
    }
    value.update(overrides)
    return value


def comment(payload, **overrides):
    """Return one transport-shaped comment; ``payload`` may be a dict or text."""
    body = payload if isinstance(payload, str) else json.dumps(payload)
    value = {
        "id": 101,
        "body": body,
        "author": GROK_LOGIN,
        "createdAt": "2026-10-22T00:00:00Z",
    }
    value.update(overrides)
    return value


def request_echo(**overrides):
    """Return the dsh ``task_request`` envelope that a relay echoes back."""
    value = {
        "bridge": "dsh-grokbot-v1",
        "message_id": MESSAGE_ID_2,
        "from": "dsh",
        "to": "grok",
        "card": CARD,
        "task_id": TASK_ID,
        "type": "task_request",
        "agent": BUILDER,
        "title": "Add the orchestrator",
        "instructions": "Implement one poll cycle.",
    }
    value.update(overrides)
    return value


class FakeDsh:
    """In-memory DSH client with failure injection and call recording."""

    def __init__(self, board_data=None):
        self.board_data = copy.deepcopy(board_data or board())
        self.details = {}
        self.calls = []
        self.updates = []
        self.comments = []
        self.fail_on = {}

    def _fail(self, method):
        error = self.fail_on.get(method)
        if error is not None:
            raise error

    def bootstrap(self):
        self.calls.append(("bootstrap",))
        self._fail("bootstrap")
        return copy.deepcopy(self.board_data)

    def _detail_for(self, task_id):
        detail = self.details.get(task_id)
        if detail is not None:
            return detail
        for item in self.board_data.get("tasks", []):
            if item.get("id") == task_id:
                return {
                    "task": copy.deepcopy(item),
                    "comments": [],
                    "activity": [],
                    "attachments": [],
                }
        return None

    def get_task(self, task_id):
        self.calls.append(("get_task", task_id))
        self._fail("get_task")
        detail = self._detail_for(task_id)
        if detail is None:
            raise AssertionError("unexpected get_task({!r})".format(task_id))
        return copy.deepcopy(detail)

    def update_status(self, task_id, version, status):
        self.calls.append(("update_status", task_id, version, status))
        self._fail("update_status")
        self.updates.append((task_id, version, status))
        return {"id": task_id, "version": version + 1, "status": status}

    def comment(self, task_id, body):
        self.calls.append(("comment", task_id, body))
        self._fail("comment")
        self.comments.append((task_id, body))
        return {"id": "c-{}".format(len(self.comments)), "body": body}


class FakeTransport:
    """In-memory transport returning canned pages or a handler result."""

    def __init__(self, ref=ISSUE_REF, pages=None, handler=None):
        self.ref = ref
        self.pages = copy.deepcopy(pages or {})
        self.handler = handler
        self.opened = []
        self.calls = []
        self.closed = []
        self.fail_open = None
        self.fail_replies = None
        self.fail_close = None

    def open(self, request):
        self.opened.append(copy.deepcopy(request))
        if self.fail_open is not None:
            raise self.fail_open
        return self.ref

    def replies(self, ref, after=None):
        self.calls.append((ref, after))
        if self.fail_replies is not None:
            raise self.fail_replies
        if self.handler is not None:
            messages, cursor = self.handler(ref, after)
            return copy.deepcopy(messages), cursor
        messages, cursor = self.pages.get((ref, after), ([], None))
        return copy.deepcopy(messages), cursor

    def close(self, ref):
        self.closed.append(ref)
        if self.fail_close is not None:
            raise self.fail_close


class FakeRoutine:
    """In-memory routine webhook with call recording and failure injection."""

    def __init__(self, run_uuid=RUN_UUID, response=None):
        self.run_uuid = run_uuid
        self.response = response
        self.calls = []
        self.failure = None

    def wake(self, payload):
        self.calls.append(copy.deepcopy(payload))
        if self.failure is not None:
            raise self.failure
        if self.response is not None:
            return copy.deepcopy(self.response)
        return {"runUuid": self.run_uuid}


class RecordingState:
    """BridgeState proxy that snapshots every save."""

    def __init__(self, inner):
        self.inner = inner
        self.saves = []

    @property
    def cards(self):
        return self.inner.cards

    @property
    def processed_messages(self):
        return self.inner.processed_messages

    def card_for_task(self, task_id):
        return self.inner.card_for_task(task_id)

    def upsert_card(self, task_id, **fields):
        return self.inner.upsert_card(task_id, **fields)

    def mark_processed(self, key):
        return self.inner.mark_processed(key)

    def save(self):
        self.inner.save()
        self.saves.append(copy.deepcopy(self.inner.to_dict()))


class OrchestratorTestCase(unittest.TestCase):
    """Shared fixtures: temp state file, fakes, and orchestration helpers."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_path = os.path.join(self._tmp.name, "bridge-state.json")
        self.state = BridgeState.load(self.state_path)
        self.dsh = FakeDsh()
        self.transport = FakeTransport()
        self.routine = FakeRoutine()

    def build(self, state=None, **kwargs):
        options = {
            "builder_agent": BUILDER,
            "allowed_logins": [GROK_LOGIN],
        }
        options.update(kwargs)
        return Orchestrator(
            self.dsh,
            self.transport,
            self.routine,
            state if state is not None else self.state,
            **options
        )

    def map_task(self, task_id=TASK_ID, issue=12, last_comment_id=0):
        self.state.upsert_card(
            task_id,
            issue=issue,
            run_uuid=RUN_UUID,
            last_comment_id=last_comment_id,
            last_status="submitted",
        )
        self.state.save()

    def reload_state(self):
        return BridgeState.load(self.state_path)


# ---------------------------------------------------------------------------
# Opening a card
# ---------------------------------------------------------------------------


class OpenTaskTests(OrchestratorTestCase):
    def test_one_card_opens_one_issue_wakes_once_and_saves_mapping(self):
        counts = self.build().run_once()

        self.assertEqual(
            counts, {"opened": 1, "applied": 0, "skipped": 0, "errors": 0}
        )
        self.assertEqual(len(self.transport.opened), 1)
        self.assertEqual(len(self.routine.calls), 1)

        request = self.transport.opened[0]
        self.assertEqual(self.routine.calls[0]["issue"], ISSUE_REF)
        self.assertEqual(self.routine.calls[0]["request"], request)
        self.assertEqual(self.transport.calls, [(ISSUE_REF, 0)])

        saved = self.reload_state().card_for_task(TASK_ID)
        self.assertIsNotNone(saved)
        self.assertEqual(saved["issue"], 12)
        self.assertIsInstance(saved["issue"], int)
        self.assertEqual(saved["run_uuid"], RUN_UUID)
        self.assertEqual(saved["last_comment_id"], 0)
        self.assertEqual(saved["last_status"], "submitted")

    def test_task_request_contains_only_allowed_envelope_fields(self):
        self.build().run_once()

        request = self.transport.opened[0]
        self.assertEqual(set(request), set(request) & ALLOWED_REQUEST_FIELDS)
        for forbidden in ("project_id", "projectId", "issue", "run_uuid", "runUuid"):
            self.assertNotIn(forbidden, request)

        parsed = parse_task_request(request)
        self.assertEqual(parsed["type"], "task_request")
        self.assertEqual(parsed["from"], "dsh")
        self.assertEqual(parsed["to"], "grok")
        self.assertEqual(parsed["agent"], BUILDER)
        self.assertEqual(parsed["card"], CARD)
        self.assertEqual(parsed["task_id"], TASK_ID)
        self.assertEqual(parsed["title"], "Add the orchestrator")
        self.assertEqual(parsed["instructions"], "Implement one poll cycle.")
        self.assertEqual(parsed["acceptance"], [])

    def test_description_becomes_instructions(self):
        self.build().run_once()

        request = self.transport.opened[0]
        self.assertEqual(request["instructions"], "Implement one poll cycle.")

    def test_blank_description_gets_a_short_fallback(self):
        self.dsh.board_data = board([task(description="   ")])

        self.build().run_once()

        request = self.transport.opened[0]
        self.assertTrue(request["instructions"].strip())
        self.assertIn(CARD, request["instructions"])
        parse_task_request(request)

    def test_missing_description_gets_a_short_fallback(self):
        self.dsh.board_data = board([task(description=None)])

        self.build().run_once()

        request = self.transport.opened[0]
        self.assertTrue(request["instructions"].strip())
        parse_task_request(request)

    def test_acceptance_is_forwarded_when_present(self):
        self.dsh.board_data = board([task(acceptance=["tests pass", "docs updated"])])

        self.build().run_once()

        self.assertEqual(
            self.transport.opened[0]["acceptance"], ["tests pass", "docs updated"]
        )

    def test_injected_new_id_supplies_the_message_id(self):
        self.build(new_id=lambda: "fixed-message-id").run_once()

        self.assertEqual(self.transport.opened[0]["message_id"], "fixed-message-id")

    def test_issue_reference_is_stored_as_an_integer_for_bridge_state(self):
        self.build().run_once()

        self.assertEqual(self.reload_state().card_for_task(TASK_ID)["issue"], 12)

    def test_non_numeric_issue_reference_is_not_mapped(self):
        self.transport.ref = "ref-1"

        counts = self.build().run_once()

        self.assertEqual(counts["opened"], 0)
        self.assertEqual(counts["errors"], 1)
        self.assertEqual(self.routine.calls, [])
        self.assertEqual(self.reload_state().cards, {})

    def test_already_mapped_task_is_not_opened_again(self):
        self.map_task()

        counts = self.build().run_once()

        self.assertEqual(counts["opened"], 0)
        self.assertEqual(self.transport.opened, [])
        self.assertEqual(self.routine.calls, [])

    def test_done_canceled_and_archived_tasks_are_not_opened(self):
        self.dsh.board_data = board(
            [
                task(),
                task(
                    id="task-done",
                    identifier="TEST-010",
                    status="done",
                ),
                task(
                    id="task-canceled",
                    identifier="TEST-011",
                    status="canceled",
                ),
                task(
                    id="task-archived",
                    identifier="TEST-012",
                    archivedAt="2026-10-22T00:00:00Z",
                ),
            ]
        )

        self.build().run_once()

        self.assertEqual(len(self.transport.opened), 1)
        self.assertEqual(self.transport.opened[0]["card"], CARD)

    def test_task_assigned_to_another_agent_is_not_opened(self):
        self.dsh.board_data = board(
            [
                task(),
                task(
                    id="task-office",
                    identifier="TEST-013",
                    assignee={"type": "agent", "id": "office"},
                ),
                task(
                    id="task-unassigned",
                    identifier="TEST-014",
                    assignee=None,
                ),
            ]
        )

        self.build().run_once()

        self.assertEqual(len(self.transport.opened), 1)
        self.assertEqual(self.transport.opened[0]["card"], CARD)

    def test_get_task_failure_does_not_open_an_issue(self):
        self.dsh.fail_on["get_task"] = DshError(500, "boom")

        counts = self.build().run_once()

        self.assertEqual(counts["opened"], 0)
        self.assertEqual(counts["errors"], 1)
        self.assertEqual(self.transport.opened, [])
        self.assertEqual(self.routine.calls, [])
        self.assertEqual(self.reload_state().cards, {})

    def test_transport_open_failure_does_not_wake_or_map(self):
        self.transport.fail_open = TransportError("issue create failed")

        counts = self.build().run_once()

        self.assertEqual(counts["opened"], 0)
        self.assertEqual(counts["errors"], 1)
        self.assertEqual(self.routine.calls, [])
        self.assertEqual(self.reload_state().cards, {})

    def test_routine_failure_does_not_save_a_mapping(self):
        self.routine.failure = RoutineError(reason="webhook down")

        counts = self.build().run_once()

        self.assertEqual(counts["opened"], 0)
        self.assertEqual(counts["errors"], 1)
        self.assertEqual(len(self.transport.opened), 1)
        self.assertEqual(self.reload_state().cards, {})

    def test_missing_run_uuid_is_allowed_and_not_stored(self):
        self.routine.run_uuid = None

        counts = self.build().run_once()

        self.assertEqual(counts["opened"], 1)
        self.assertEqual(counts["errors"], 0)
        saved = self.reload_state().card_for_task(TASK_ID)
        self.assertEqual(saved["issue"], 12)
        self.assertEqual(saved["last_status"], "submitted")
        self.assertNotIn("run_uuid", saved)
        self.assertEqual(self.transport.closed, [])

    def test_noop_wake_with_empty_dict_persists_mapping(self):
        self.routine.response = {}

        counts = self.build().run_once()

        self.assertEqual(counts["opened"], 1)
        self.assertEqual(counts["errors"], 0)
        saved = self.reload_state().card_for_task(TASK_ID)
        self.assertEqual(saved["issue"], 12)
        self.assertEqual(saved["last_comment_id"], 0)
        self.assertEqual(saved["last_status"], "submitted")
        self.assertNotIn("run_uuid", saved)
        self.assertEqual(self.transport.closed, [])

    def test_card_without_run_uuid_is_not_reopened(self):
        self.routine.response = {}
        orchestrator = self.build()

        first = orchestrator.run_once()
        second = orchestrator.run_once()

        self.assertEqual(first["opened"], 1)
        self.assertEqual(second["opened"], 0)
        self.assertEqual(len(self.transport.opened), 1)
        self.assertEqual(len(self.routine.calls), 1)

    def test_relay_update_is_applied_after_run_uuidless_open(self):
        self.routine.response = {}
        self.transport.handler = lambda ref, after: (
            [
                reply(
                    "task_update",
                    state="in_progress",
                    message_id=MESSAGE_ID_2,
                )
            ],
            7,
        )

        counts = self.build(allowed_logins=[BUILDER]).run_once()

        self.assertEqual(counts["opened"], 1)
        self.assertEqual(counts["applied"], 1)
        self.assertEqual(counts["errors"], 0)
        self.assertEqual(self.dsh.updates, [(TASK_ID, 1, "in_progress")])
        saved = self.reload_state().card_for_task(TASK_ID)
        self.assertNotIn("run_uuid", saved)
        self.assertEqual(saved["last_status"], "in_progress")
        self.assertEqual(saved["last_comment_id"], 7)

    def test_routine_run_uuid_is_still_stored(self):
        counts = self.build().run_once()

        self.assertEqual(counts["opened"], 1)
        self.assertEqual(counts["errors"], 0)
        saved = self.reload_state().card_for_task(TASK_ID)
        self.assertEqual(saved["run_uuid"], RUN_UUID)

    def test_task_without_identifier_is_not_opened(self):
        self.dsh.board_data = board([task(identifier=None)])

        counts = self.build().run_once()

        self.assertEqual(counts["opened"], 0)
        self.assertEqual(counts["errors"], 1)
        self.assertEqual(self.transport.opened, [])


class BootstrapTests(OrchestratorTestCase):
    def test_missing_or_mistyped_bootstrap_sections_fail_closed(self):
        cases = [
            {},
            {"tasks": []},
            {"projects": []},
            {"projects": [], "tasks": {}},
            {"projects": {}, "tasks": []},
            {"projects": [], "tasks": "nope"},
            [],
        ]
        for board_data in cases:
            with self.subTest(board=board_data):
                self.dsh.board_data = board_data
                with self.assertRaises(OrchestratorError):
                    self.build().run_once()
                self.assertEqual(self.transport.opened, [])
                self.assertEqual(self.routine.calls, [])

    def test_bootstrap_error_propagates_without_writing_state(self):
        self.dsh.fail_on["bootstrap"] = DshError(None, "connection refused")

        with self.assertRaises(DshError):
            self.build().run_once()

        self.assertEqual(self.reload_state().cards, {})


# ---------------------------------------------------------------------------
# Applying replies
# ---------------------------------------------------------------------------


class ReplyTests(OrchestratorTestCase):
    def test_task_update_maps_in_progress_and_comments(self):
        self.map_task()
        self.transport.handler = lambda ref, after: (
            [
                comment(
                    reply(
                        "task_update",
                        state="in_progress",
                        note="Started the work.",
                        evidence=["pytest: 3 passed"],
                        message_id=MESSAGE_ID_2,
                    )
                )
            ],
            101,
        )

        counts = self.build().run_once()

        self.assertEqual(counts["applied"], 1)
        self.assertEqual(self.dsh.updates, [(TASK_ID, 1, "in_progress")])
        self.assertEqual(len(self.dsh.comments), 1)
        body = self.dsh.comments[0][1]
        self.assertIn("Started the work.", body)
        self.assertIn("pytest: 3 passed", body)

        saved = self.reload_state().card_for_task(TASK_ID)
        self.assertEqual(saved["last_status"], "in_progress")
        self.assertEqual(saved["last_comment_id"], 101)

    def test_blocked_maps_to_in_progress_and_adds_blocked_comment(self):
        self.map_task()
        self.transport.handler = lambda ref, after: (
            [
                comment(
                    reply(
                        "task_update",
                        state="blocked",
                        note="Waiting on the API key.",
                        message_id=MESSAGE_ID_2,
                    )
                )
            ],
            101,
        )

        self.build().run_once()

        self.assertEqual(self.dsh.updates, [(TASK_ID, 1, "in_progress")])
        body = self.dsh.comments[0][1]
        self.assertTrue(body.startswith("Blocked:"), body)
        self.assertIn("Waiting on the API key.", body)

        saved = self.reload_state().card_for_task(TASK_ID)
        self.assertEqual(saved["last_status"], "in_progress")

    def test_blocked_with_only_evidence_uses_it_as_the_reason(self):
        self.map_task()
        self.transport.handler = lambda ref, after: (
            [
                comment(
                    reply(
                        "task_update",
                        state="blocked",
                        evidence=["missing GROK_ROUTINE_KEY"],
                        message_id=MESSAGE_ID_2,
                    )
                )
            ],
            101,
        )

        self.build().run_once()

        body = self.dsh.comments[0][1]
        self.assertTrue(body.startswith("Blocked:"), body)
        self.assertIn("missing GROK_ROUTINE_KEY", body)

    def test_task_complete_sets_done_and_comment_includes_evidence(self):
        self.map_task()
        self.transport.handler = lambda ref, after: (
            [
                comment(
                    reply(
                        "task_complete",
                        state="done",
                        note="Finished.",
                        evidence=["pytest: 12 passed", "commit abc123"],
                        message_id=MESSAGE_ID_2,
                    )
                )
            ],
            101,
        )

        counts = self.build().run_once()

        self.assertEqual(counts["applied"], 1)
        self.assertEqual(self.dsh.updates, [(TASK_ID, 1, "done")])
        body = self.dsh.comments[0][1]
        self.assertIn("pytest: 12 passed", body)
        self.assertIn("commit abc123", body)

        saved = self.reload_state().card_for_task(TASK_ID)
        self.assertEqual(saved["last_status"], "done")
        self.assertEqual(saved["last_comment_id"], 101)

    def test_task_complete_closes_the_transport_once(self):
        self.map_task()
        self.transport.handler = lambda ref, after: (
            [
                comment(
                    reply(
                        "task_complete",
                        state="done",
                        evidence=["tests pass"],
                        message_id=MESSAGE_ID_2,
                    )
                )
            ],
            101,
        )

        counts = self.build().run_once()

        self.assertEqual(
            counts, {"opened": 0, "applied": 1, "skipped": 0, "errors": 0}
        )
        self.assertEqual(self.transport.closed, [ISSUE_REF])

    def test_duplicate_task_complete_does_not_close_twice(self):
        self.map_task()
        duplicate = comment(
            reply(
                "task_complete",
                state="done",
                evidence=["tests pass"],
                message_id=MESSAGE_ID_2,
            )
        )
        self.transport.handler = lambda ref, after: ([duplicate], 101)

        orchestrator = self.build()
        orchestrator.run_once()
        orchestrator.run_once()

        self.assertEqual(self.transport.closed, [ISSUE_REF])

    def test_close_failure_counts_an_error_but_keeps_done_state(self):
        self.map_task()
        self.transport.fail_close = TransportError("issue close failed")
        self.transport.handler = lambda ref, after: (
            [
                comment(
                    reply(
                        "task_complete",
                        state="done",
                        evidence=["tests pass"],
                        message_id=MESSAGE_ID_2,
                    )
                )
            ],
            101,
        )

        counts = self.build().run_once()

        self.assertEqual(counts["applied"], 1)
        self.assertEqual(counts["errors"], 1)
        self.assertEqual(self.transport.closed, [ISSUE_REF])
        self.assertEqual(self.dsh.updates, [(TASK_ID, 1, "done")])

        saved = self.reload_state()
        self.assertEqual(saved.card_for_task(TASK_ID)["last_status"], "done")
        self.assertEqual(saved.card_for_task(TASK_ID)["last_comment_id"], 101)
        self.assertEqual(
            saved.processed_messages, ["dsh-grokbot-v1:" + MESSAGE_ID_2]
        )

    def test_task_update_does_not_close_the_transport(self):
        self.map_task()
        self.transport.handler = lambda ref, after: (
            [
                comment(
                    reply(
                        "task_update",
                        state="in_progress",
                        message_id=MESSAGE_ID_2,
                    )
                )
            ],
            101,
        )

        self.build().run_once()

        self.assertEqual(self.transport.closed, [])

    def test_task_complete_without_evidence_is_rejected_before_any_write(self):
        self.map_task()
        bad = reply("task_complete", state="done", message_id=MESSAGE_ID_2)
        self.transport.handler = lambda ref, after: ([comment(bad)], 101)

        counts = self.build().run_once()

        self.assertEqual(counts["applied"], 0)
        self.assertEqual(counts["skipped"], 1)
        self.assertEqual(self.dsh.updates, [])
        self.assertEqual(self.dsh.comments, [])
        self.assertEqual(self.reload_state().processed_messages, [])
        self.assertEqual(self.transport.closed, [])

    def test_ack_makes_no_dsh_calls_but_is_marked_processed(self):
        self.map_task()
        self.transport.handler = lambda ref, after: (
            [comment(reply("ack", status="received", message_id=MESSAGE_ID_2))],
            101,
        )

        counts = self.build().run_once()

        self.assertEqual(counts["applied"], 1)
        self.assertEqual(self.dsh.updates, [])
        self.assertEqual(self.dsh.comments, [])
        self.assertEqual(self.transport.closed, [])
        saved = self.reload_state()
        self.assertEqual(
            saved.processed_messages, ["dsh-grokbot-v1:" + MESSAGE_ID_2]
        )
        self.assertEqual(saved.card_for_task(TASK_ID)["last_comment_id"], 101)

    def test_duplicate_comment_produces_one_dsh_update(self):
        self.map_task()
        duplicate = comment(
            reply(
                "task_update",
                state="in_progress",
                note="Working.",
                message_id=MESSAGE_ID_2,
            )
        )
        self.transport.handler = lambda ref, after: ([duplicate], 101)

        orchestrator = self.build()
        first = orchestrator.run_once()
        second = orchestrator.run_once()

        self.assertEqual(first["applied"], 1)
        self.assertEqual(second["applied"], 0)
        self.assertEqual(second["skipped"], 1)
        self.assertEqual(len(self.dsh.updates), 1)
        self.assertEqual(len(self.dsh.comments), 1)
        self.assertEqual(
            self.reload_state().processed_messages,
            ["dsh-grokbot-v1:" + MESSAGE_ID_2],
        )

    def test_comment_at_or_before_last_comment_id_is_ignored(self):
        self.map_task(last_comment_id=200)
        self.transport.handler = lambda ref, after: (
            [
                comment(
                    reply(
                        "task_update",
                        state="in_progress",
                        message_id=MESSAGE_ID_2,
                    )
                )
            ],
            150,
        )

        counts = self.build().run_once()

        self.assertEqual(counts["skipped"], 1)
        self.assertEqual(self.dsh.updates, [])
        self.assertEqual(self.dsh.comments, [])
        self.assertEqual(
            self.reload_state().card_for_task(TASK_ID)["last_comment_id"], 200
        )

    def test_after_uses_the_persisted_last_comment_id(self):
        self.map_task(last_comment_id=77)

        self.build().run_once()

        self.assertEqual(self.transport.calls, [(ISSUE_REF, 77)])

    def test_transport_cursor_is_stored_exactly_as_returned(self):
        self.map_task()
        self.transport.handler = lambda ref, after: (
            [comment(reply("ack", status="received", message_id=MESSAGE_ID_2))],
            999,
        )

        self.build().run_once()

        self.assertEqual(
            self.reload_state().card_for_task(TASK_ID)["last_comment_id"], 999
        )

    def test_missing_transport_cursor_keeps_the_comment_id(self):
        self.map_task()
        self.transport.handler = lambda ref, after: (
            [comment(reply("ack", status="received", message_id=MESSAGE_ID_2))],
            None,
        )

        self.build().run_once()

        self.assertEqual(
            self.reload_state().card_for_task(TASK_ID)["last_comment_id"], 101
        )

    def test_cursor_at_or_below_after_does_not_rewind_state(self):
        self.map_task(last_comment_id=200)
        self.transport.handler = lambda ref, after: (
            [comment(reply("ack", status="received", message_id=MESSAGE_ID_2), id=150)],
            150,
        )

        self.build().run_once()

        self.assertEqual(
            self.reload_state().card_for_task(TASK_ID)["last_comment_id"], 200
        )


# ---------------------------------------------------------------------------
# Relay envelopes and GitHub comments
# ---------------------------------------------------------------------------


class ReplyShapeTests(OrchestratorTestCase):
    """The same reply loop accepts GitHub comments and raw relay envelopes."""

    def build(self, state=None, **kwargs):
        # Relay replies carry the configured actor id as their author.
        kwargs.setdefault("allowed_logins", [BUILDER])
        return super().build(state=state, **kwargs)

    def test_relay_task_update_is_applied_and_cursor_is_persisted(self):
        self.map_task()
        self.transport.handler = lambda ref, after: (
            [
                reply(
                    "task_update",
                    state="in_progress",
                    note="Relay working.",
                    message_id=MESSAGE_ID_2,
                )
            ],
            7,
        )

        counts = self.build().run_once()

        self.assertEqual(counts["applied"], 1)
        self.assertEqual(counts["errors"], 0)
        self.assertEqual(self.dsh.updates, [(TASK_ID, 1, "in_progress")])
        self.assertIn("Relay working.", self.dsh.comments[0][1])

        saved = self.reload_state().card_for_task(TASK_ID)
        self.assertEqual(saved["last_status"], "in_progress")
        # The relay's message id is a UUID, not a state cursor.
        self.assertEqual(saved["last_comment_id"], 7)

    def test_relay_task_complete_sets_done_and_closes(self):
        self.map_task()
        self.transport.handler = lambda ref, after: (
            [
                reply(
                    "task_complete",
                    state="done",
                    evidence=["relay: tests pass"],
                    message_id=MESSAGE_ID_2,
                )
            ],
            7,
        )

        counts = self.build().run_once()

        self.assertEqual(counts["applied"], 1)
        self.assertEqual(self.dsh.updates, [(TASK_ID, 1, "done")])
        self.assertIn("relay: tests pass", self.dsh.comments[0][1])
        self.assertEqual(self.transport.closed, [ISSUE_REF])

        saved = self.reload_state().card_for_task(TASK_ID)
        self.assertEqual(saved["last_status"], "done")
        self.assertEqual(saved["last_comment_id"], 7)

    def test_relay_task_request_echo_is_skipped_without_dsh_mutation(self):
        self.map_task()
        self.transport.handler = lambda ref, after: ([request_echo()], 7)

        counts = self.build().run_once()

        self.assertEqual(counts["applied"], 0)
        self.assertEqual(counts["skipped"], 1)
        self.assertEqual(counts["errors"], 0)
        self.assertEqual(self.dsh.updates, [])
        self.assertEqual(self.dsh.comments, [])
        self.assertEqual(self.reload_state().processed_messages, [])
        self.assertEqual(
            self.reload_state().card_for_task(TASK_ID)["last_status"], "submitted"
        )

    def test_relay_first_poll_can_skip_echo_and_apply_a_reply(self):
        self.map_task()
        self.transport.handler = lambda ref, after: (
            [
                request_echo(),
                reply(
                    "task_update",
                    state="in_progress",
                    message_id=MESSAGE_ID_3,
                ),
            ],
            7,
        )

        counts = self.build().run_once()

        self.assertEqual(counts["applied"], 1)
        self.assertEqual(counts["skipped"], 1)
        self.assertEqual(self.dsh.updates, [(TASK_ID, 1, "in_progress")])
        self.assertEqual(
            self.reload_state().processed_messages,
            ["dsh-grokbot-v1:" + MESSAGE_ID_3],
        )

    def test_relay_agent_must_be_allowlisted(self):
        self.map_task()
        self.transport.handler = lambda ref, after: (
            [
                reply(
                    "task_update",
                    state="in_progress",
                    agent="mallory",
                    message_id=MESSAGE_ID_2,
                )
            ],
            7,
        )

        counts = self.build().run_once()

        self.assertEqual(counts["skipped"], 1)
        self.assertEqual(self.dsh.updates, [])
        self.assertEqual(self.dsh.comments, [])

    def test_relay_duplicate_message_id_is_skipped(self):
        self.map_task()
        duplicate = reply(
            "task_update",
            state="in_progress",
            message_id=MESSAGE_ID_2,
        )
        self.transport.handler = lambda ref, after: ([duplicate], 7)

        orchestrator = self.build()
        first = orchestrator.run_once()
        second = orchestrator.run_once()

        self.assertEqual(first["applied"], 1)
        self.assertEqual(second["applied"], 0)
        self.assertEqual(second["skipped"], 1)
        self.assertEqual(len(self.dsh.updates), 1)

    def test_github_comment_shape_still_works(self):
        self.map_task()
        self.transport.handler = lambda ref, after: (
            [
                comment(
                    reply(
                        "task_update",
                        state="in_progress",
                        message_id=MESSAGE_ID_2,
                    )
                )
            ],
            101,
        )

        counts = self.build(allowed_logins=[GROK_LOGIN]).run_once()

        self.assertEqual(counts["applied"], 1)
        self.assertEqual(self.dsh.updates, [(TASK_ID, 1, "in_progress")])
        self.assertEqual(
            self.reload_state().card_for_task(TASK_ID)["last_comment_id"], 101
        )


# ---------------------------------------------------------------------------
# Malformed and hostile replies
# ---------------------------------------------------------------------------


class RejectedReplyTests(OrchestratorTestCase):
    def assert_ignored(self, body, author=None):
        self.map_task()
        overrides = {} if author is None else {"author": author}
        self.transport.handler = lambda ref, after: (
            [comment(body, **overrides)],
            101,
        )

        counts = self.build().run_once()

        self.assertEqual(counts["applied"], 0)
        self.assertGreaterEqual(counts["skipped"], 1)
        self.assertEqual(self.dsh.updates, [])
        self.assertEqual(self.dsh.comments, [])
        self.assertEqual(self.reload_state().processed_messages, [])

    def test_bad_json_is_ignored_without_dsh_mutation(self):
        self.assert_ignored("this is not JSON, it is prose")

    def test_multiple_json_objects_are_rejected(self):
        self.assert_ignored('{"a": 1} {"b": 2}')

    def test_json_array_is_rejected(self):
        self.assert_ignored('["not", "an", "object"]')

    def test_wrong_author_is_ignored(self):
        self.assert_ignored(
            reply("task_update", state="in_progress", message_id=MESSAGE_ID_2),
            author="mallory",
        )

    def test_missing_author_is_ignored(self):
        self.assert_ignored(
            reply("task_update", state="in_progress", message_id=MESSAGE_ID_2),
            author="",
        )

    def test_wrong_card_is_ignored(self):
        self.assert_ignored(
            reply(
                "task_update",
                state="in_progress",
                card=OTHER_CARD,
                message_id=MESSAGE_ID_2,
            )
        )

    def test_wrong_task_id_is_ignored(self):
        self.assert_ignored(
            reply(
                "task_update",
                state="in_progress",
                task_id=OTHER_TASK_ID,
                message_id=MESSAGE_ID_2,
            )
        )

    def test_wrong_agent_is_ignored(self):
        self.assert_ignored(
            reply(
                "task_update",
                state="in_progress",
                agent="office",
                message_id=MESSAGE_ID_2,
            )
        )

    def test_task_request_is_not_a_reply(self):
        self.assert_ignored(
            reply(
                "task_request",
                title="not a reply",
                instructions="should be rejected",
                message_id=MESSAGE_ID_2,
            )
        )

    def test_task_update_with_done_state_is_rejected(self):
        self.assert_ignored(
            reply("task_update", state="done", message_id=MESSAGE_ID_2)
        )

    def test_reply_type_unknown_is_rejected(self):
        self.assert_ignored(
            reply("something_else", message_id=MESSAGE_ID_2)
        )

    def test_reply_with_unknown_field_is_rejected(self):
        self.assert_ignored(
            reply(
                "task_update",
                state="in_progress",
                project_id="dsh-kanban",
                message_id=MESSAGE_ID_2,
            )
        )

    def test_message_without_comment_id_is_ignored(self):
        self.map_task()
        message = comment(reply("ack", status="received", message_id=MESSAGE_ID_2))
        del message["id"]
        self.transport.handler = lambda ref, after: ([message], 101)

        counts = self.build().run_once()

        self.assertEqual(counts["skipped"], 1)
        self.assertEqual(self.reload_state().processed_messages, [])

    def test_fenced_json_is_accepted_with_surrounding_prose(self):
        self.map_task()
        payload = reply("ack", status="received", message_id=MESSAGE_ID_2)
        body = "Status update\n\n```json\n{}\n```\n\nThanks.".format(
            json.dumps(payload)
        )
        self.transport.handler = lambda ref, after: ([comment(body)], 101)

        counts = self.build().run_once()

        self.assertEqual(counts["applied"], 1)

    def test_bare_json_is_accepted(self):
        self.map_task()
        payload = reply("ack", status="received", message_id=MESSAGE_ID_2)
        self.transport.handler = lambda ref, after: (
            [comment(json.dumps(payload))],
            101,
        )

        counts = self.build().run_once()

        self.assertEqual(counts["applied"], 1)

    def test_object_embedded_in_prose_is_accepted(self):
        self.map_task()
        payload = reply("ack", status="received", message_id=MESSAGE_ID_2)
        body = "Here is my reply:\n{}\nThanks.".format(json.dumps(payload))
        self.transport.handler = lambda ref, after: ([comment(body)], 101)

        counts = self.build().run_once()

        self.assertEqual(counts["applied"], 1)

    def test_dict_author_login_is_accepted_case_insensitively(self):
        self.map_task()
        self.transport.handler = lambda ref, after: (
            [
                comment(
                    reply("ack", status="received", message_id=MESSAGE_ID_2),
                    author={"login": "Grok-Bot"},
                )
            ],
            101,
        )

        counts = self.build().run_once()

        self.assertEqual(counts["applied"], 1)


# ---------------------------------------------------------------------------
# DSH failures and persistence
# ---------------------------------------------------------------------------


class DshFailureTests(OrchestratorTestCase):
    def test_dsh_error_does_not_mark_the_message_processed(self):
        self.map_task()
        self.dsh.fail_on["update_status"] = DshError(409, "version conflict")
        self.transport.handler = lambda ref, after: (
            [
                comment(
                    reply(
                        "task_update",
                        state="in_progress",
                        message_id=MESSAGE_ID_2,
                    )
                )
            ],
            101,
        )

        counts = self.build().run_once()

        self.assertEqual(counts["applied"], 0)
        self.assertEqual(counts["errors"], 1)
        self.assertEqual(self.reload_state().processed_messages, [])
        self.assertEqual(
            self.reload_state().card_for_task(TASK_ID)["last_comment_id"], 0
        )

    def test_comment_failure_does_not_mark_the_message_processed(self):
        self.map_task()
        self.dsh.fail_on["comment"] = DshError(500, "comment failed")
        self.transport.handler = lambda ref, after: (
            [comment(reply("ack", status="received", message_id=MESSAGE_ID_2))],
            101,
        )

        # ack never writes to DSH, so use an update that does.
        self.transport.handler = lambda ref, after: (
            [
                comment(
                    reply(
                        "task_update",
                        state="in_progress",
                        message_id=MESSAGE_ID_2,
                    )
                )
            ],
            101,
        )
        counts = self.build().run_once()

        self.assertEqual(counts["errors"], 1)
        self.assertEqual(self.reload_state().processed_messages, [])
        self.assertEqual(
            self.reload_state().card_for_task(TASK_ID)["last_comment_id"], 0
        )

    def test_get_task_failure_while_applying_is_not_marked_processed(self):
        self.map_task()
        self.dsh.fail_on["get_task"] = DshError(500, "read failed")
        self.transport.handler = lambda ref, after: (
            [
                comment(
                    reply(
                        "task_complete",
                        state="done",
                        evidence=["done"],
                        message_id=MESSAGE_ID_2,
                    )
                )
            ],
            101,
        )

        counts = self.build().run_once()

        self.assertEqual(counts["errors"], 1)
        self.assertEqual(self.dsh.updates, [])
        self.assertEqual(self.dsh.comments, [])
        self.assertEqual(self.reload_state().processed_messages, [])
        self.assertEqual(self.transport.closed, [])

    def test_current_terminal_dsh_task_is_not_regressed(self):
        self.map_task()
        self.dsh.details[TASK_ID] = {
            "task": dict(task(status="done", version=4)),
            "comments": [],
            "activity": [],
            "attachments": [],
        }
        self.transport.handler = lambda ref, after: (
            [
                comment(
                    reply(
                        "task_update",
                        state="in_progress",
                        message_id=MESSAGE_ID_2,
                    )
                )
            ],
            101,
        )

        counts = self.build().run_once()

        self.assertEqual(counts["applied"], 0)
        self.assertEqual(self.dsh.updates, [])
        self.assertEqual(self.dsh.comments, [])

    def test_state_is_saved_after_each_applied_reply(self):
        self.map_task()
        first = comment(
            reply("ack", status="received", message_id=MESSAGE_ID_2),
            id=101,
        )
        second = comment(
            reply("ack", status="received", message_id=MESSAGE_ID_3),
            id=102,
        )
        self.transport.handler = lambda ref, after: ([first, second], 102)

        recording = RecordingState(self.state)
        self.build(state=recording).run_once()

        self.assertGreaterEqual(len(recording.saves), 2)
        partial = [
            snapshot
            for snapshot in recording.saves
            if snapshot["processed_messages"] == ["dsh-grokbot-v1:" + MESSAGE_ID_2]
        ]
        self.assertTrue(partial, recording.saves)
        self.assertEqual(
            partial[0]["cards"][TASK_ID]["last_comment_id"], 101
        )

        saved = self.reload_state()
        self.assertEqual(
            saved.processed_messages,
            [
                "dsh-grokbot-v1:" + MESSAGE_ID_2,
                "dsh-grokbot-v1:" + MESSAGE_ID_3,
            ],
        )
        self.assertEqual(saved.card_for_task(TASK_ID)["last_comment_id"], 102)

    def test_mapping_without_an_issue_reference_is_not_polled(self):
        self.state.upsert_card(
            TASK_ID,
            run_uuid=RUN_UUID,
            last_comment_id=0,
            last_status="submitted",
        )
        self.state.save()

        counts = self.build().run_once()

        self.assertEqual(counts["errors"], 1)
        self.assertEqual(self.transport.calls, [])

    def test_one_error_does_not_stop_the_next_mapping(self):
        self.map_task()
        other = task(id=OTHER_TASK_ID, identifier=OTHER_CARD)
        self.dsh.board_data = board([task(), other])
        self.state.upsert_card(
            OTHER_TASK_ID,
            issue=13,
            run_uuid="other-run",
            last_comment_id=0,
            last_status="submitted",
        )
        self.state.save()
        self.dsh.fail_on["get_task"] = DshError(500, "read failed")

        def handler(ref, after):
            if ref == "13":
                payload = reply(
                    "task_update",
                    state="in_progress",
                    card=OTHER_CARD,
                    task_id=OTHER_TASK_ID,
                    message_id=MESSAGE_ID_3,
                )
            else:
                payload = reply(
                    "task_update",
                    state="in_progress",
                    message_id=MESSAGE_ID_2,
                )
            return [comment(payload)], 101

        self.transport.handler = handler

        counts = self.build().run_once()

        self.assertEqual(counts["errors"], 2)
        self.assertEqual(self.dsh.updates, [])


class ConstructorTests(OrchestratorTestCase):
    def test_blank_builder_agent_is_rejected(self):
        with self.assertRaises(ValueError):
            self.build(builder_agent="  ")

    def test_allowed_logins_must_not_be_a_bare_string(self):
        with self.assertRaises(ValueError):
            self.build(allowed_logins="grok-bot")

    def test_allowed_logins_entries_must_be_non_empty_strings(self):
        with self.assertRaises(ValueError):
            self.build(allowed_logins=["", GROK_LOGIN])

    def test_new_id_must_be_callable(self):
        with self.assertRaises(ValueError):
            self.build(new_id="not-callable")

    def test_blank_new_id_result_is_an_error(self):
        counts = self.build(new_id=lambda: "   ").run_once()

        self.assertEqual(counts["opened"], 0)
        self.assertEqual(counts["errors"], 1)
        self.assertEqual(self.transport.opened, [])


class DependencyTests(unittest.TestCase):
    def test_orchestrator_imports_no_adapter_module(self):
        with open(orchestrator_module.__file__, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())

        names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    base = "bridge.{}".format(base) if base else "bridge"
                if base:
                    names.append(base)
                names.extend(
                    "{}.{}".format(base, alias.name) if base else alias.name
                    for alias in node.names
                )

        forbidden_roots = (
            "bridge.dsh",
            "bridge.github",
            "bridge.github_transport",
            "bridge.routine",
            "bridge.main",
        )
        forbidden = [
            name
            for name in names
            if any(
                name == root or name.startswith(root + ".")
                for root in forbidden_roots
            )
        ]
        self.assertEqual(forbidden, [], "forbidden imports: {}".format(forbidden))


if __name__ == "__main__":
    unittest.main()
