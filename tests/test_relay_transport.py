"""Black-box tests for the HTTP long-poll relay transport (M1.5b task R2).

Every test talks to a real HTTP server bound to an ephemeral loopback port.
The server is implemented in this file, records each request, and replays
scripted responses, so the transport is exercised through ``urllib.request``
without any external network access.
"""

import ast
import json
import socket
import sys
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import bridge.relay_transport as relay_module
from bridge.relay_transport import RelayError, RelayTransport
from bridge.transport import Transport, TransportError

TOKEN = "channel-token-1"

REQUEST = {
    "bridge": "dsh-grokbot-v1",
    "message_id": "11111111-1111-4111-8111-111111111111",
    "from": "dsh",
    "to": "grok",
    "card": "TEST-004",
    "task_id": "440e9040-1657-45bf-a945-0cadc6cabec0",
    "type": "task_request",
    "agent": "builder",
    "title": "Add a relay transport",
    "instructions": "Implement the transport and its tests.",
}

ACK = {
    "bridge": "dsh-grokbot-v1",
    "message_id": "22222222-2222-4222-8222-222222222222",
    "from": "grok",
    "to": "dsh",
    "card": "TEST-004",
    "task_id": "440e9040-1657-45bf-a945-0cadc6cabec0",
    "type": "ack",
    "agent": "builder",
    "status": "received",
}

SECOND_ACK = dict(
    ACK,
    message_id="33333333-3333-4333-8333-333333333333",
    note="second reply",
)

FORBIDDEN_IMPORTS = (
    "bridge.github_transport",
    "bridge.dsh",
    "bridge.routine",
    "bridge.orchestrator",
    "bridge.state",
    "bridge.envelope",
    "relay",
)


class FakeRelay:
    """Records requests and replays scripted responses in order."""

    def __init__(self):
        self.requests = []
        self.responses = []
        self.delay = 0.0
        self._lock = threading.Lock()

    def push(self, status, payload):
        with self._lock:
            self.responses.append((status, payload))

    def next_response(self):
        with self._lock:
            if self.responses:
                return self.responses.pop(0)
            return 200, {"messages": [], "next": 0}

    def record(self, request):
        with self._lock:
            self.requests.append(request)


class _RelayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def _handle(self, method):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        parts = urllib.parse.urlsplit(self.path)
        request = {
            "method": method,
            "path": parts.path,
            "query": urllib.parse.parse_qs(parts.query),
            "headers": {
                name.lower(): value for name, value in self.headers.items()
            },
            "body": body,
        }

        relay = self.server.relay
        relay.record(request)
        status, payload = relay.next_response()

        if isinstance(payload, (dict, list)):
            data = json.dumps(payload).encode("utf-8")
        elif isinstance(payload, str):
            data = payload.encode("utf-8")
        else:
            data = payload

        if relay.delay:
            time.sleep(relay.delay)

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format, *args):
        """Keep test output free of per-request access logs."""


class _RelayServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        """Ignore clients that disconnected after timing out."""
        error = sys.exc_info()[1]
        if isinstance(
            error,
            (BrokenPipeError, ConnectionResetError, ConnectionAbortedError),
        ):
            return
        super().handle_error(request, client_address)


class RelayTestCase(unittest.TestCase):
    """Starts one fake relay per test and provides request helpers."""

    def setUp(self):
        self.relay = FakeRelay()
        self.server = _RelayServer(("127.0.0.1", 0), _RelayHandler)
        self.server.relay = self.relay
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self.thread.start()
        self.addCleanup(self._stop_server)
        self.base_url = "http://127.0.0.1:{}".format(
            self.server.server_address[1]
        )
        self.transport = RelayTransport(self.base_url, "1", TOKEN)

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2.0)

    def push(self, status, payload):
        self.relay.push(status, payload)

    @property
    def last_request(self):
        self.assertTrue(self.relay.requests, "transport made no HTTP request")
        return self.relay.requests[-1]

    def last_query(self):
        return self.last_request["query"]


