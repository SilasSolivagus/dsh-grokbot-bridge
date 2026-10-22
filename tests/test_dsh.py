"""Tests for the DSH Kanban HTTP client used by the M1.5 bridge.

Every test talks to a local ``http.server`` fake started on ``127.0.0.1``;
nothing here touches a live DSH instance.
"""

import json
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bridge.dsh import DshClient, DshError

TOKEN = "builder-token-123"
TASK_ID = "440e9040-1657-45bf-a945-0cadc6cabec0"

BOOTSTRAP = {
    "projects": [{"id": "dsh-kanban", "name": "DSH Kanban"}],
    "tasks": [
        {
            "id": TASK_ID,
            "projectId": "dsh-kanban",
            "status": "inbox",
            "version": 1,
        }
    ],
}

DETAIL = {
    "task": {"id": TASK_ID, "status": "inbox", "version": 1},
    "comments": [{"id": "c-1", "taskId": TASK_ID, "body": "hi"}],
    "activity": [{"id": "a-1", "taskId": TASK_ID, "field": "status"}],
    "attachments": [],
}

TASK = {"id": TASK_ID, "status": "in_progress", "version": 2}

COMMENT = {
    "id": "c-2",
    "taskId": TASK_ID,
    "body": "started",
    "author": {"type": "agent", "id": "builder"},
}


class FakeDshServer:
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

            def do_PATCH(self):
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
    def base_url(self):
        host, port = self._httpd.server_address[:2]
        return "http://{}:{}/kanban-api".format(host, port)

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


class DshTestCase(unittest.TestCase):
    """Base class with a fake DSH server and a configured client."""

    def setUp(self):
        self.server = FakeDshServer()
        self.addCleanup(self.server.close)
        self.client = self.new_client()

    def new_client(self, **kwargs):
        kwargs.setdefault("timeout", 5.0)
        return DshClient(self.server.base_url, TOKEN, **kwargs)

    def only_request(self):
        requests = self.server.requests
        self.assertEqual(len(requests), 1, requests)
        return requests[0]

    def json_body(self, request):
        return json.loads(request["body"].decode("utf-8"))


class BootstrapTests(DshTestCase):
    def test_bootstrap_uses_get_on_bootstrap_endpoint(self):
        self.server.respond_json(BOOTSTRAP)

        self.client.bootstrap()

        request = self.only_request()
        self.assertEqual(request["method"], "GET")
        self.assertEqual(request["path"], "/kanban-api/bootstrap")

    def test_bootstrap_sends_bearer_token(self):
        self.server.respond_json(BOOTSTRAP)

        self.client.bootstrap()

        request = self.only_request()
        self.assertEqual(
            request["headers"]["authorization"], "Bearer " + TOKEN
        )

    def test_bootstrap_returns_projects_and_tasks(self):
        self.server.respond_json(BOOTSTRAP)

        self.assertEqual(self.client.bootstrap(), BOOTSTRAP)

    def test_bootstrap_accepts_json_and_sends_no_body(self):
        self.server.respond_json(BOOTSTRAP)

        self.client.bootstrap()

        request = self.only_request()
        self.assertEqual(request["headers"]["accept"], "application/json")
        self.assertEqual(request["body"], b"")
        self.assertIn(
            request["headers"].get("content-length", "0"), ("", "0")
        )

    def test_surrounding_token_whitespace_is_ignored(self):
        self.server.respond_json(BOOTSTRAP)
        client = DshClient(
            self.server.base_url, "  " + TOKEN + "\n", timeout=5.0
        )

        client.bootstrap()

        request = self.only_request()
        self.assertEqual(
            request["headers"]["authorization"], "Bearer " + TOKEN
        )

    def test_base_url_trailing_slash_is_not_doubled(self):
        self.server.respond_json(BOOTSTRAP)
        client = DshClient(self.server.base_url + "///", TOKEN, timeout=5.0)

        client.bootstrap()

        request = self.only_request()
        self.assertEqual(request["path"], "/kanban-api/bootstrap")


class GetTaskTests(DshTestCase):
    def test_get_task_uses_task_endpoint(self):
        self.server.respond_json(DETAIL)

        self.assertEqual(self.client.get_task(TASK_ID), DETAIL)

        request = self.only_request()
        self.assertEqual(request["method"], "GET")
        self.assertEqual(
            request["path"], "/kanban-api/tasks/" + TASK_ID
        )

    def test_get_task_returns_comments_activity_and_attachments(self):
        self.server.respond_json(DETAIL)

        detail = self.client.get_task(TASK_ID)

        self.assertEqual(detail["comments"], DETAIL["comments"])
        self.assertEqual(detail["activity"], DETAIL["activity"])
        self.assertEqual(detail["attachments"], DETAIL["attachments"])

    def test_get_task_encodes_the_task_id(self):
        self.server.respond_json(DETAIL)

        self.client.get_task("task/one two")

        request = self.only_request()
        self.assertEqual(
            request["path"], "/kanban-api/tasks/task%2Fone%20two"
        )

    def test_get_task_rejects_a_blank_id(self):
        with self.assertRaises(ValueError):
            self.client.get_task("   ")

        self.assertEqual(self.server.requests, [])

    def test_get_task_rejects_non_string_ids(self):
        for value in (None, 1, True, ["task"]):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.client.get_task(value)
        self.assertEqual(self.server.requests, [])


