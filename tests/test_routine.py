"""Tests for the Grok routine webhook client used by the M1.5 bridge.

Every test talks to a local ``http.server`` fake started on ``127.0.0.1``;
nothing here touches a live Grok routine.

The official webhook accepts a JSON body and answers ``{"runUuid": "..."}``.
It has no completion callback, so ``wake`` only reports that the routine was
accepted.
"""

import json
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bridge.routine import RoutineClient, RoutineError

KEY = "crsr_secret_key_123"
RUN_UUID = "9f8c1f4e-2b7a-4d55-9a8e-3c6b0e1d2f3a"

REQUEST = {
    "bridge": "dsh-grokbot-v1",
    "type": "task_request",
    "card": "TEST-004",
    "title": "Add routine client",
    "instructions": "Implement the webhook client.",
}


class FakeRoutineServer:
    """A tiny HTTP server that records requests and replays one response."""

    def __init__(self):
        self._lock = threading.Lock()
        self._requests = []
        self._status = 200
        self._body = b"{}"
        self._content_type = "application/json"
        self._declared_length = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):  # keep test output clean
                pass

            def do_GET(self):
                owner._handle(self)

            def do_POST(self):
                owner._handle(self)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self._thread.start()

    @property
    def webhook_url(self):
        host, port = self._httpd.server_address[:2]
        return "http://{}:{}/hooks/routine-1".format(host, port)

    @property
    def requests(self):
        with self._lock:
            return list(self._requests)

    def respond_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.respond_raw(body, status=status, content_type="application/json")

    def respond_raw(
        self,
        body,
        status=200,
        content_type="application/json",
        declared_length=None,
    ):
        with self._lock:
            self._status = status
            self._body = body
            self._content_type = content_type
            self._declared_length = declared_length

    def _handle(self, handler):
        length = int(handler.headers.get("Content-Length") or 0)
        raw = handler.rfile.read(length) if length else b""
        record = {
            "method": handler.command,
            "path": handler.path,
            "headers": {
                key.lower(): value for key, value in handler.headers.items()
            },
            "body": raw,
        }
        with self._lock:
            self._requests.append(record)
            status = self._status
            body = self._body
            content_type = self._content_type
            declared_length = self._declared_length
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        length = len(body) if declared_length is None else declared_length
        handler.send_header("Content-Length", str(length))
        handler.end_headers()
        if body:
            handler.wfile.write(body)

    def close(self):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


class RoutineTestCase(unittest.TestCase):
    """Base class with a fake webhook server and a configured client."""

    def setUp(self):
        self.server = FakeRoutineServer()
        self.addCleanup(self.server.close)
        self.client = self.new_client()

    def new_client(self, **kwargs):
        kwargs.setdefault("timeout", 5.0)
        return RoutineClient(self.server.webhook_url, KEY, **kwargs)

    def only_request(self):
        requests = self.server.requests
        self.assertEqual(len(requests), 1, requests)
        return requests[0]

    def json_body(self, request):
        return json.loads(request["body"].decode("utf-8"))


class ConfigTests(unittest.TestCase):
    def test_webhook_url_is_required(self):
        for value in ("", "   ", None, 3):
            with self.subTest(webhook_url=value):
                with self.assertRaises(RoutineError) as caught:
                    RoutineClient(value, KEY)
                self.assertIn("webhook_url", str(caught.exception))

    def test_key_is_required(self):
        for value in ("", "   ", None, 3):
            with self.subTest(key=value):
                with self.assertRaises(RoutineError) as caught:
                    RoutineClient("http://127.0.0.1:1/hook", value)
                self.assertIn("key", str(caught.exception))

    def test_webhook_url_must_be_absolute_http(self):
        for value in (
            "ftp://127.0.0.1/hook",
            "127.0.0.1:8080/hook",
            "http:///hook",
            "/relative/hook",
        ):
            with self.subTest(webhook_url=value):
                with self.assertRaises(RoutineError):
                    RoutineClient(value, KEY)

    def test_webhook_url_must_not_have_a_fragment(self):
        with self.assertRaises(RoutineError):
            RoutineClient("http://127.0.0.1:1/hook#fragment", KEY)

    def test_webhook_url_allows_a_query_string(self):
        client = RoutineClient(
            "http://127.0.0.1:1/hook?token=abc", KEY, timeout=5.0
        )

        self.assertEqual(
            client.webhook_url, "http://127.0.0.1:1/hook?token=abc"
        )

    def test_timeout_must_be_a_positive_number(self):
        for value in (0, -1, "5", True, None):
            with self.subTest(timeout=value):
                with self.assertRaises(RoutineError):
                    RoutineClient(
                        "http://127.0.0.1:1/hook", KEY, timeout=value
                    )

    def test_surrounding_key_whitespace_is_ignored(self):
        client = RoutineClient(
            "http://127.0.0.1:1/hook", "  " + KEY + "\n", timeout=5.0
        )

        self.assertEqual(client.key, KEY)

    def test_key_must_be_printable_ascii(self):
        for value in ("two words", "line\nbreak", "tab\tkey", "密钥"):
            with self.subTest(key=repr(value)):
                with self.assertRaises(RoutineError) as caught:
                    RoutineClient("http://127.0.0.1:1/hook", value)
                self.assertNotIn(value, str(caught.exception))

    def test_repr_does_not_expose_the_key(self):
        client = RoutineClient("http://127.0.0.1:1/hook", KEY)

        self.assertNotIn(KEY, repr(client))
        self.assertIn("RoutineClient", repr(client))

    def test_repr_does_not_expose_a_key_embedded_in_the_url(self):
        url = "http://127.0.0.1:1/hook?token=" + KEY
        client = RoutineClient(url, KEY)

        self.assertNotIn(KEY, repr(client))
        self.assertIn("RoutineClient", repr(client))