class RelayTransportConstructorTests(unittest.TestCase):
    def test_implements_the_transport_interface(self):
        transport = RelayTransport("http://127.0.0.1:9999", "1", TOKEN)

        self.assertIsInstance(transport, Transport)
        self.assertEqual(RelayTransport.__abstractmethods__, frozenset())
        self.assertEqual(transport.base_url, "http://127.0.0.1:9999")
        self.assertEqual(transport.channel, "1")
        self.assertEqual(transport.timeout, 30.0)

    def test_normalizes_the_base_url_and_the_numeric_channel(self):
        transport = RelayTransport(
            "https://relay.example/bridge/", "007", TOKEN
        )

        self.assertEqual(transport.base_url, "https://relay.example/bridge")
        self.assertEqual(transport.channel, "7")

    def test_repr_does_not_leak_the_token(self):
        text = repr(RelayTransport("http://relay.example", "1", TOKEN))

        self.assertIn("relay.example", text)
        self.assertNotIn(TOKEN, text)

    def test_rejects_a_bad_base_url(self):
        values = (
            None,
            "",
            "   ",
            "relay.example",
            "ftp://relay.example",
            "http://",
            "http://relay.example/?x=1",
            "http://relay.example/a b",
        )
        for value in values:
            with self.subTest(base_url=value):
                with self.assertRaises(ValueError):
                    RelayTransport(value, "1", TOKEN)

    def test_rejects_a_bad_channel(self):
        values = (None, "", "   ", "abc", "0", "-1", "1.5", 1)
        for value in values:
            with self.subTest(channel=value):
                with self.assertRaises(ValueError):
                    RelayTransport("http://relay.example", value, TOKEN)

    def test_rejects_a_bad_token(self):
        values = (None, "", "   ", "a\nb", 1)
        for value in values:
            with self.subTest(token=value):
                with self.assertRaises(ValueError):
                    RelayTransport("http://relay.example", "1", value)

    def test_rejects_a_bad_timeout(self):
        values = (0, -1, True, "30", None, float("nan"), float("inf"))
        for value in values:
            with self.subTest(timeout=value):
                with self.assertRaises(ValueError):
                    RelayTransport(
                        "http://relay.example", "1", TOKEN, timeout=value
                    )

    def test_accepts_a_positive_timeout_as_float(self):
        transport = RelayTransport(
            "http://relay.example", "1", TOKEN, timeout=5
        )

        self.assertEqual(transport.timeout, 5.0)


class RelayOpenTests(RelayTestCase):
    def test_open_posts_the_envelope_and_returns_the_channel(self):
        self.push(201, {"seq": 7})

        ref = self.transport.open(REQUEST)

        self.assertEqual(ref, "1")
        request = self.last_request
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["path"], "/v1/channels/1/messages")
        self.assertEqual(request["query"], {})
        self.assertEqual(
            request["headers"]["authorization"], "Bearer " + TOKEN
        )
        self.assertEqual(
            request["headers"]["content-type"], "application/json"
        )
        self.assertEqual(
            json.loads(request["body"].decode("utf-8")), REQUEST
        )

    def test_open_does_not_require_a_positive_sequence(self):
        self.push(201, {"seq": 0})

        self.assertEqual(self.transport.open(REQUEST), "1")

    def test_open_keeps_a_path_prefix_and_trims_trailing_slashes(self):
        transport = RelayTransport(
            self.base_url + "/bridge/", "1", TOKEN
        )
        self.push(201, {"seq": 1})

        self.assertEqual(transport.open(REQUEST), "1")
        self.assertEqual(
            self.last_request["path"], "/bridge/v1/channels/1/messages"
        )

    def test_open_rejects_a_non_object_request(self):
        for request in (None, [], "x", 1):
            with self.subTest(request=request):
                with self.assertRaises(ValueError):
                    self.transport.open(request)

        self.assertEqual(self.relay.requests, [])

    def test_open_rejects_a_request_that_is_not_json_serializable(self):
        with self.assertRaises(ValueError):
            self.transport.open({"bad": object()})

        self.assertEqual(self.relay.requests, [])