class UpdateStatusTests(DshTestCase):
    def test_update_status_uses_patch_on_task_endpoint(self):
        self.server.respond_json(TASK)

        self.client.update_status(TASK_ID, 1, "in_progress")

        request = self.only_request()
        self.assertEqual(request["method"], "PATCH")
        self.assertEqual(
            request["path"], "/kanban-api/tasks/" + TASK_ID
        )

    def test_update_status_sends_version_and_status(self):
        self.server.respond_json(TASK)

        self.client.update_status(TASK_ID, 3, "blocked")

        request = self.only_request()
        self.assertEqual(
            self.json_body(request), {"version": 3, "status": "blocked"}
        )
        self.assertEqual(
            set(self.json_body(request)), {"version", "status"}
        )

    def test_update_status_sends_json_content_type(self):
        self.server.respond_json(TASK)

        self.client.update_status(TASK_ID, 3, "in_progress")

        request = self.only_request()
        self.assertEqual(
            request["headers"]["content-type"], "application/json"
        )
        self.assertEqual(
            request["headers"]["authorization"], "Bearer " + TOKEN
        )

    def test_update_status_returns_the_updated_task(self):
        self.server.respond_json(TASK)

        self.assertEqual(
            self.client.update_status(TASK_ID, 1, "in_progress"), TASK
        )

    def test_update_status_accepts_version_one_and_done(self):
        self.server.respond_json(TASK)

        self.client.update_status(TASK_ID, 1, "done")

        self.assertEqual(
            self.json_body(self.only_request()),
            {"version": 1, "status": "done"},
        )

    def test_update_status_rejects_invalid_versions(self):
        for value in (0, -1, True, "3", 3.0, None):
            with self.subTest(version=value):
                with self.assertRaises(ValueError):
                    self.client.update_status(TASK_ID, value, "in_progress")
        self.assertEqual(self.server.requests, [])

    def test_update_status_rejects_invalid_statuses(self):
        for value in ("", "   ", None, 3):
            with self.subTest(status=value):
                with self.assertRaises(ValueError):
                    self.client.update_status(TASK_ID, 1, value)
        self.assertEqual(self.server.requests, [])

    def test_update_status_rejects_a_blank_task_id(self):
        with self.assertRaises(ValueError):
            self.client.update_status("", 1, "in_progress")
        self.assertEqual(self.server.requests, [])


class CommentTests(DshTestCase):
    def test_comment_posts_to_the_comments_endpoint(self):
        self.server.respond_json(COMMENT, status=201)

        self.client.comment(TASK_ID, "started")

        request = self.only_request()
        self.assertEqual(request["method"], "POST")
        self.assertEqual(
            request["path"],
            "/kanban-api/tasks/{}/comments".format(TASK_ID),
        )

    def test_comment_sends_the_body(self):
        self.server.respond_json(COMMENT, status=201)

        self.client.comment(TASK_ID, "started")

        request = self.only_request()
        self.assertEqual(self.json_body(request), {"body": "started"})
        self.assertEqual(set(self.json_body(request)), {"body"})

    def test_comment_returns_the_created_comment(self):
        self.server.respond_json(COMMENT, status=201)

        self.assertEqual(
            self.client.comment(TASK_ID, "started"), COMMENT
        )

    def test_comment_preserves_multiline_and_unicode_bodies(self):
        self.server.respond_json(COMMENT, status=201)
        body = "第一行\nsecond line\tend"

        self.client.comment(TASK_ID, body)

        self.assertEqual(
            self.json_body(self.only_request()), {"body": body}
        )

    def test_comment_rejects_a_blank_body(self):
        for value in ("", "   ", None, 3):
            with self.subTest(body=value):
                with self.assertRaises(ValueError):
                    self.client.comment(TASK_ID, value)
        self.assertEqual(self.server.requests, [])

    def test_comment_rejects_a_blank_task_id(self):
        with self.assertRaises(ValueError):
            self.client.comment("   ", "started")
        self.assertEqual(self.server.requests, [])


