"""Tests for the GitHub issue transport.

The adapter owns the ``gh`` command line and the issue/comment shapes.
Envelope validation, author allowlisting, and task state remain orchestrator
concerns, so these tests pin only the wire behavior: create an issue whose
body is exactly one fenced JSON block, read comments with a numeric cursor,
close the issue, and surface ``gh`` failures as ``GitHubError``.

No test runs ``gh`` against GitHub. The only subprocess is patched.
"""

import json
import re
import subprocess
import unittest
from unittest import mock

from bridge.github_transport import GitHubError, GitHubTransport
from bridge.transport import Transport, TransportError

REPO = "SilasSolivagus/dsh-grokbot-bridge"
ISSUE_URL = (
    "https://github.com/SilasSolivagus/dsh-grokbot-bridge/issues/42\n"
)

REQUEST = {
    "bridge": "dsh-grokbot-v1",
    "message_id": "11111111-1111-4111-8111-111111111111",
    "from": "掌柜",
    "to": "Origin",
    "card": "TEST-004",
    "task_id": "440e9040-1657-45bf-a945-0cadc6cabec0",
    "type": "task_request",
    "agent": "掌柜",
    "title": "Add a GitHub transport",
    "instructions": "Implement GitHubTransport and its tests.",
    "acceptance": ["tests pass", "no live GitHub calls"],
}

COMMENT_ONE = {
    "id": "IC_kwDOVTKNvM8AAAABeZjUqw",
    "author": {"login": "Origin"},
    "body": '{"bridge": "dsh-grokbot-v1", "type": "status"}',
    "createdAt": "2026-10-22T07:14:01Z",
    "url": (
        "https://github.com/SilasSolivagus/dsh-grokbot-bridge/issues/42"
        "#issuecomment-101"
    ),
}

COMMENT_TWO = {
    "id": "IC_kwDOVTKNvM8AAAABeZkhEw",
    "author": {"login": "grok-bot"},
    "body": "not JSON: the orchestrator validates envelopes",
    "createdAt": "2026-10-22T07:15:14Z",
    "url": (
        "https://github.com/SilasSolivagus/dsh-grokbot-bridge/issues/42"
        "#issuecomment-102"
    ),
}


def comments_json(*comments):
    """Return a ``gh issue view --json comments`` payload."""
    return json.dumps({"comments": list(comments)}, ensure_ascii=False)


def issue_body(argv):
    """Return the value following ``--body`` in a recorded command."""
    return argv[argv.index("--body") + 1]


def called_process_error(stderr="bad credentials", returncode=1):
    """Build the error the default subprocess runner raises on failure."""
    return subprocess.CalledProcessError(
        returncode,
        ["gh", "issue", "create"],
        output="",
        stderr=stderr,
    )


class FakeRunner:
    """Stand-in for ``subprocess.run`` using canned results.

    A result may be a stdout string, a ``CompletedProcess``, or an exception
    to raise. Every call is recorded as an argv list.
    """

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if not self.results:
            raise AssertionError("unexpected gh call: {!r}".format(argv))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        if isinstance(result, subprocess.CompletedProcess):
            return result
        return subprocess.CompletedProcess(argv, 0, stdout=result, stderr="")