class RelayRepliesTests(RelayTestCase):
    def test_replies_long_polls_and_returns_envelopes_and_cursor(self):
        self.push(
            200,
            {
                "messages": [
                    {"seq": 4, "envelope": ACK},
                    {"seq": 5, "envelope": SECOND_ACK},
                ],
                "next": 5,
            },
        )

        messages, cursor = self.transport.replies("1")

        self.assertEqual(messages, [ACK, SECOND_ACK])
        self.assertEqual(cursor, 5)
        self.assertIsInstance(cursor, int)
        request = self.last_request
        self.assertEqual(request["method"], "GET")
        self.assertEqual(request["path"], "/v1/channels/1/messages")
        self.assertEqual(request["query"]["after"], ["0"])
        self.assertEqual(request["query"]["wait"], ["25"])
        self.assertEqual(
            request["headers"]["authorization"], "Bearer " + TOKEN
        )

    def test_replies_sends_the_after_cursor(self):
        self.push(
            200,
            {"messages": [{"seq": 6, "envelope": ACK}], "next": 6},
        )

        messages, cursor = self.transport.replies("1", after=5)

        self.assertEqual(messages, [ACK])
        self.assertEqual(cursor, 6)
        self.assertEqual(self.last_query()["after"], ["5"])

    def test_replies_accepts_a_numeric_string_cursor(self):
        self.push(200, {"messages": [], "next": 4})

        messages, cursor = self.transport.replies("1", after="4")

        self.assertEqual(messages, [])
        self.assertEqual(cursor, "4")
        self.assertEqual(self.last_query()["after"], ["4"])

    def test_empty_poll_returns_the_after_cursor_unchanged(self):
        self.push(200, {"messages": [], "next": 3})

        self.assertEqual(self.transport.replies("1", after=3), ([], 3))

        self.push(200, {"messages": [], "next": 0})

        self.assertEqual(self.transport.replies("1"), ([], None))

    def test_wait_is_capped_at_the_relay_long_poll_limit(self):
        transport = RelayTransport(
            self.base_url, "1", TOKEN, timeout=100.0
        )
        self.push(200, {"messages": [], "next": 0})

        transport.replies("1")

        self.assertEqual(self.last_query()["wait"], ["25"])

    def test_wait_shortens_with_a_short_timeout(self):
        transport = RelayTransport(self.base_url, "1", TOKEN, timeout=10.0)
        self.push(200, {"messages": [], "next": 0})

        transport.replies("1")

        self.assertEqual(self.last_query()["wait"], ["9"])

    def test_wait_is_zero_for_sub_second_timeouts(self):
        transport = RelayTransport(self.base_url, "1", TOKEN, timeout=0.5)
        self.push(200, {"messages": [], "next": 0})

        transport.replies("1")

        self.assertEqual(self.last_query()["wait"], ["0"])

    def test_replies_rejects_a_bad_reference_or_cursor(self):
        for ref in ("", "abc", "0", None):
            with self.subTest(ref=ref):
                with self.assertRaises(ValueError):
                    self.transport.replies(ref)

        for after in (-1, True, 1.5, "abc", [], object()):
            with self.subTest(after=after):
                with self.assertRaises(ValueError):
                    self.transport.replies("1", after=after)

        self.assertEqual(self.relay.requests, [])

    def test_malformed_reply_documents_raise_transport_error(self):
        payloads = (
            b"",
            b"not json",
            [],
            {},
            {"messages": "x", "next": 1},
            {"messages": [], "next": "1"},
            {"messages": [], "next": True},
            {"messages": [], "next": -1},
            {"messages": [1], "next": 1},
            {"messages": [{"seq": 1}], "next": 1},
            {"messages": [{"seq": 1, "envelope": []}], "next": 1},
            {"messages": [{"envelope": {}}], "next": 1},
            {"messages": [{"seq": "1", "envelope": {}}], "next": 1},
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                self.push(200, payload)

                with self.assertRaises(TransportError):
                    self.transport.replies("1")


class RelayFailureTests(RelayTestCase):
    def test_http_error_statuses_raise_transport_error(self):
        for status in (400, 401, 403, 404, 405, 413, 500, 503):
            with self.subTest(status=status):
                self.push(status, {"error": "failed"})

                with self.assertRaises(TransportError):
                    self.transport.open(REQUEST)

            with self.subTest(status=status, operation="replies"):
                self.push(status, {"error": "failed"})

                with self.assertRaises(TransportError):
                    self.transport.replies("1")

    def test_http_error_reports_status_without_leaking_the_token(self):
        self.push(401, {"error": "unauthorized"})

        with self.assertRaises(RelayError) as raised:
            self.transport.open(REQUEST)

        message = str(raised.exception)
        self.assertIn("401", message)
        self.assertNotIn(TOKEN, message)

    def test_malformed_open_documents_raise_transport_error(self):
        payloads = (
            b"",
            b"not json",
            [],
            {},
            {"seq": "1"},
            {"seq": True},
            {"seq": -1},
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                self.push(201, payload)

                with self.assertRaises(TransportError):
                    self.transport.open(REQUEST)

    def test_connection_refused_raises_transport_error(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        transport = RelayTransport(
            "http://127.0.0.1:{}".format(port), "1", TOKEN, timeout=1.0
        )

        with self.assertRaises(TransportError):
            transport.open(REQUEST)

    def test_socket_timeout_raises_transport_error(self):
        self.relay.delay = 0.5
        transport = RelayTransport(
            self.base_url, "1", TOKEN, timeout=0.15
        )

        with self.assertRaises(TransportError):
            transport.open(REQUEST)


class RelayCloseTests(RelayTestCase):
    def test_close_is_a_noop_and_makes_no_request(self):
        self.assertIsNone(self.transport.close("1"))
        self.assertEqual(self.relay.requests, [])

    def test_close_validates_the_reference(self):
        for ref in ("", "abc", "0", None):
            with self.subTest(ref=ref):
                with self.assertRaises(ValueError):
                    self.transport.close(ref)


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


class RelayDependencyTests(unittest.TestCase):
    def test_relay_transport_depends_only_on_the_transport_boundary(self):
        with open(relay_module.__file__, encoding="utf-8") as handle:
            names = imported_names(handle.read())

        forbidden = [
            name
            for name in names
            if any(
                name == root or name.startswith(root + ".")
                for root in FORBIDDEN_IMPORTS
            )
        ]
        self.assertEqual(
            forbidden, [], "forbidden imports: {}".format(forbidden)
        )
        self.assertIn("bridge.transport", names)


if __name__ == "__main__":
    unittest.main()