class HttpErrorTests(DshTestCase):
    def test_401_403_and_409_raise_dsh_error_with_status_and_text(self):
        for status in (401, 403, 409):
            with self.subTest(status=status):
                payload = {
                    "error": {
                        "code": "CODE-{}".format(status),
                        "message": "boom {}".format(status),
                        "details": {},
                    }
                }
                self.server.respond_json(payload, status=status)

                with self.assertRaises(DshError) as caught:
                    self.client.bootstrap()

                error = caught.exception
                self.assertEqual(error.status, status)
                self.assertIn("boom {}".format(status), error.text)
                self.assertIn(str(status), str(error))
                self.assertIn("boom {}".format(status), str(error))

    def test_error_text_is_decoded_as_utf8(self):
        self.server.respond_json(
            {"error": {"code": "FORBIDDEN", "message": "令牌无效"}},
            status=403,
        )

        with self.assertRaises(DshError) as caught:
            self.client.bootstrap()

        self.assertEqual(caught.exception.status, 403)
        self.assertIn("令牌无效", caught.exception.text)
        self.assertIn("令牌无效", str(caught.exception))

    def test_error_text_survives_non_utf8_bytes(self):
        self.server.respond_raw(b"\xff\xfe", status=403)

        with self.assertRaises(DshError) as caught:
            self.client.bootstrap()

        error = caught.exception
        self.assertEqual(error.status, 403)
        self.assertIsInstance(error.text, str)

    def test_http_error_message_does_not_leak_the_token(self):
        self.server.respond_json(
            {"error": {"code": "UNAUTHORIZED", "message": "bad"}},
            status=401,
        )

        with self.assertRaises(DshError) as caught:
            self.client.bootstrap()

        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertNotIn(TOKEN, caught.exception.text)


class MalformedResponseTests(DshTestCase):
    def test_invalid_json_on_success_raises_dsh_error(self):
        self.server.respond_raw(b"not json")

        with self.assertRaises(DshError) as caught:
            self.client.bootstrap()

        error = caught.exception
        self.assertEqual(error.status, 200)
        self.assertEqual(error.text, "not json")

    def test_json_array_on_success_raises_dsh_error(self):
        self.server.respond_raw(b"[]")

        with self.assertRaises(DshError) as caught:
            self.client.get_task(TASK_ID)

        error = caught.exception
        self.assertEqual(error.status, 200)
        self.assertEqual(error.text, "[]")

    def test_empty_success_body_raises_dsh_error(self):
        self.server.respond_raw(b"")

        with self.assertRaises(DshError) as caught:
            self.client.comment(TASK_ID, "started")

        error = caught.exception
        self.assertEqual(error.status, 200)
        self.assertEqual(error.text, "")

    def test_truncated_response_raises_dsh_error_without_status(self):
        self.server.respond_raw(
            b'{"projects":', declared_length=100
        )

        with self.assertRaises(DshError) as caught:
            self.client.bootstrap()

        error = caught.exception
        self.assertIsNone(error.status)
        self.assertIn("IncompleteRead", error.text)


class TransportErrorTests(unittest.TestCase):
    def test_connection_failure_raises_dsh_error_without_status(self):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        client = DshClient(
            "http://127.0.0.1:{}/kanban-api".format(port),
            TOKEN,
            timeout=1.0,
        )

        with self.assertRaises(DshError) as caught:
            client.bootstrap()

        error = caught.exception
        self.assertIsNone(error.status)
        self.assertIsInstance(error.text, str)
        self.assertTrue(error.text)
        self.assertEqual(error.method, "GET")


class ConfigTests(unittest.TestCase):
    def test_token_is_required(self):
        for token in ("", "   ", None, 3):
            with self.subTest(token=token):
                with self.assertRaises(ValueError):
                    DshClient(
                        "http://127.0.0.1:61849/kanban-api", token
                    )

    def test_base_url_is_required(self):
        for url in ("", "   ", None, 3):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    DshClient(url, TOKEN)

    def test_base_url_must_be_http(self):
        for url in (
            "ftp://127.0.0.1/kanban-api",
            "127.0.0.1:61849/kanban-api",
            "http:///kanban-api",
            "http://127.0.0.1/kanban-api?x=1",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    DshClient(url, TOKEN)

    def test_base_url_trailing_slashes_are_trimmed(self):
        client = DshClient(
            "http://127.0.0.1:61849/kanban-api///", TOKEN
        )

        self.assertEqual(
            client.base_url, "http://127.0.0.1:61849/kanban-api"
        )

    def test_timeout_must_be_a_positive_number(self):
        for value in (0, -1, "5", True, None):
            with self.subTest(timeout=value):
                with self.assertRaises(ValueError):
                    DshClient(
                        "http://127.0.0.1:61849/kanban-api",
                        TOKEN,
                        timeout=value,
                    )

    def test_repr_does_not_expose_the_token(self):
        client = DshClient(
            "http://127.0.0.1:61849/kanban-api", TOKEN
        )

        self.assertNotIn(TOKEN, repr(client))
        self.assertIn("DshClient", repr(client))


if __name__ == "__main__":
    unittest.main()