class OpenTests(unittest.TestCase):
    def test_open_creates_an_issue_and_returns_the_number_as_a_string(self):
        runner = FakeRunner(ISSUE_URL)
        transport = GitHubTransport(REPO, runner)

        ref = transport.open(REQUEST)

        self.assertEqual(ref, "42")
        self.assertIsInstance(ref, str)
        argv = runner.calls[0]
        self.assertEqual(
            argv,
            [
                "gh",
                "issue",
                "create",
                "--repo",
                REPO,
                "--title",
                REQUEST["title"],
                "--body",
                issue_body(argv),
                "--label",
                "bridge",
                "--label",
                "task",
            ],
        )

    def test_open_body_contains_exactly_one_fenced_json_block(self):
        runner = FakeRunner(ISSUE_URL)
        transport = GitHubTransport(REPO, runner)

        transport.open(REQUEST)

        body = issue_body(runner.calls[0])
        self.assertEqual(body.count("```"), 2)
        self.assertTrue(body.startswith("```json\n"))
        self.assertTrue(body.endswith("\n```"))
        blocks = re.findall(r"```json\n(.*?)\n```", body, re.DOTALL)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(json.loads(blocks[0]), REQUEST)

    def test_open_escapes_code_fences_inside_request_strings(self):
        request = dict(
            REQUEST,
            instructions="Run this:\n```python\nprint('hi')\n```",
        )
        runner = FakeRunner(ISSUE_URL)
        transport = GitHubTransport(REPO, runner)

        transport.open(request)

        body = issue_body(runner.calls[0])
        self.assertEqual(body.count("```"), 2)
        blocks = re.findall(r"```json\n(.*?)\n```", body, re.DOTALL)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(json.loads(blocks[0]), request)

    def test_open_labels_the_issue_bridge_and_task(self):
        runner = FakeRunner(ISSUE_URL)
        transport = GitHubTransport(REPO, runner)

        transport.open(REQUEST)

        argv = runner.calls[0]
        labels = [
            argv[index + 1]
            for index, value in enumerate(argv)
            if value == "--label"
        ]
        self.assertEqual(labels, ["bridge", "task"])

    def test_open_parses_the_number_from_the_created_issue_url(self):
        runner = FakeRunner(
            "Creating issue in SilasSolivagus/dsh-grokbot-bridge\n"
            + ISSUE_URL
        )
        transport = GitHubTransport(REPO, runner)

        self.assertEqual(transport.open(REQUEST), "42")

    def test_open_raises_when_output_contains_no_issue_url(self):
        transport = GitHubTransport(REPO, FakeRunner("nothing useful\n"))

        with self.assertRaises(GitHubError) as raised:
            transport.open(REQUEST)

        self.assertIn("issue", str(raised.exception).lower())

    def test_open_raises_github_error_when_gh_fails(self):
        transport = GitHubTransport(
            REPO, FakeRunner(called_process_error("HTTP 401: Bad credentials"))
        )

        with self.assertRaises(GitHubError) as raised:
            transport.open(REQUEST)

        self.assertIsInstance(raised.exception, TransportError)
        self.assertIn("gh issue create", str(raised.exception))
        self.assertIn("Bad credentials", str(raised.exception))

    def test_open_raises_github_error_on_nonzero_completed_process(self):
        failure = subprocess.CompletedProcess(
            ["gh", "issue", "create"],
            1,
            stdout="",
            stderr="HTTP 403: Resource not accessible",
        )
        transport = GitHubTransport(REPO, FakeRunner(failure))

        with self.assertRaises(GitHubError) as raised:
            transport.open(REQUEST)

        self.assertIn("Resource not accessible", str(raised.exception))

    def test_open_requires_a_non_empty_title(self):
        runner = FakeRunner()
        transport = GitHubTransport(REPO, runner)

        with self.assertRaises(ValueError):
            transport.open({"card": "TEST-004"})

        self.assertEqual(runner.calls, [])

    def test_open_requires_a_request_object(self):
        transport = GitHubTransport(REPO, FakeRunner())

        with self.assertRaises(ValueError):
            transport.open(["not", "a", "dict"])

    def test_runner_may_return_stdout_as_a_string(self):
        transport = GitHubTransport(REPO, lambda argv: ISSUE_URL)

        self.assertEqual(transport.open(REQUEST), "42")

    def test_runner_returning_an_unsupported_value_raises_github_error(self):
        transport = GitHubTransport(REPO, lambda argv: object())

        with self.assertRaises(GitHubError):
            transport.close("42")

    @mock.patch("bridge.github_transport.subprocess.run")
    def test_default_runner_uses_argv_and_never_a_shell(self, run):
        run.return_value = subprocess.CompletedProcess(
            ["gh"], 0, stdout=ISSUE_URL, stderr=""
        )
        transport = GitHubTransport(REPO)

        transport.open(REQUEST)

        argv = run.call_args[0][0]
        kwargs = run.call_args[1]
        self.assertIsInstance(argv, list)
        self.assertEqual(argv[0], "gh")
        self.assertEqual(argv[1:3], ["issue", "create"])
        self.assertIs(kwargs["check"], True)
        self.assertIs(kwargs["capture_output"], True)
        self.assertNotIn("shell", kwargs)