class WakeRequestTests(RoutineTestCase):
    def test_wake_posts_to_the_webhook_url(self):
        self.server.respond_json({"runUuid": RUN_UUID})

        self.client.wake(REQUEST)

        request = self.only_request()
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["path"], "/hooks/routine-1")

    def test_wake_sends_bearer_key(self):
        self.server.respond_json({"runUuid": RUN_UUID})

        self.client.wake(REQUEST)

        request = self.only_request()
        self.assertEqual(
            request["headers"]["authorization"], "Bearer " + KEY
        )

    def test_wake_sends_json_content_type(self):
        self.server.respond_json({"runUuid": RUN_UUID})

        self.client.wake(REQUEST)

        request = self.only_request()
        self.assertEqual(
            request["headers"]["content-type"], "application/json"
        )

    def test_wake_sends_the_request_as_the_json_body(self):
        self.server.respond_json({"runUuid": RUN_UUID})

        self.client.wake(REQUEST)

        self.assertEqual(self.json_body(self.only_request()), REQUEST)

    def test_wake_sends_utf8_without_ascii_escaping(self):
        self.server.respond_json({"runUuid": RUN_UUID})
        request = dict(REQUEST, title="第一行\nsecond line")

        self.client.wake(request)

        raw = self.only_request()["body"]
        self.assertIn("第一行".encode("utf-8"), raw)

    def test_wake_returns_the_run_uuid(self):
        self.server.respond_json({"runUuid": RUN_UUID})

        self.assertEqual(self.client.wake(REQUEST), {"runUuid": RUN_UUID})

    def test_wake_reports_only_the_run_uuid(self):
        self.server.respond_json(
            {"runUuid": RUN_UUID, "status": "accepted"}
        )

        self.assertEqual(self.client.wake(REQUEST), {"runUuid": RUN_UUID})

    def test_wake_trims_the_run_uuid(self):
        self.server.respond_json({"runUuid": "  " + RUN_UUID + "\n"})

        self.assertEqual(self.client.wake(REQUEST), {"runUuid": RUN_UUID})

    def test_wake_accepts_other_2xx_statuses(self):
        self.server.respond_json({"runUuid": RUN_UUID}, status=202)

        self.assertEqual(self.client.wake(REQUEST), {"runUuid": RUN_UUID})

    def test_wake_sends_an_empty_object(self):
        self.server.respond_json({"runUuid": RUN_UUID})

        self.client.wake({})

        self.assertEqual(self.json_body(self.only_request()), {})

    def test_wake_keeps_a_trailing_slash_in_the_webhook_url(self):
        self.server.respond_json({"runUuid": RUN_UUID})
        client = RoutineClient(
            self.server.webhook_url + "/", KEY, timeout=5.0
        )

        client.wake(REQUEST)

        self.assertEqual(self.only_request()["path"], "/hooks/routine-1/")

    def test_wake_keeps_a_query_string_in_the_webhook_url(self):
        self.server.respond_json({"runUuid": RUN_UUID})
        client = RoutineClient(
            self.server.webhook_url + "?channel=bridge", KEY, timeout=5.0
        )

        client.wake(REQUEST)

        self.assertEqual(
            self.only_request()["path"],
            "/hooks/routine-1?channel=bridge",
        )

    def test_wake_makes_exactly_one_request(self):
        self.server.respond_json({"runUuid": RUN_UUID})

        self.client.wake(REQUEST)

        self.assertEqual(len(self.server.requests), 1)


