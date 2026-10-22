"""Tests for the relay bot CLI (M1.5b task R3).

The CLI is exercised as a real subprocess so its exit codes, stdout, and
stderr are tested exactly as a bot would see them. ``poll``/``send`` run
against a real :class:`relay.server.RelayServer` on an ephemeral loopback
port; a small scripted HTTP server covers non-2xx responses and token
redaction. No external network, GitHub, DSH, or Grok endpoint is contacted.

Security invariants pinned here:

* the bearer/admin token never appears in stdout or stderr of ``poll`` or
  ``send``, including on HTTP errors whose body echoes the token;
* a non-2xx response exits ``1`` and a usage error exits ``2``;
* ``send`` transports the envelope unchanged and never validates it against
  the bridge schema (the orchestrator validates on receipt).
"""

import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from relay.server import RelayServer

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "scripts", "relay-bot.py")
ADMIN_TOKEN = "admin-token-canary"
WRONG_TOKEN = "wrong-token-canary"

TASK_ID = "440e9040-1657-45bf-a945-0cadc6cabec0"
REQUEST = {
    "bridge": "dsh-grokbot-v1",
    "message_id": "11111111-1111-4111-8111-111111111111",
    "from": "dsh",
    "to": "grok",
    "card": "TEST-004",
    "task_id": TASK_ID,
    "type": "task_request",
    "agent": "builder",
    "title": "Add a relay bot client",
    "instructions": "Implement the CLI and its tests.",
    "acceptance": ["tests pass"],
}
ACK = {
    "bridge": "dsh-grokbot-v1",
    "message_id": "22222222-2222-4222-8222-222222222222",
    "from": "grok",
    "to": "dsh",
    "card": "TEST-004",
    "task_id": TASK_ID,
    "type": "ack",
    "agent": "builder",
    "status": "received",
    "note": "已收到",
}
UPDATE = {
    "bridge": "dsh-grokbot-v1",
    "message_id": "33333333-3333-4333-8333-333333333333",
    "from": "grok",
    "to": "dsh",
    "card": "TEST-004",
    "task_id": TASK_ID,
    "type": "task_update",
    "agent": "builder",
    "state": "in_progress",
    "evidence": ["first test written"],
}
COMPLETE = {
    "bridge": "dsh-grokbot-v1",
    "message_id": "44444444-4444-4444-8444-444444444444",
    "from": "grok",
    "to": "dsh",
    "card": "TEST-004",
    "task_id": TASK_ID,
    "type": "task_complete",
    "agent": "builder",
    "state": "done",
    "evidence": ["all tests pass"],
    "note": "done",
}

_ENV = dict(os.environ, PYTHONIOENCODING="utf-8")


def run_cli(*args, stdin=None, timeout=20):
    """Run ``scripts/relay-bot.py`` and return the completed process."""
    return subprocess.run(
        [sys.executable, SCRIPT, *args],
        input=stdin,
        cwd=REPO_ROOT,
        env=_ENV,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )


def start_cli(*args):
    """Start the CLI without waiting, for long-poll tests."""
    return subprocess.Popen(
        [sys.executable, SCRIPT, *args],
        cwd=REPO_ROOT,
        env=_ENV,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )


def write_json(directory, name, payload):
    """Write ``payload`` as UTF-8 JSON and return the path."""
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    return path


