"""Black-box tests for the self-hosted relay server (M1.5b task R1).

Every test starts a real relay on a loopback ephemeral port and talks HTTP to
it. Requests carry short timeouts and long-polls use sub-second waits, so a
broken implementation fails fast instead of hanging the suite.
"""

import json
import threading
import time
import unittest
import urllib.error
import urllib.request

from relay.server import RelayServer

ADMIN_TOKEN = "test-admin-token"
PAYLOAD_LIMIT = 64 * 1024
_MISSING = object()


def _decode(raw):
    """Decode an optional JSON response body, tolerating empty/non-JSON."""
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


class RelayServerTestCase(unittest.TestCase):
    """Shared server lifecycle and HTTP helpers."""

    def setUp(self):
        self.server = RelayServer(ADMIN_TOKEN)
        self.base_url = self.server.start()
        self.addCleanup(self.server.stop)

    def request(self, method, path, body=None, token=_MISSING, timeout=5.0):
        """Perform one request and return ``(status, json_body, headers)``."""
        data = None
        headers = {}
        if body is not None:
            if isinstance(body, bytes):
                data = body
            elif isinstance(body, str):
                data = body.encode("utf-8")
            else:
                data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token is not _MISSING and token is not None:
            headers["Authorization"] = "Bearer {}".format(token)

        request = urllib.request.Request(
            self.base_url + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, _decode(response.read()), response.headers
        except urllib.error.HTTPError as exc:
            headers = exc.headers
            try:
                raw = exc.read()
            finally:
                exc.close()
            return exc.code, _decode(raw), headers

    def create_channel(self):
        status, body, _ = self.request("POST", "/v1/channels", token=ADMIN_TOKEN)
        self.assertEqual(status, 201, body)
        self.assertIsInstance(body, dict)
        return body

    def post_message(self, channel, envelope, token=_MISSING, body=None):
        path = "/v1/channels/{}/messages".format(channel["channel"])
        if body is None:
            body = envelope
        if token is _MISSING:
            token = channel["token"]
        return self.request("POST", path, body=body, token=token)

    def get_messages(self, channel, after=0, wait=None, token=_MISSING, timeout=5.0):
        path = "/v1/channels/{}/messages?after={}".format(channel["channel"], after)
        if wait is not None:
            path += "&wait={}".format(wait)
        if token is _MISSING:
            token = channel["token"]
        return self.request("GET", path, token=token, timeout=timeout)


class ChannelCreationTests(RelayServerTestCase):
    def test_create_channel_returns_numeric_id_token_and_url(self):
        channel = self.create_channel()

        self.assertTrue(channel["channel"].isdigit())
        self.assertGreater(int(channel["channel"]), 0)
        self.assertIsInstance(channel["token"], str)
        self.assertGreaterEqual(len(channel["token"]), 16)
        self.assertEqual(
            channel["url"],
            self.base_url + "/v1/channels/" + channel["channel"],
        )

    def test_channels_get_distinct_incrementing_ids_and_tokens(self):
        first = self.create_channel()
        second = self.create_channel()

        self.assertEqual(int(second["channel"]), int(first["channel"]) + 1)
        self.assertNotEqual(first["token"], second["token"])

    def test_start_binds_loopback_ephemeral_port(self):
        self.assertTrue(self.base_url.startswith("http://127.0.0.1:"))
        self.assertGreater(int(self.base_url.rsplit(":", 1)[1]), 0)

    def test_stop_releases_the_listener(self):
        server = RelayServer(ADMIN_TOKEN)
        base_url = server.start()
        server.stop()

        with self.assertRaises(urllib.error.URLError):
            urllib.request.urlopen(base_url + "/v1/channels", timeout=2)

    def test_channel_creation_requires_the_admin_token(self):
        first = self.create_channel()

        for token in (_MISSING, None, "wrong-admin-token", first["token"]):
            status, _, _ = self.request("POST", "/v1/channels", token=token)
            self.assertEqual(status, 401)

        second = self.create_channel()
        self.assertEqual(int(second["channel"]), int(first["channel"]) + 1)


class MessageAuthAndRoutingTests(RelayServerTestCase):
    def test_message_endpoints_require_the_channel_token(self):
        channel = self.create_channel()

        for token in (None, "wrong-channel-token", ADMIN_TOKEN):
            status, _, _ = self.post_message(channel, {"n": 1}, token=token)
            self.assertEqual(status, 401)
            status, _, _ = self.get_messages(channel, token=token)
            self.assertEqual(status, 401)

        status, body, _ = self.get_messages(channel)
        self.assertEqual(status, 200)
        self.assertEqual(body["messages"], [])
        self.assertEqual(body["next"], 0)

    def test_a_channel_token_cannot_read_or_write_another_channel(self):
        first = self.create_channel()
        second = self.create_channel()

        status, _, _ = self.post_message(first, {"n": 1}, token=second["token"])
        self.assertEqual(status, 401)
        status, _, _ = self.get_messages(first, token=second["token"])
        self.assertEqual(status, 401)

        status, _, _ = self.post_message(first, {"n": 1})
        self.assertEqual(status, 201)
        status, body, _ = self.get_messages(second)
        self.assertEqual(status, 200)
        self.assertEqual(body["messages"], [])

    def test_unknown_channel_returns_404(self):
        channel = self.create_channel()
        unknown = "999999"

        status, _, _ = self.request(
            "POST",
            "/v1/channels/{}/messages".format(unknown),
            body={"n": 1},
            token=channel["token"],
        )
        self.assertEqual(status, 404)
        status, _, _ = self.request(
            "GET",
            "/v1/channels/{}/messages?after=0".format(unknown),
            token=channel["token"],
        )
        self.assertEqual(status, 404)
        status, _, _ = self.request(
            "GET",
            "/v1/channels/not-a-number/messages",
            token=channel["token"],
        )
        self.assertEqual(status, 404)
        status, _, _ = self.request("GET", "/v1/nope", token=channel["token"])
        self.assertEqual(status, 404)

    def test_wrong_method_returns_405(self):
        channel = self.create_channel()

        status, _, headers = self.request(
            "GET", "/v1/channels", token=ADMIN_TOKEN
        )
        self.assertEqual(status, 405)
        self.assertIn("POST", headers.get("Allow", ""))

        status, _, headers = self.request(
            "PUT",
            "/v1/channels/{}/messages".format(channel["channel"]),
            body={"n": 1},
            token=channel["token"],
        )
        self.assertEqual(status, 405)
        allow = headers.get("Allow", "")
        self.assertIn("GET", allow)
        self.assertIn("POST", allow)

        status, _, _ = self.request(
            "DELETE",
            "/v1/channels/{}/messages".format(channel["channel"]),
            token=channel["token"],
        )
        self.assertEqual(status, 405)


class MessageDeliveryTests(RelayServerTestCase):
    def test_post_and_read_preserve_order_and_cursor(self):
        channel = self.create_channel()
        envelopes = [
            {"bridge": "dsh-grokbot-v1", "n": index} for index in (1, 2, 3)
        ]

        for expected_seq, envelope in enumerate(envelopes, start=1):
            status, body, _ = self.post_message(channel, envelope)
            self.assertEqual(status, 201, body)
            self.assertEqual(body, {"seq": expected_seq})

        status, body, _ = self.get_messages(channel, after=0)
        self.assertEqual(status, 200)
        self.assertEqual([item["seq"] for item in body["messages"]], [1, 2, 3])
        self.assertEqual([item["envelope"] for item in body["messages"]], envelopes)
        self.assertEqual(body["next"], 3)

        status, body, _ = self.get_messages(channel, after=2)
        self.assertEqual(status, 200)
        self.assertEqual([item["seq"] for item in body["messages"]], [3])
        self.assertEqual(body["next"], 3)

        status, body, _ = self.get_messages(channel, after=3)
        self.assertEqual(status, 200)
        self.assertEqual(body["messages"], [])
        self.assertEqual(body["next"], 3)

    def test_get_with_wait_does_not_block_when_messages_exist(self):
        channel = self.create_channel()
        self.assertEqual(self.post_message(channel, {"n": 1})[0], 201)

        started = time.monotonic()
        status, body, _ = self.get_messages(channel, after=0, wait=5)
        elapsed = time.monotonic() - started

        self.assertEqual(status, 200)
        self.assertEqual([item["seq"] for item in body["messages"]], [1])
        self.assertEqual(body["next"], 1)
        self.assertLess(elapsed, 2.0)

    def test_long_poll_wakes_when_a_message_arrives(self):
        channel = self.create_channel()
        outcome = {}

        def poll():
            outcome["response"] = self.get_messages(
                channel, after=0, wait=5, timeout=10
            )

        started = time.monotonic()
        poller = threading.Thread(target=poll)
        poller.start()
        time.sleep(0.2)
        status, body, _ = self.post_message(channel, {"n": 1})
        self.assertEqual(status, 201, body)
        poller.join(5)
        elapsed = time.monotonic() - started

        self.assertFalse(poller.is_alive(), "long-poll did not wake up")
        status, body, _ = outcome["response"]
        self.assertEqual(status, 200)
        self.assertEqual([item["seq"] for item in body["messages"]], [1])
        self.assertEqual(body["next"], 1)
        self.assertLess(elapsed, 3.0)

    def test_long_poll_times_out_with_an_empty_result(self):
        channel = self.create_channel()

        started = time.monotonic()
        status, body, _ = self.get_messages(channel, after=0, wait=0.3)
        elapsed = time.monotonic() - started

        self.assertEqual(status, 200)
        self.assertEqual(body, {"messages": [], "next": 0})
        self.assertGreaterEqual(elapsed, 0.2)
        self.assertLess(elapsed, 3.0)

    def test_long_poll_without_wait_returns_immediately(self):
        channel = self.create_channel()

        started = time.monotonic()
        status, body, _ = self.get_messages(channel, after=0)
        elapsed = time.monotonic() - started

        self.assertEqual(status, 200)
        self.assertEqual(body, {"messages": [], "next": 0})
        self.assertLess(elapsed, 1.0)

    def test_channels_are_isolated(self):
        first = self.create_channel()
        second = self.create_channel()

        self.assertEqual(self.post_message(first, {"n": 1})[0], 201)
        self.assertEqual(self.post_message(first, {"n": 2})[0], 201)
        self.assertEqual(self.post_message(second, {"n": 100})[0], 201)

        status, body, _ = self.get_messages(first)
        self.assertEqual(status, 200)
        self.assertEqual([item["seq"] for item in body["messages"]], [1, 2])
        self.assertEqual(body["next"], 2)

        status, body, _ = self.get_messages(second)
        self.assertEqual(status, 200)
        self.assertEqual([item["seq"] for item in body["messages"]], [1])
        self.assertEqual(body["messages"][0]["envelope"], {"n": 100})
        self.assertEqual(body["next"], 1)

        status, body, _ = self.get_messages(second, after=1, wait=0.2)
        self.assertEqual(status, 200)
        self.assertEqual(body["messages"], [])

    def test_invalid_cursors_and_waits_return_400(self):
        channel = self.create_channel()

        for query in (
            "after=abc",
            "after=-1",
            "after=0&wait=abc",
            "after=0&wait=-1",
            "after=0&wait=nan",
        ):
            status, _, _ = self.request(
                "GET",
                "/v1/channels/{}/messages?{}".format(channel["channel"], query),
                token=channel["token"],
            )
            self.assertEqual(status, 400, query)


class PayloadTests(RelayServerTestCase):
    def test_exactly_64_kib_is_accepted(self):
        channel = self.create_channel()
        empty = json.dumps({"note": ""})
        envelope = {"note": "x" * (PAYLOAD_LIMIT - len(empty))}
        payload = json.dumps(envelope)
        self.assertEqual(len(payload), PAYLOAD_LIMIT)

        status, body, _ = self.post_message(channel, None, body=payload)
        self.assertEqual(status, 201, body)
        self.assertEqual(body, {"seq": 1})

    def test_more_than_64_kib_is_rejected_with_413(self):
        channel = self.create_channel()
        empty = json.dumps({"note": ""})
        envelope = {"note": "x" * (PAYLOAD_LIMIT - len(empty) + 1)}
        payload = json.dumps(envelope)
        self.assertEqual(len(payload), PAYLOAD_LIMIT + 1)

        status, _, _ = self.post_message(channel, None, body=payload)
        self.assertEqual(status, 413)

    def test_non_object_or_invalid_json_is_rejected_with_400(self):
        channel = self.create_channel()

        for body in ("[]", '"text"', "not json", "null", ""):
            status, _, _ = self.post_message(channel, None, body=body)
            self.assertEqual(status, 400, repr(body))


if __name__ == "__main__":
    unittest.main()