class RepliesTests(unittest.TestCase):
    def test_replies_views_the_issue_comments_as_json(self):
        runner = FakeRunner(comments_json())
        transport = GitHubTransport(REPO, runner)

        transport.replies("42")

        self.assertEqual(
            runner.calls,
            [
                [
                    "gh",
                    "issue",
                    "view",
                    "42",
                    "--repo",
                    REPO,
                    "--comments",
                    "--json",
                    "comments",
                ]
            ],
        )

    def test_replies_returns_normalized_comments_and_the_highest_cursor(self):
        transport = GitHubTransport(
            REPO, FakeRunner(comments_json(COMMENT_ONE, COMMENT_TWO))
        )

        comments, cursor = transport.replies("42")

        self.assertEqual(
            comments,
            [
                {
                    "id": 101,
                    "body": COMMENT_ONE["body"],
                    "author": "Origin",
                    "createdAt": COMMENT_ONE["createdAt"],
                },
                {
                    "id": 102,
                    "body": COMMENT_TWO["body"],
                    "author": "grok-bot",
                    "createdAt": COMMENT_TWO["createdAt"],
                },
            ],
        )
        self.assertEqual(cursor, 102)

    def test_replies_filters_comments_at_or_before_the_cursor(self):
        transport = GitHubTransport(
            REPO, FakeRunner(comments_json(COMMENT_ONE, COMMENT_TWO))
        )

        comments, cursor = transport.replies("42", after=101)

        self.assertEqual([comment["id"] for comment in comments], [102])
        self.assertEqual(cursor, 102)

    def test_replies_accepts_a_numeric_string_cursor(self):
        transport = GitHubTransport(
            REPO, FakeRunner(comments_json(COMMENT_ONE, COMMENT_TWO))
        )

        comments, cursor = transport.replies("42", after="101")

        self.assertEqual([comment["id"] for comment in comments], [102])
        self.assertEqual(cursor, 102)

    def test_replies_returns_the_same_cursor_when_nothing_is_new(self):
        payload = comments_json(COMMENT_ONE, COMMENT_TWO)
        transport = GitHubTransport(REPO, FakeRunner(payload, payload))

        self.assertEqual(transport.replies("42", after=102), ([], 102))
        self.assertEqual(transport.replies("42", after=150), ([], 150))

    def test_replies_with_no_comments_keeps_the_cursor(self):
        payload = comments_json()
        transport = GitHubTransport(REPO, FakeRunner(payload, payload))

        self.assertEqual(transport.replies("42"), ([], None))
        self.assertEqual(transport.replies("42", after=7), ([], 7))

    def test_replies_does_not_validate_body_json_or_filter_authors(self):
        transport = GitHubTransport(
            REPO, FakeRunner(comments_json(COMMENT_TWO))
        )

        comments, _ = transport.replies("42")

        self.assertEqual(comments[0]["body"], COMMENT_TWO["body"])
        self.assertEqual(comments[0]["author"], "grok-bot")

    def test_replies_uses_database_id_when_gh_provides_it(self):
        comment = dict(COMMENT_ONE, databaseId=7)
        transport = GitHubTransport(REPO, FakeRunner(comments_json(comment)))

        comments, cursor = transport.replies("42")

        self.assertEqual(comments[0]["id"], 7)
        self.assertEqual(cursor, 7)

    def test_replies_accepts_a_numeric_id_without_a_url(self):
        comment = {"id": 7, "body": "hi", "author": {"login": "Origin"}}
        transport = GitHubTransport(REPO, FakeRunner(comments_json(comment)))

        comments, cursor = transport.replies("42")

        self.assertEqual(comments[0]["id"], 7)
        self.assertEqual(comments[0]["createdAt"], "")
        self.assertEqual(cursor, 7)

    def test_replies_tolerates_a_missing_author(self):
        comment = {"id": 7, "body": "hi", "author": None, "createdAt": None}
        transport = GitHubTransport(REPO, FakeRunner(comments_json(comment)))

        comments, _ = transport.replies("42")

        self.assertEqual(comments[0]["author"], "")
        self.assertEqual(comments[0]["createdAt"], "")

    def test_replies_raises_github_error_on_invalid_json(self):
        transport = GitHubTransport(REPO, FakeRunner("this is not JSON"))

        with self.assertRaises(GitHubError) as raised:
            transport.replies("42")

        self.assertIn("invalid JSON", str(raised.exception))

    def test_replies_raises_github_error_without_a_comments_list(self):
        transport = GitHubTransport(REPO, FakeRunner('{"data": []}'))

        with self.assertRaises(GitHubError):
            transport.replies("42")

    def test_replies_raises_github_error_on_a_non_object_payload(self):
        transport = GitHubTransport(REPO, FakeRunner("[]"))

        with self.assertRaises(GitHubError):
            transport.replies("42")

    def test_replies_raises_github_error_when_gh_fails(self):
        transport = GitHubTransport(
            REPO, FakeRunner(called_process_error("HTTP 502: Bad gateway"))
        )

        with self.assertRaises(GitHubError) as raised:
            transport.replies("42")

        self.assertIn("HTTP 502", str(raised.exception))

    def test_replies_requires_a_reference(self):
        runner = FakeRunner()
        transport = GitHubTransport(REPO, runner)

        with self.assertRaises(ValueError):
            transport.replies("   ")

        self.assertEqual(runner.calls, [])


