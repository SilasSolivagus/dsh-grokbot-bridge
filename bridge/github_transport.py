"""GitHub issue transport for the M1.5 task bus.

One validated task request maps to one GitHub issue. The issue body is exactly
one fenced ``json`` block containing the request, so the issue reads as a
single machine message; replies are the issue comments. Envelope validation,
author allowlisting, and interpretation of reply bodies remain orchestrator
concerns, so this adapter returns comment text untouched.

The adapter does not filter authors or rewrite reply envelopes. It normalizes
each comment to ``{id, body, author, createdAt}`` and uses the comment id as
the reply cursor. ``gh issue view --json comments`` reports GraphQL node ids
(for example ``IC_kwDO...``), which are neither numeric nor ordered, while the
bridge state stores a numeric comment id. The numeric database id is the
trailing number of each comment URL, so the adapter uses that number when it is
available and falls back to ``databaseId`` or a numeric ``id``.

``gh`` runs through an injectable runner that receives an argv list; there is
no shell. The default runner calls::

    subprocess.run(argv, check=True, capture_output=True, text=True)

A failed command, unusable stdout, or a non-zero ``CompletedProcess`` raises
:class:`GitHubError`, which is a :class:`~bridge.transport.TransportError`.
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from bridge.transport import Transport, TransportError

__all__ = ["GitHubError", "GitHubTransport"]

LABELS = ("bridge", "task")

_ISSUE_URL = re.compile(r"/issues/([0-9]+)")
_COMMENT_URL_ID = re.compile(r"#issuecomment-([0-9]+)")

Runner = Callable[[List[str]], Any]
Comment = Dict[str, Any]


class GitHubError(TransportError):
    """Raised when a ``gh`` command fails or returns unusable output."""


def _text(value: Any, label: str) -> str:
    """Validate a caller-provided string and return it stripped."""
    if not isinstance(value, str):
        raise ValueError(
            "{} must be a string, got {}".format(label, type(value).__name__)
        )
    text = value.strip()
    if not text:
        raise ValueError("{} must be a non-empty string".format(label))
    return text


def _default_runner(argv: List[str]) -> subprocess.CompletedProcess:
    """Run one ``gh`` command without a shell."""
    return subprocess.run(
        argv,
        check=True,
        capture_output=True,
        text=True,
    )


def _stderr_text(stderr: Any) -> str:
    """Return stderr as stripped text, tolerating bytes and ``None``."""
    if stderr is None:
        return ""
    if isinstance(stderr, bytes):
        return stderr.decode("utf-8", "replace").strip()
    return str(stderr).strip()


def _command_failure(
    argv: List[str],
    returncode: Optional[int],
    stderr: Any,
) -> GitHubError:
    """Build the error for a failed ``gh`` invocation."""
    message = "gh command failed (exit {}): {}".format(
        returncode, " ".join(argv)
    )
    detail = _stderr_text(stderr)
    if detail:
        message = "{}: {}".format(message, detail)
    return GitHubError(message)


def _load_json(stdout: str, source: str) -> Any:
    """Parse one JSON document from command stdout."""
    text = stdout.strip()
    if not text:
        raise GitHubError("{} returned no output".format(source))
    try:
        return json.loads(text)
    except ValueError as exc:
        raise GitHubError(
            "{} returned invalid JSON: {}".format(source, exc)
        ) from None


def _issue_body(request: Dict[str, Any]) -> str:
    """Render one request as exactly one fenced JSON block.

    A backtick inside a string value would add another fence and break the
    one-block invariant. ``\\u0060`` decodes to the same string and cannot
    appear in JSON syntax outside a string.
    """
    payload = json.dumps(request, ensure_ascii=False, indent=2)
    payload = payload.replace("`", "\\u0060")
    return "```json\n{}\n```".format(payload)


def _issue_number(stdout: str) -> int:
    """Extract the created issue number from ``gh issue create`` output."""
    matches = _ISSUE_URL.findall(stdout)
    if not matches:
        raise GitHubError(
            "gh issue create did not return an issue URL: {!r}".format(
                stdout.strip()
            )
        )
    return int(matches[-1])


def _as_int(value: Any) -> Optional[int]:
    """Return ``value`` as an integer when it is a numeric id."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
    return None


def _comment_id(comment: Dict[str, Any]) -> Union[int, str]:
    """Return a comment's database id, with a tolerant fallback."""
    database_id = _as_int(comment.get("databaseId"))
    if database_id is not None:
        return database_id

    numeric_id = _as_int(comment.get("id"))
    if numeric_id is not None:
        return numeric_id

    url = comment.get("url")
    if isinstance(url, str):
        match = _COMMENT_URL_ID.search(url)
        if match:
            return int(match.group(1))

    raw_id = comment.get("id")
    if isinstance(raw_id, str) and raw_id.strip():
        return raw_id.strip()

    raise GitHubError("gh issue view comment has no usable id")