class RelayBotTestCase(unittest.TestCase):
    """Start one relay and one channel per test."""

    def setUp(self):
        self.server = RelayServer(ADMIN_TOKEN)
        self.base_url = self.server.start()
        self.addCleanup(self.server.stop)
        channel = self.http_create_channel()
        self.channel = channel["channel"]
        self.token = channel["token"]

    # -- direct HTTP helpers (independent of the CLI under test) ---------

    def http_create_channel(self, token=ADMIN_TOKEN):
        request = urllib.request.Request(
            self.base_url + "/v1/channels",
            data=b"",
            headers={"Authorization": "Bearer {}".format(token)},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def http_post(self, envelope, token=None):
        request = urllib.request.Request(
            "{}/v1/channels/{}/messages".format(self.base_url, self.channel),
            data=json.dumps(envelope, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": "Bearer {}".format(token or self.token),
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def http_poll(self, after=0, wait=0):
        request = urllib.request.Request(
            "{}/v1/channels/{}/messages?after={}&wait={}".format(
                self.base_url, self.channel, after, wait
            ),
            headers={"Authorization": "Bearer {}".format(self.token)},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    # -- CLI helpers -----------------------------------------------------

    def message_args(self, token=None):
        return [
            "--url",
            self.base_url,
            "--channel",
            self.channel,
            "--token",
            token or self.token,
        ]

    def poll(self, after=None, wait=None, token=None):
        args = ["poll"] + self.message_args(token)
        if after is not None:
            args += ["--after", str(after)]
        if wait is not None:
            args += ["--wait", str(wait)]
        return run_cli(*args)

    def send(self, path, token=None, stdin=None):
        return run_cli(
            "send", *self.message_args(token), "--json", path, stdin=stdin
        )

    def assert_no_token(self, result, *tokens):
        output = result.stdout + result.stderr
        for token in tokens:
            self.assertNotIn(token, output)


class PollCommandTests(RelayBotTestCase):
    def test_poll_prints_messages_and_next_cursor(self):
        self.assertEqual(self.http_post(REQUEST), {"seq": 1})
        self.assertEqual(self.http_post(ACK), {"seq": 2})

        result = self.poll(after=0, wait=0)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "messages": [
                    {"seq": 1, "envelope": REQUEST},
                    {"seq": 2, "envelope": ACK},
                ],
                "next": 2,
            },
        )
        self.assertEqual(result.stderr, "")
        self.assert_no_token(result, self.token, ADMIN_TOKEN)

    def test_poll_returns_only_messages_after_the_cursor(self):
        for envelope in (REQUEST, ACK, UPDATE):
            self.http_post(envelope)

        result = self.poll(after=2, wait=0)

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(
            payload,
            {
                "messages": [{"seq": 3, "envelope": UPDATE}],
                "next": 3,
            },
        )

    def test_poll_empty_channel_returns_empty_result(self):
        result = self.poll(after=0, wait=0)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout), {"messages": [], "next": 0}
        )

    def test_poll_defaults_to_after_zero(self):
        self.http_post(REQUEST)

        result = self.poll(wait=0)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {"messages": [{"seq": 1, "envelope": REQUEST}], "next": 1},
        )

    def test_poll_canonicalizes_a_channel_id_with_a_leading_zero(self):
        self.http_post(REQUEST)

        result = run_cli(
            "poll",
            "--url",
            self.base_url,
            "--channel",
            "0" + self.channel,
            "--token",
            self.token,
            "--after",
            "0",
            "--wait",
            "0",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["next"], 1)

    def test_poll_wait_holds_until_a_message_arrives(self):
        process = start_cli(
            "poll",
            *self.message_args(),
            "--after",
            "0",
            "--wait",
            "5",
        )
        try:
            # Give the child time to enter the long-poll before posting.
            threading.Event().wait(0.3)
            self.http_post(REQUEST)
            stdout, stderr = process.communicate(timeout=10)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

        self.assertEqual(process.returncode, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(
            payload,
            {"messages": [{"seq": 1, "envelope": REQUEST}], "next": 1},
        )

    def test_poll_rejects_a_negative_cursor_as_a_usage_error(self):
        result = self.poll(after=-1, wait=0)

        self.assertEqual(result.returncode, 2)
        self.assertIn("after", result.stderr.lower())
        self.assert_no_token(result, self.token, ADMIN_TOKEN)

    def test_poll_accepts_a_wait_inside_the_relay_cap(self):
        result = self.poll(after=0, wait=1)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout), {"messages": [], "next": 0}
        )

    def test_poll_accepts_a_fractional_wait(self):
        result = self.poll(after=0, wait=0.2)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout), {"messages": [], "next": 0}
        )

    def test_poll_rejects_a_negative_wait_as_a_usage_error(self):
        result = self.poll(after=0, wait=-1)

        self.assertEqual(result.returncode, 2)
        self.assertIn("--wait", result.stderr)
        self.assert_no_token(result, self.token, ADMIN_TOKEN)