class CloseTests(unittest.TestCase):
    def test_close_closes_the_issue(self):
        runner = FakeRunner("")
        transport = GitHubTransport(REPO, runner)

        result = transport.close("42")

        self.assertIsNone(result)
        self.assertEqual(
            runner.calls,
            [["gh", "issue", "close", "42", "--repo", REPO]],
        )

    def test_close_raises_github_error_when_gh_fails(self):
        transport = GitHubTransport(
            REPO, FakeRunner(called_process_error("HTTP 404: Not Found"))
        )

        with self.assertRaises(GitHubError) as raised:
            transport.close("42")

        self.assertIn("HTTP 404", str(raised.exception))

    def test_close_requires_a_reference(self):
        runner = FakeRunner()
        transport = GitHubTransport(REPO, runner)

        with self.assertRaises(ValueError):
            transport.close("")

        self.assertEqual(runner.calls, [])


class ConstructionTests(unittest.TestCase):
    def test_github_transport_is_a_transport(self):
        transport = GitHubTransport(REPO, FakeRunner())

        self.assertIsInstance(transport, Transport)

    def test_repo_must_be_a_non_empty_string(self):
        for repo in (None, "", "   ", 42):
            with self.subTest(repo=repo):
                with self.assertRaises(ValueError):
                    GitHubTransport(repo, FakeRunner())

    def test_runner_must_be_callable(self):
        with self.assertRaises(ValueError):
            GitHubTransport(REPO, "not callable")

    def test_github_error_is_a_transport_error(self):
        self.assertTrue(issubclass(GitHubError, TransportError))


if __name__ == "__main__":
    unittest.main()