def _id_key(value: Union[int, str]) -> Tuple[int, Any]:
    """Sort numeric ids before opaque string ids."""
    number = _as_int(value)
    if number is not None:
        return (0, number)
    return (1, str(value))


def _is_after(
    comment_id: Union[int, str],
    after: Optional[Union[int, str]],
) -> bool:
    """Return whether ``comment_id`` is newer than the ``after`` cursor."""
    if after is None:
        return True
    after_number = _as_int(after)
    id_number = _as_int(comment_id)
    if after_number is not None and id_number is not None:
        return id_number > after_number
    return str(comment_id) > str(after)


def _author_login(author: Any) -> str:
    """Return the login from a gh comment author, or an empty string."""
    if isinstance(author, dict):
        author = author.get("login")
    return author if isinstance(author, str) else ""


def _string(value: Any) -> str:
    """Return a string field unchanged, or an empty string when absent."""
    return value if isinstance(value, str) else ""


def _comment(raw: Any) -> Comment:
    """Normalize one ``gh`` comment to the transport shape."""
    if not isinstance(raw, dict):
        raise GitHubError("gh issue view returned a non-object comment")
    return {
        "id": _comment_id(raw),
        "body": _string(raw.get("body")),
        "author": _author_login(raw.get("author")),
        "createdAt": _string(raw.get("createdAt")),
    }


class GitHubTransport(Transport):
    """Transport backed by one GitHub issue per task request.

    ``repo`` is an ``owner/name`` slug passed to every ``gh`` call, so the
    adapter does not depend on the current directory. ``runner`` receives an
    argv list and may return stdout as a string or a ``CompletedProcess``.
    """

    def __init__(self, repo: Any, runner: Optional[Runner] = None) -> None:
        self.repo = _text(repo, "repo")
        if runner is not None and not callable(runner):
            raise ValueError("runner must be callable")
        self._runner: Runner = (
            runner if runner is not None else _default_runner
        )

    def __repr__(self) -> str:
        return "GitHubTransport(repo={!r})".format(self.repo)

    # -- public API -----------------------------------------------------

    def open(self, request: Dict[str, Any]) -> str:
        """Create the issue for ``request``; return its number as a string."""
        if not isinstance(request, dict):
            raise ValueError("request must be a dict")
        title = _text(request.get("title"), "request title")
        args = [
            "issue",
            "create",
            "--repo",
            self.repo,
            "--title",
            title,
            "--body",
            _issue_body(request),
        ]
        for label in LABELS:
            args.extend(("--label", label))
        return str(_issue_number(self._run(args)))

    def replies(
        self,
        ref: str,
        after: Optional[Union[int, str]] = None,
    ) -> Tuple[List[Comment], Optional[Union[int, str]]]:
        """Return comments newer than ``after`` and the next cursor."""
        issue = _text(ref, "issue reference")
        stdout = self._run(
            [
                "issue",
                "view",
                issue,
                "--repo",
                self.repo,
                "--comments",
                "--json",
                "comments",
            ]
        )
        data = _load_json(stdout, "gh issue view")
        if not isinstance(data, dict):
            raise GitHubError(
                "gh issue view returned {}, expected a JSON object".format(
                    type(data).__name__
                )
            )
        raw_comments = data.get("comments")
        if not isinstance(raw_comments, list):
            raise GitHubError("gh issue view response has no comments list")

        comments = [_comment(raw) for raw in raw_comments]
        new_comments = [
            comment
            for comment in comments
            if _is_after(comment["id"], after)
        ]

        cursor = after
        if comments:
            highest = max(
                (comment["id"] for comment in comments), key=_id_key
            )
            if _is_after(highest, after):
                cursor = highest
        return new_comments, cursor

    def close(self, ref: str) -> None:
        """Close the issue identified by ``ref``."""
        issue = _text(ref, "issue reference")
        self._run(["issue", "close", issue, "--repo", self.repo])

    # -- internals ------------------------------------------------------

    def _run(self, args: List[str]) -> str:
        """Run one ``gh`` command and return stdout, translating failures."""
        argv = ["gh"] + list(args)
        try:
            result = self._runner(argv)
        except subprocess.CalledProcessError as exc:
            raise _command_failure(
                argv, exc.returncode, exc.stderr
            ) from None
        except OSError as exc:
            raise GitHubError("cannot run gh: {}".format(exc)) from None

        if isinstance(result, str):
            return result
        if isinstance(result, subprocess.CompletedProcess):
            if result.returncode != 0:
                raise _command_failure(
                    argv, result.returncode, result.stderr
                )
            stdout = result.stdout
            if stdout is None:
                return ""
            if isinstance(stdout, bytes):
                return stdout.decode("utf-8", "replace")
            if isinstance(stdout, str):
                return stdout
            raise GitHubError(
                "gh command returned unexpected stdout type: {}".format(
                    type(stdout).__name__
                )
            )
        fmt = "gh runner returned {}, expected stdout or CompletedProcess"
        raise GitHubError(fmt.format(type(result).__name__))