class SendCommandTests(RelayBotTestCase):
    def test_send_posts_a_file_and_prints_seq(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_json(directory, "request.json", REQUEST)
            result = self.send(path)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"seq": 1})
        self.assertEqual(result.stderr, "")
        self.assert_no_token(result, self.token, ADMIN_TOKEN)
        self.assertEqual(
            self.http_poll(after=0)["messages"],
            [{"seq": 1, "envelope": REQUEST}],
        )

    def test_send_reads_stdin_with_dash(self):
        result = self.send("-", stdin=json.dumps(ACK, ensure_ascii=False))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"seq": 1})
        self.assertEqual(
            self.http_poll(after=0)["messages"],
            [{"seq": 1, "envelope": ACK}],
        )

    def test_second_send_gets_the_next_sequence_number(self):
        with tempfile.TemporaryDirectory() as directory:
            first = write_json(directory, "first.json", REQUEST)
            second = write_json(directory, "second.json", ACK)
            first_result = self.send(first)
            second_result = self.send(second)

        self.assertEqual(json.loads(first_result.stdout), {"seq": 1})
        self.assertEqual(json.loads(second_result.stdout), {"seq": 2})

    def test_send_preserves_unicode_and_unknown_fields(self):
        envelope = dict(UPDATE, note="进度：第一步完成", custom_field={"a": 1})

        result = self.send("-", stdin=json.dumps(envelope, ensure_ascii=False))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.http_poll(after=0)["messages"][0]["envelope"], envelope
        )

    def test_send_invalid_json_exits_1_without_posting(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "broken.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("not json")
            result = self.send(path)

        self.assertEqual(result.returncode, 1)
        self.assertIn("invalid json", result.stderr.lower())
        self.assert_no_token(result, self.token, ADMIN_TOKEN)
        self.assertEqual(self.http_poll(after=0)["messages"], [])

    def test_send_non_object_json_exits_1_without_posting(self):
        result = self.send("-", stdin="[]")

        self.assertEqual(result.returncode, 1)
        self.assertIn("json object", result.stderr.lower())
        self.assertEqual(self.http_poll(after=0)["messages"], [])

    def test_send_missing_file_exits_1(self):
        result = self.send("/definitely/not/a/relay/envelope.json")

        self.assertEqual(result.returncode, 1)
        self.assertIn("cannot read", result.stderr.lower())
        self.assert_no_token(result, self.token, ADMIN_TOKEN)

    def test_send_requires_the_json_option(self):
        result = run_cli("send", *self.message_args())

        self.assertEqual(result.returncode, 2)
        self.assertIn("--json", result.stderr)


class TokenAndFailureTests(RelayBotTestCase):
    def test_poll_with_a_wrong_token_exits_1_without_echoing_it(self):
        result = self.poll(after=0, wait=0, token=WRONG_TOKEN)

        self.assertEqual(result.returncode, 1)
        self.assertIn("401", result.stderr)
        self.assert_no_token(result, WRONG_TOKEN, self.token, ADMIN_TOKEN)

    def test_a_token_starting_with_a_dash_is_not_parsed_as_an_option(self):
        # token_urlsafe can produce a leading '-', which must reach the relay
        # instead of making argparse report a missing --token value.
        dash_token = "-dash-token-canary"
        result = self.poll(after=0, wait=0, token=dash_token)

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("401", result.stderr)
        self.assertNotIn("expected one argument", result.stderr)
        self.assert_no_token(result, dash_token, self.token, ADMIN_TOKEN)

    def test_send_with_a_wrong_token_exits_1_without_echoing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_json(directory, "request.json", REQUEST)
            result = self.send(path, token=WRONG_TOKEN)

        self.assertEqual(result.returncode, 1)
        self.assertIn("401", result.stderr)
        self.assert_no_token(result, WRONG_TOKEN, self.token, ADMIN_TOKEN)

    def test_unknown_channel_exits_1(self):
        result = run_cli(
            "poll",
            "--url",
            self.base_url,
            "--channel",
            "999999",
            "--token",
            self.token,
            "--after",
            "0",
            "--wait",
            "0",
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("404", result.stderr)
        self.assert_no_token(result, self.token, ADMIN_TOKEN)

    def test_connection_refused_exits_1_without_echoing_the_token(self):
        server = RelayServer(ADMIN_TOKEN)
        dead_url = server.start()
        server.stop()

        result = run_cli(
            "poll",
            "--url",
            dead_url,
            "--channel",
            "1",
            "--token",
            self.token,
            "--after",
            "0",
            "--wait",
            "0",
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("failed", result.stderr.lower())
        self.assert_no_token(result, self.token, ADMIN_TOKEN)

    def test_any_non_2xx_status_exits_1(self):
        server = _EchoErrorServer(("127.0.0.1", 0), _EchoErrorHandler)
        server.echo_token = self.token
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def cleanup():
            server.shutdown()
            server.server_close()
            thread.join(2.0)

        self.addCleanup(cleanup)
        url = "http://127.0.0.1:{}".format(server.server_address[1])

        for status in (400, 401, 403, 404, 405, 413, 500, 503):
            server.status = status
            result = run_cli(
                "poll",
                "--url",
                url,
                "--channel",
                "1",
                "--token",
                self.token,
                "--after",
                "0",
                "--wait",
                "0",
            )
            self.assertEqual(result.returncode, 1, status)
            self.assertIn(str(status), result.stderr)
            self.assert_no_token(result, self.token, ADMIN_TOKEN)

    def test_an_error_body_that_echoes_the_token_is_redacted(self):
        server = _EchoErrorServer(("127.0.0.1", 0), _EchoErrorHandler)
        server.echo_token = self.token
        server.status = 500
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def cleanup():
            server.shutdown()
            server.server_close()
            thread.join(2.0)

        self.addCleanup(cleanup)
        url = "http://127.0.0.1:{}".format(server.server_address[1])

        result = run_cli(
            "send",
            "--url",
            url,
            "--channel",
            "1",
            "--token",
            self.token,
            "--json",
            "-",
            stdin=json.dumps(REQUEST),
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("500", result.stderr)
        self.assertNotIn(self.token, result.stderr)
        self.assertIn("***", result.stderr)

    def test_invalid_url_is_a_usage_error(self):
        result = run_cli(
            "poll",
            "--url",
            "ftp://relay.example.test",
            "--channel",
            "1",
            "--token",
            self.token,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("http", result.stderr.lower())
        self.assert_no_token(result, self.token)

    def test_non_numeric_channel_is_a_usage_error(self):
        result = run_cli(
            "poll",
            "--url",
            self.base_url,
            "--channel",
            "not-a-number",
            "--token",
            self.token,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("channel", result.stderr.lower())
        self.assert_no_token(result, self.token)

    def test_non_positive_timeout_is_a_usage_error(self):
        result = run_cli(
            "poll",
            *self.message_args(),
            "--timeout",
            "0",
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("timeout", result.stderr.lower())
        self.assert_no_token(result, self.token)


class CreateChannelCommandTests(RelayBotTestCase):
    def test_create_channel_prints_channel_token_and_url(self):
        result = run_cli(
            "create-channel",
            "--url",
            self.base_url,
            "--admin-token",
            ADMIN_TOKEN,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(set(payload), {"channel", "token", "url"})
        self.assertTrue(payload["channel"].isdigit())
        self.assertTrue(payload["token"])
        self.assertEqual(
            payload["url"],
            "{}/v1/channels/{}".format(self.base_url, payload["channel"]),
        )

        # The minted token must work with the bot commands.
        post = run_cli(
            "send",
            "--url",
            self.base_url,
            "--channel",
            payload["channel"],
            "--token",
            payload["token"],
            "--json",
            "-",
            stdin=json.dumps(REQUEST),
        )
        self.assertEqual(post.returncode, 0, post.stderr)
        self.assertEqual(json.loads(post.stdout), {"seq": 1})

    def test_wrong_admin_token_exits_1_without_echoing_it(self):
        result = run_cli(
            "create-channel",
            "--url",
            self.base_url,
            "--admin-token",
            WRONG_TOKEN,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("401", result.stderr)
        self.assert_no_token(result, WRONG_TOKEN, ADMIN_TOKEN)

    def test_create_channel_requires_the_admin_token(self):
        result = run_cli("create-channel", "--url", self.base_url)

        self.assertEqual(result.returncode, 2)
        self.assertIn("--admin-token", result.stderr)


class _EchoErrorHandler(BaseHTTPRequestHandler):
    """Return a scripted non-2xx body that deliberately echoes the token."""

    protocol_version = "HTTP/1.0"

    def do_GET(self):
        self._respond()

    def do_POST(self):
        self._respond()

    def _respond(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        body = json.dumps(
            {"error": "denied for {}".format(self.server.echo_token)}
        ).encode("utf-8")
        self.send_response(self.server.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        """Keep test output free of per-request access logs."""


class _EchoErrorServer(ThreadingHTTPServer):
    daemon_threads = True


class _ScriptedResponseHandler(BaseHTTPRequestHandler):
    """Return a scripted body with status 200, for malformed-response tests."""

    protocol_version = "HTTP/1.0"

    def do_GET(self):
        self._respond()

    def do_POST(self):
        self._respond()

    def _respond(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        body = self.server.body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        """Keep test output free of per-request access logs."""


class _ScriptedResponseServer(ThreadingHTTPServer):
    daemon_threads = True


class MalformedResponseTests(RelayBotTestCase):
    def _start_scripted(self, body):
        server = _ScriptedResponseServer(
            ("127.0.0.1", 0), _ScriptedResponseHandler
        )
        server.body = body
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def cleanup():
            server.shutdown()
            server.server_close()
            thread.join(2.0)

        self.addCleanup(cleanup)
        return "http://127.0.0.1:{}".format(server.server_address[1])

    def _poll_url(self, url):
        return run_cli(
            "poll",
            "--url",
            url,
            "--channel",
            "1",
            "--token",
            self.token,
            "--after",
            "0",
            "--wait",
            "0",
        )

    def test_non_json_response_exits_1(self):
        result = self._poll_url(self._start_scripted("not json"))

        self.assertEqual(result.returncode, 1)
        self.assertIn("invalid json", result.stderr.lower())
        self.assert_no_token(result, self.token, ADMIN_TOKEN)

    def test_non_object_response_exits_1(self):
        result = self._poll_url(self._start_scripted("[]"))

        self.assertEqual(result.returncode, 1)
        self.assertIn("json object", result.stderr.lower())
        self.assert_no_token(result, self.token, ADMIN_TOKEN)

    def test_poll_response_without_a_messages_list_exits_1(self):
        result = self._poll_url(self._start_scripted('{"next": 0}'))

        self.assertEqual(result.returncode, 1)
        self.assertIn("messages", result.stderr.lower())
        self.assert_no_token(result, self.token, ADMIN_TOKEN)

    def test_send_response_without_seq_exits_1(self):
        url = self._start_scripted("{}")
        result = run_cli(
            "send",
            "--url",
            url,
            "--channel",
            "1",
            "--token",
            self.token,
            "--json",
            "-",
            stdin=json.dumps(REQUEST),
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("seq", result.stderr.lower())
        self.assert_no_token(result, self.token, ADMIN_TOKEN)


@unittest.skipUnless(
    os.name == "posix" and hasattr(signal, "SIGINT"),
    "the serve smoke test needs POSIX signals",
)
class ServeCommandTests(RelayBotTestCase):
    def test_serve_starts_a_relay_and_stops_on_sigint(self):
        process = start_cli(
            "serve",
            "--admin-token",
            ADMIN_TOKEN,
            "--host",
            "127.0.0.1",
            "--port",
            "0",
        )
        try:
            ready, _, _ = select.select([process.stdout], [], [], 10)
            self.assertTrue(ready, "serve did not print a URL")
            line = process.stdout.readline().strip()
            self.assertTrue(
                line.startswith("relay listening on "),
                "unexpected startup line: {!r}".format(line),
            )
            url = line.split("relay listening on ", 1)[1]

            request = urllib.request.Request(
                url + "/v1/channels",
                data=b"",
                headers={"Authorization": "Bearer {}".format(ADMIN_TOKEN)},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 201)

            process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=10)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

        self.assertEqual(process.returncode, 0, stderr)
        self.assertNotIn(ADMIN_TOKEN, stdout + stderr)

    def test_serve_rejects_an_invalid_port(self):
        result = run_cli(
            "serve",
            "--admin-token",
            ADMIN_TOKEN,
            "--port",
            "70000",
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("port", result.stderr.lower())
        self.assert_no_token(result, ADMIN_TOKEN)


class HelpTests(unittest.TestCase):
    def test_help_lists_every_command(self):
        result = run_cli("--help")

        self.assertEqual(result.returncode, 0)
        for command in ("poll", "send", "create-channel", "serve"):
            self.assertIn(command, result.stdout)


if __name__ == "__main__":
    unittest.main()
