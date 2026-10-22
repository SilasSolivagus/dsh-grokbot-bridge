"""Tests for the M1.5 transport interface.

A transport owns the wire details of the bridge (an issue comment thread, a
relay queue, or an in-memory fake). These tests pin the interface only; they
do not exercise a real adapter.
"""

import ast
import inspect
import unittest

import bridge.transport as transport_module
from bridge.transport import Transport, TransportError

REQUEST = {
    "bridge": "dsh-grokbot-v1",
    "message_id": "11111111-1111-4111-8111-111111111111",
    "from": "掌柜",
    "to": "Origin",
    "card": "TEST-004",
    "task_id": "440e9040-1657-45bf-a945-0cadc6cabec0",
    "type": "task_request",
    "agent": "掌柜",
    "title": "Add a transport interface",
    "instructions": "Implement the interface and its tests.",
}

REPLY = {
    "bridge": "dsh-grokbot-v1",
    "message_id": "22222222-2222-4222-8222-222222222222",
    "from": "Origin",
    "to": "掌柜",
    "card": "TEST-004",
    "task_id": "440e9040-1657-45bf-a945-0cadc6cabec0",
    "type": "ack",
    "agent": "Origin",
    "status": "received",
}

SECOND_REPLY = dict(REPLY, message_id="33333333-3333-4333-8333-333333333333")

FORBIDDEN_IMPORTS = (
    "bridge.github",
    "bridge.dsh",
    "bridge.routine",
    "bridge.orchestrator",
)


class FakeTransport(Transport):
    """In-memory transport used to exercise the interface contract."""

    def __init__(self, pages=None):
        self.opened = []
        self.closed = []
        self.calls = []
        self.pages = {
            key: (list(messages), cursor)
            for key, (messages, cursor) in (pages or {}).items()
        }

    def open(self, request):
        self.opened.append(request)
        return "ref-{}".format(len(self.opened))

    def replies(self, ref, after=None):
        self.calls.append((ref, after))
        messages, cursor = self.pages.get((ref, after), ([], None))
        return list(messages), cursor

    def close(self, ref):
        self.closed.append(ref)


class FailingTransport(Transport):
    """Transport whose adapter operations fail with a fixed error."""

    def __init__(self, error):
        self.error = error

    def open(self, request):
        raise self.error

    def replies(self, ref, after=None):
        raise self.error

    def close(self, ref):
        raise self.error


def imported_names(source):
    """Return every module name imported by ``source``, including relative."""
    names = []
    for node in ast.walk(ast.parse(source)):
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
    return names


class TransportAbcTests(unittest.TestCase):
    def test_transport_is_an_abstract_base_class(self):
        self.assertTrue(inspect.isabstract(Transport))
        with self.assertRaises(TypeError) as raised:
            Transport()
        self.assertIn("abstract", str(raised.exception).lower())

    def test_abstract_methods_are_open_replies_and_close(self):
        self.assertEqual(
            Transport.__abstractmethods__,
            frozenset({"open", "replies", "close"}),
        )

    def test_subclass_missing_an_abstract_method_cannot_be_instantiated(self):
        class MissingClose(Transport):
            def open(self, request):
                return "ref-1"

            def replies(self, ref, after=None):
                return [], None

        with self.assertRaises(TypeError):
            MissingClose()

    def test_abstract_methods_called_directly_raise_not_implemented(self):
        fake = FakeTransport()
        calls = (
            ("open", (REQUEST,)),
            ("replies", ("ref-1",)),
            ("close", ("ref-1",)),
        )
        for name, args in calls:
            with self.subTest(method=name):
                with self.assertRaises(NotImplementedError):
                    getattr(Transport, name)(fake, *args)

    def test_interface_method_signatures(self):
        open_params = inspect.signature(Transport.open).parameters
        self.assertEqual(list(open_params), ["self", "request"])

        reply_params = inspect.signature(Transport.replies).parameters
        self.assertEqual(list(reply_params), ["self", "ref", "after"])
        self.assertIsNone(reply_params["after"].default)

        close_params = inspect.signature(Transport.close).parameters
        self.assertEqual(list(close_params), ["self", "ref"])