class HttpErrorTests(RoutineTestCase):
    def test_non_2xx_raises_routine_error_with_status_and_text(self):
        for status in (400, 401, 403, 500):
            with self.subTest(status=status):
                body = "webhook rejected {}".format(status)
                self.server.respond_raw(body.encode("utf-8"), status=status)

                with self.assertRaises(RoutineError) as caught:
                    self.client.wake(REQUEST)

                error = caught.exception
                self.assertEqual(error.status, status)
                self.assertEqual(error.method, "POST")
                self.assertIn(body, error.text)
                self.assertIn(body, str(error))
                self.assertIn(str(status), str(error))

    def test_http_error_does_not_leak_the_key_from_the_response_body(self):
        self.server.respond_raw(
            ("bad token: " + KEY).encode("utf-8"), status=401
        )

        with self.assertRaises(RoutineError) as caught:
            self.client.wake(REQUEST)

        error = caught.exception
        self.assertNotIn(KEY, str(error))
        self.assertNotIn(KEY, error.text)
        self.assertNotIn(KEY, repr(error))
        self.assertIn("[redacted]", error.text)

    def test_error_text_is_decoded_as_utf8(self):
        self.server.respond_json({"error": "令牌无效"}, status=403)

        with self.assertRaises(RoutineError) as caught:
            self.client.wake(REQUEST)

        self.assertEqual(caught.exception.status, 403)
        self.assertIn("令牌无效", caught.exception.text)
        self.assertIn("令牌无效", str(caught.exception))

    def test_error_text_survives_non_utf8_bytes(self):
        self.server.respond_raw(b"\xff\xfe", status=400)

        with self.assertRaises(RoutineError) as caught:
            self.client.wake(REQUEST)

        self.assertEqual(caught.exception.status, 400)
        self.assertIsInstance(caught.exception.text, str)

    def test_http_error_keeps_the_response_text_for_diagnostics(self):
        self.server.respond_json(
            {"error": {"code": "UNAUTHORIZED", "message": "bad key"}},
            status=401,
        )

        with self.assertRaises(RoutineError) as caught:
            self.client.wake(REQUEST)

        self.assertIn("UNAUTHORIZED", caught.exception.text)


class MalformedResponseTests(RoutineTestCase):
    def test_invalid_json_raises_routine_error(self):
        self.server.respond_raw(b"not json")

        with self.assertRaises(RoutineError) as caught:
            self.client.wake(REQUEST)

        error = caught.exception
        self.assertEqual(error.status, 200)
        self.assertEqual(error.text, "not json")
        self.assertIn("valid JSON", str(error))

    def test_json_array_raises_routine_error(self):
        self.server.respond_raw(b"[]")

        with self.assertRaises(RoutineError) as caught:
            self.client.wake(REQUEST)

        self.assertIn("JSON object", str(caught.exception))

    def test_empty_success_body_raises_routine_error(self):
        self.server.respond_raw(b"")

        with self.assertRaises(RoutineError) as caught:
            self.client.wake(REQUEST)

        error = caught.exception
        self.assertEqual(error.status, 200)
        self.assertEqual(error.text, "")
        self.assertIn("empty response body", str(error))

    def test_missing_run_uuid_raises_routine_error(self):
        self.server.respond_json({"ok": True})

        with self.assertRaises(RoutineError) as caught:
            self.client.wake(REQUEST)

        self.assertIn("runUuid", str(caught.exception))

    def test_invalid_run_uuid_values_raise_routine_error(self):
        for value in ("", "   ", None, 123, True, [], {}):
            with self.subTest(runUuid=value):
                self.server.respond_json({"runUuid": value})

                with self.assertRaises(RoutineError) as caught:
                    self.client.wake(REQUEST)

                self.assertIn("runUuid", str(caught.exception))

    def test_truncated_response_raises_routine_error_without_status(self):
        self.server.respond_raw(
            b'{"runUuid": "x"}', declared_length=100
        )

        with self.assertRaises(RoutineError) as caught:
            self.client.wake(REQUEST)

        error = caught.exception
        self.assertIsNone(error.status)
        self.assertIn("IncompleteRead", error.text)

    def test_success_error_does_not_leak_the_key(self):
        self.server.respond_raw(
            ("unexpected " + KEY).encode("utf-8")
        )

        with self.assertRaises(RoutineError) as caught:
            self.client.wake(REQUEST)

        self.assertNotIn(KEY, str(caught.exception))
        self.assertNotIn(KEY, caught.exception.text)


class RequestValidationTests(RoutineTestCase):
    def test_non_mapping_request_raises_routine_error(self):
        for value in (None, [], "request", 3, True):
            with self.subTest(request=value):
                with self.assertRaises(RoutineError):
                    self.client.wake(value)
        self.assertEqual(self.server.requests, [])

    def test_non_serializable_request_raises_routine_error(self):
        with self.assertRaises(RoutineError):
            self.client.wake({"task": object()})

        self.assertEqual(self.server.requests, [])


class TransportErrorTests(unittest.TestCase):
    def test_connection_failure_raises_routine_error_without_status(self):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        client = RoutineClient(
            "http://127.0.0.1:{}/hooks/routine-1".format(port),
            KEY,
            timeout=1.0,
        )

        with self.assertRaises(RoutineError) as caught:
            client.wake(REQUEST)

        error = caught.exception
        self.assertIsNone(error.status)
        self.assertIsInstance(error.text, str)
        self.assertTrue(error.text)
        self.assertEqual(error.method, "POST")
        self.assertNotIn(KEY, str(error))


if __name__ == "__main__":
    unittest.main()