class FakeTransportContractTests(unittest.TestCase):
    def test_fake_transport_can_open_read_replies_and_close(self):
        transport = FakeTransport(
            pages={("ref-1", None): ([REPLY], "comment-1")}
        )

        ref = transport.open(REQUEST)
        messages, next_cursor = transport.replies(ref)
        transport.close(ref)

        self.assertEqual(ref, "ref-1")
        self.assertEqual(transport.opened, [REQUEST])
        self.assertEqual(messages, [REPLY])
        self.assertEqual(next_cursor, "comment-1")
        self.assertEqual(transport.closed, ["ref-1"])

    def test_open_returns_a_non_empty_string_reference(self):
        ref = FakeTransport().open(REQUEST)
        self.assertIsInstance(ref, str)
        self.assertTrue(ref)

    def test_replies_returns_messages_and_next_cursor(self):
        transport = FakeTransport(
            pages={("ref-1", None): ([REPLY], "comment-1")}
        )

        result = transport.replies("ref-1")

        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)
        messages, next_cursor = result
        self.assertIsInstance(messages, list)
        self.assertIsInstance(messages[0], dict)
        self.assertIsInstance(next_cursor, str)

    def test_replies_defaults_after_to_none(self):
        transport = FakeTransport()

        transport.replies("ref-1")

        self.assertEqual(transport.calls, [("ref-1", None)])

    def test_replies_after_cursor_returns_only_new_messages(self):
        transport = FakeTransport(
            pages={
                ("ref-1", None): ([REPLY], "comment-1"),
                ("ref-1", "comment-1"): ([SECOND_REPLY], "comment-2"),
            }
        )

        first_messages, first_cursor = transport.replies("ref-1")
        second_messages, second_cursor = transport.replies(
            "ref-1", after=first_cursor
        )

        self.assertEqual(first_messages, [REPLY])
        self.assertEqual(second_messages, [SECOND_REPLY])
        self.assertEqual(second_cursor, "comment-2")
        self.assertEqual(
            transport.calls,
            [("ref-1", None), ("ref-1", "comment-1")],
        )

    def test_replies_may_return_an_empty_list_and_no_cursor(self):
        transport = FakeTransport(
            pages={("ref-1", "comment-2"): ([], None)}
        )

        messages, next_cursor = transport.replies("ref-1", after="comment-2")

        self.assertEqual(messages, [])
        self.assertIsNone(next_cursor)

    def test_close_receives_the_reference(self):
        transport = FakeTransport()

        transport.close("ref-9")

        self.assertEqual(transport.closed, ["ref-9"])

    def test_adapter_failure_raises_transport_error(self):
        failure = TransportError("relay unavailable")
        transport = FailingTransport(failure)

        with self.assertRaises(TransportError) as raised:
            transport.open(REQUEST)

        self.assertIs(raised.exception, failure)


class TransportErrorTests(unittest.TestCase):
    def test_transport_error_is_an_exception(self):
        self.assertTrue(issubclass(TransportError, Exception))

        error = TransportError("relay unavailable")

        self.assertEqual(str(error), "relay unavailable")
        with self.assertRaises(TransportError):
            raise error


class TransportDependencyTests(unittest.TestCase):
    def test_transport_module_does_not_import_adapters_or_orchestrator(self):
        with open(transport_module.__file__, encoding="utf-8") as handle:
            names = imported_names(handle.read())

        forbidden = [
            name
            for name in names
            if any(
                name == root or name.startswith(root + ".")
                for root in FORBIDDEN_IMPORTS
            )
        ]
        self.assertEqual(forbidden, [], "forbidden imports: {}".format(forbidden))


if __name__ == "__main__":
    unittest.main()
