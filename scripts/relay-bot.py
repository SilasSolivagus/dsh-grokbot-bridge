#!/usr/bin/env python3
"""Standalone relay bot client for the M1.5b task bus.

This script lets a bot (for example the scheduled Grok routine) use the
self-hosted HTTP long-poll relay without importing the bridge package and
without any GitHub dependency. It only transports JSON; it does not validate
the envelope schema. The receiving orchestrator validates every reply with
``bridge.envelope``.

Commands::

    # Poll for messages newer than N and print {"messages": [...], "next": N}
    python3 scripts/relay-bot.py poll \
        --url https://relay.example.test --channel 7 --token "$RELAY_TOKEN" \
        --after 0 --wait 25

    # Post one envelope object and print {"seq": N}
    python3 scripts/relay-bot.py send \
        --url https://relay.example.test --channel 7 --token "$RELAY_TOKEN" \
        --json reply.json

    # ``--json -`` reads the envelope from stdin
    python3 scripts/relay-bot.py send ... --json -

    # Create a channel with the relay admin token (the token is printed once)
    python3 scripts/relay-bot.py create-channel \
        --url http://127.0.0.1:8787 --admin-token "$RELAY_ADMIN_TOKEN"

    # Run the relay itself on loopback (operator convenience)
    python3 scripts/relay-bot.py serve --admin-token "$RELAY_ADMIN_TOKEN"

Security rules:

* The bearer token is only sent in the ``Authorization`` header. It is never
  written to stdout, and every diagnostic is redacted before printing.
* Any non-2xx relay response, network failure, or malformed response exits
  ``1``; invalid command-line values exit ``2``.
* ``create-channel`` is the one command that prints a token, because minting a
  channel token is its entire job. Store that output securely.

Python 3.9+ standard library only.
"""

from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

__all__ = ["main"]

_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_ERROR_BYTES = 4096
_MAX_DETAIL = 200
_DEFAULT_TIMEOUT = 30.0
_DEFAULT_PORT = 8787


class RelayBotError(Exception):
    """A runtime relay or input error; printed with exit code 1."""


class UsageError(RelayBotError):
    """An invalid command-line value; printed with exit code 2."""


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------


def _text(value: Any, label: str) -> str:
    """Return a stripped non-empty string or raise :class:`UsageError`."""
    if not isinstance(value, str):
        raise UsageError("{} must be a string".format(label))
    text = value.strip()
    if not text:
        raise UsageError("{} must be a non-empty string".format(label))
    return text


def _base_url(value: Any) -> str:
    """Validate an absolute http(s) relay base URL without a trailing slash."""
    text = _text(value, "--url")
    if any(char.isspace() for char in text):
        raise UsageError("--url must not contain whitespace")
    text = text.rstrip("/")
    parts = urllib.parse.urlsplit(text)
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        raise UsageError("--url must be an absolute http(s) relay URL")
    if parts.query or parts.fragment:
        raise UsageError("--url must not contain a query or fragment")
    return text


def _channel(value: Any) -> str:
    """Validate and canonicalize a positive numeric channel id."""
    text = _text(value, "--channel")
    if not text.isascii() or not text.isdigit():
        raise UsageError("--channel must be a positive numeric channel id")
    number = int(text)
    if number <= 0:
        raise UsageError("--channel must be a positive numeric channel id")
    return str(number)


def _token(value: Any, label: str) -> str:
    """Validate a bearer token that is safe to place in a header."""
    text = _text(value, label)
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise UsageError("{} must not contain control characters".format(label))
    return text


def _cursor(value: Any, label: str) -> int:
    """Validate a non-negative integer cursor."""
    text = _text(value, label)
    if not text.isascii() or not text.isdigit():
        raise UsageError("{} must be a non-negative integer".format(label))
    return int(text)


def _wait_seconds(value: Any, label: str) -> float:
    """Validate a non-negative, finite long-poll wait in seconds."""
    if isinstance(value, bool):
        raise UsageError(
            "{} must be a non-negative number of seconds".format(label)
        )
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise UsageError(
            "{} must be a non-negative number of seconds".format(label)
        ) from None
    if not math.isfinite(number) or number < 0:
        raise UsageError(
            "{} must be a non-negative number of seconds".format(label)
        )
    return number


def _positive_timeout(value: Any) -> float:
    """Validate a positive, finite HTTP timeout in seconds."""
    if isinstance(value, bool):
        raise UsageError("--timeout must be a positive number of seconds")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise UsageError(
            "--timeout must be a positive number of seconds"
        ) from None
    if not math.isfinite(number) or number <= 0:
        raise UsageError("--timeout must be a positive number of seconds")
    return number


def _host(value: Any) -> str:
    """Validate a bind host."""
    text = _text(value, "--host")
    if any(char.isspace() for char in text):
        raise UsageError("--host must not contain whitespace")
    return text


def _port(value: Any) -> int:
    """Validate a TCP port, allowing 0 for an ephemeral port."""
    text = _text(value, "--port")
    if not text.isascii() or not text.isdigit():
        raise UsageError("--port must be an integer between 0 and 65535")
    number = int(text)
    if not 0 <= number <= 65535:
        raise UsageError("--port must be an integer between 0 and 65535")
    return number


# ---------------------------------------------------------------------------
# Redaction and diagnostics
# ---------------------------------------------------------------------------


def _redact(text: str, token: Optional[str]) -> str:
    """Remove any occurrence of ``token`` from a diagnostic."""
    if not token:
        return text
    text = text.replace(token, "***")
    quoted = urllib.parse.quote(token, safe="")
    if quoted != token:
        text = text.replace(quoted, "***")
    return text


def _detail(text: str) -> str:
    """Collapse a server-supplied detail to one short line."""
    return " ".join(text.split())[:_MAX_DETAIL]


def _error(message: str) -> None:
    """Write one prefixed error line to stderr."""
    print("relay-bot: error: {}".format(message), file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so the bearer token is never forwarded to another URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http_error(exc: urllib.error.HTTPError, token: str) -> RelayBotError:
    """Translate an ``HTTPError`` without leaking the bearer token."""
    try:
        raw = exc.read(_MAX_ERROR_BYTES)
    except Exception:  # the error body is best-effort diagnostics
        raw = b""
    finally:
        try:
            exc.close()
        except Exception:
            pass
    detail = ""
    if raw:
        body = raw.decode("utf-8", "replace").strip()
        if body:
            try:
                document = json.loads(body)
            except ValueError:
                document = None
            if isinstance(document, dict):
                for key in ("error", "message"):
                    value = document.get(key)
                    if isinstance(value, str) and value.strip():
                        detail = value.strip()
                        break
            if not detail:
                detail = body
    if not detail:
        detail = str(getattr(exc, "reason", "") or "").strip()
    message = "HTTP {}".format(exc.code)
    detail = _detail(detail)
    if detail:
        message = "{}: {}".format(message, _redact(detail, token))
    return RelayBotError(message)


def _request(
    method: str,
    url: str,
    token: str,
    timeout: float,
    data: Optional[bytes] = None,
) -> bytes:
    """Perform one JSON request and return the raw response body."""
    headers = {
        "Authorization": "Bearer {}".format(token),
        "Accept": "application/json",
    }
    if data is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(
        url, data=data, headers=headers, method=method
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise _http_error(exc, token) from None
    except urllib.error.URLError as exc:
        raise RelayBotError(
            "relay {} {} failed: {}".format(
                method, url, _redact(str(exc.reason), token)
            )
        ) from None
    except (socket.timeout, TimeoutError):
        raise RelayBotError(
            "relay {} {} timed out after {:g}s".format(
                method, url, timeout
            )
        ) from None
    except (http.client.HTTPException, OSError, ValueError) as exc:
        raise RelayBotError(
            "relay {} {} failed: {}".format(
                method, url, _redact(str(exc), token)
            )
        ) from None

    if len(raw) > _MAX_RESPONSE_BYTES:
        raise RelayBotError(
            "relay {} {} returned more than {} bytes".format(
                method, url, _MAX_RESPONSE_BYTES
            )
        )
    return raw


def _json_object(raw: bytes, source: str) -> Dict[str, Any]:
    """Decode one JSON object response or raise :class:`RelayBotError`."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise RelayBotError(
            "{} returned invalid UTF-8".format(source)
        ) from None
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise RelayBotError(
            "{} returned invalid JSON: {}".format(source, exc)
        ) from None
    if not isinstance(document, dict):
        raise RelayBotError(
            "{} returned {}, expected a JSON object".format(
                source, type(document).__name__
            )
        )
    return document


def _response_int(value: Any, label: str) -> int:
    """Read a non-negative integer field from a relay response."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise RelayBotError(
            "{} must be a non-negative integer".format(label)
        )
    if value < 0:
        raise RelayBotError(
            "{} must be a non-negative integer".format(label)
        )
    return value


def _response_text(value: Any, label: str) -> str:
    """Read a non-empty string field from a relay response."""
    if not isinstance(value, str) or not value.strip():
        raise RelayBotError("{} must be a non-empty string".format(label))
    return value.strip()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _messages_url(base_url: str, channel: str) -> str:
    """Return the messages endpoint for one channel."""
    return "{}/v1/channels/{}/messages".format(base_url, channel)


def _read_envelope(path: str) -> Dict[str, Any]:
    """Read one JSON object from ``path``, or from stdin when it is ``-``."""
    if path == "-":
        source = "stdin"
        try:
            text = sys.stdin.read()
        except OSError as exc:
            raise RelayBotError(
                "cannot read stdin: {}".format(exc)
            ) from None
    else:
        source = "--json file"
        try:
            with open(
                os.path.expanduser(path), "r", encoding="utf-8"
            ) as handle:
                text = handle.read()
        except OSError as exc:
            raise RelayBotError(
                "cannot read --json file {}: {}".format(path, exc)
            ) from None
    try:
        envelope = json.loads(text)
    except ValueError as exc:
        raise RelayBotError(
            "invalid JSON in {}: {}".format(source, exc)
        ) from None
    if not isinstance(envelope, dict):
        raise RelayBotError(
            "{} must contain one JSON object, got {}".format(
                source, type(envelope).__name__
            )
        )
    return envelope


def _poll(args: argparse.Namespace) -> Dict[str, Any]:
    """Long-poll the relay and return its normalized response."""
    query = urllib.parse.urlencode(
        {"after": args.after, "wait": args.wait}
    )
    url = "{}?{}".format(
        _messages_url(args.base_url, args.channel), query
    )
    document = _json_object(
        _request("GET", url, args.token, args.timeout),
        "relay poll response",
    )

    raw_messages = document.get("messages")
    if not isinstance(raw_messages, list):
        raise RelayBotError(
            "relay poll response is missing a 'messages' list"
        )
    next_seq = _response_int(
        document.get("next"), "relay poll response 'next'"
    )

    messages: List[Dict[str, Any]] = []
    for index, item in enumerate(raw_messages):
        if not isinstance(item, dict):
            raise RelayBotError(
                "relay poll response messages[{}] is not an object".format(
                    index
                )
            )
        seq = _response_int(
            item.get("seq"),
            "relay poll response messages[{}].seq".format(index),
        )
        envelope = item.get("envelope")
        if not isinstance(envelope, dict):
            raise RelayBotError(
                "relay poll response messages[{}].envelope is not an "
                "object".format(index)
            )
        messages.append({"seq": seq, "envelope": envelope})

    return {"messages": messages, "next": next_seq}


def _send(args: argparse.Namespace) -> Dict[str, Any]:
    """Post one envelope object and return its sequence number."""
    envelope = _read_envelope(args.json_file)
    body = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
    document = _json_object(
        _request(
            "POST",
            _messages_url(args.base_url, args.channel),
            args.token,
            args.timeout,
            data=body,
        ),
        "relay send response",
    )
    seq = _response_int(
        document.get("seq"), "relay send response 'seq'"
    )
    return {"seq": seq}


def _create_channel(args: argparse.Namespace) -> Dict[str, Any]:
    """Mint one channel and print its id, token, and API URL.

    This is the only command that intentionally writes a token to stdout; that
    token is the result the operator asked for.
    """
    url = args.base_url + "/v1/channels"
    document = _json_object(
        _request("POST", url, args.admin_token, args.timeout, data=b""),
        "relay create-channel response",
    )
    return {
        "channel": _response_text(
            document.get("channel"),
            "relay create-channel response 'channel'",
        ),
        "token": _response_text(
            document.get("token"),
            "relay create-channel response 'token'",
        ),
        "url": _response_text(
            document.get("url"), "relay create-channel response 'url'"
        ),
    }


def _serve(args: argparse.Namespace) -> int:
    """Run :class:`relay.server.RelayServer` until Ctrl-C."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from relay.server import RelayServer
    except ImportError as exc:
        raise RelayBotError(
            "cannot import relay.server: {}".format(exc)
        ) from None

    server = RelayServer(args.admin_token, host=args.host, port=args.port)
    try:
        url = server.start()
    except (OSError, ValueError) as exc:
        raise RelayBotError(
            "cannot start relay on {}:{}: {}".format(
                args.host, args.port, _redact(str(exc), args.admin_token)
            )
        ) from None

    print("relay listening on {}".format(url), flush=True)
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("relay stopped", file=sys.stderr, flush=True)
    finally:
        server.stop()
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _message_options() -> argparse.ArgumentParser:
    """Return the options shared by poll and send."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--url",
        required=True,
        metavar="URL",
        help="relay base URL, for example https://relay.example.test",
    )
    parser.add_argument(
        "--channel",
        required=True,
        metavar="ID",
        help="numeric channel id",
    )
    parser.add_argument(
        "--token",
        required=True,
        metavar="TOKEN",
        help="channel token (sent as a bearer token)",
    )
    parser.add_argument(
        "--timeout",
        default=str(_DEFAULT_TIMEOUT),
        metavar="SECONDS",
        help="HTTP timeout in seconds (default 30)",
    )
    return parser


def _parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        prog="relay-bot.py",
        description=(
            "Poll and send JSON envelopes over the M1.5 task-bus relay. "
            "No GitHub account is required."
        ),
    )
    commands = parser.add_subparsers(dest="command", metavar="command")
    commands.required = True

    poll_parser = commands.add_parser(
        "poll",
        parents=[_message_options()],
        help="long-poll for messages newer than a cursor",
        description=(
            "Print {\"messages\": [...], \"next\": N} for one poll. "
            "The relay holds an empty poll for up to --wait seconds "
            "(capped at 25)."
        ),
    )
    poll_parser.add_argument(
        "--after",
        default="0",
        metavar="N",
        help="return messages with seq > N (default 0)",
    )
    poll_parser.add_argument(
        "--wait",
        default="0",
        metavar="SECONDS",
        help="seconds the relay may hold an empty poll (default 0)",
    )

    send_parser = commands.add_parser(
        "send",
        parents=[_message_options()],
        help="post one envelope object",
        description="Post one JSON envelope and print {\"seq\": N}.",
    )
    send_parser.add_argument(
        "--json",
        dest="json_file",
        required=True,
        metavar="FILE",
        help="envelope JSON file, or - to read stdin",
    )

    create_parser = commands.add_parser(
        "create-channel",
        help="mint a channel with the relay admin token",
        description=(
            "Create one channel and print its id, channel token, and API URL. "
            "This is the only command that prints a token."
        ),
    )
    create_parser.add_argument(
        "--url",
        required=True,
        metavar="URL",
        help="relay base URL",
    )
    create_parser.add_argument(
        "--admin-token",
        required=True,
        metavar="TOKEN",
        help="relay admin token",
    )
    create_parser.add_argument(
        "--timeout",
        default=str(_DEFAULT_TIMEOUT),
        metavar="SECONDS",
        help="HTTP timeout in seconds (default 30)",
    )

    serve_parser = commands.add_parser(
        "serve",
        help="run the relay server on loopback",
        description=(
            "Start the self-hosted relay server and print its base URL. "
            "Stop it with Ctrl-C."
        ),
    )
    serve_parser.add_argument(
        "--admin-token",
        required=True,
        metavar="TOKEN",
        help="relay admin token",
    )
    serve_parser.add_argument(
        "--host",
        default="127.0.0.1",
        metavar="HOST",
        help="bind address (default 127.0.0.1)",
    )
    serve_parser.add_argument(
        "--port",
        default=str(_DEFAULT_PORT),
        metavar="PORT",
        help="bind port (default 8787; 0 picks an ephemeral port)",
    )
    return parser


def _validate(args: argparse.Namespace) -> None:
    """Validate and canonicalize parsed arguments in place."""
    if args.command in ("poll", "send"):
        args.base_url = _base_url(args.url)
        args.channel = _channel(args.channel)
        args.token = _token(args.token, "--token")
        args.timeout = _positive_timeout(args.timeout)
        if args.command == "poll":
            args.after = _cursor(args.after, "--after")
            args.wait = _wait_seconds(args.wait, "--wait")
        else:
            args.json_file = _text(args.json_file, "--json")
    elif args.command == "create-channel":
        args.base_url = _base_url(args.url)
        args.admin_token = _token(args.admin_token, "--admin-token")
        args.timeout = _positive_timeout(args.timeout)
    elif args.command == "serve":
        args.admin_token = _token(args.admin_token, "--admin-token")
        args.host = _host(args.host)
        args.port = _port(args.port)


_TOKEN_OPTIONS = ("--token", "--admin-token")
_OTHER_OPTIONS = frozenset(
    (
        "--url",
        "--channel",
        "--after",
        "--wait",
        "--json",
        "--timeout",
        "--host",
        "--port",
        "--help",
        "-h",
    )
)


def _normalize_argv(items: List[str]) -> List[str]:
    """Join token options to their values with ``=``.

    Relay tokens are URL-safe random strings, so a channel token can begin
    with ``-``. Argparse would mistake such a separate value for an option;
    ``--token=-abc`` is unambiguous. Only the two token options need this
    treatment, and a following known option is still reported as a missing
    value.
    """
    normalized: List[str] = []
    index = 0
    while index < len(items):
        item = items[index]
        if (
            item in _TOKEN_OPTIONS
            and index + 1 < len(items)
            and items[index + 1] not in _OTHER_OPTIONS
        ):
            normalized.append("{}={}".format(item, items[index + 1]))
            index += 2
            continue
        normalized.append(item)
        index += 1
    return normalized


def _parse_args(argv: Optional[List[str]]) -> argparse.Namespace:
    """Normalize token values, then parse the command line."""
    items = list(sys.argv[1:] if argv is None else argv)
    return _parser().parse_args(_normalize_argv(items))


def _write_json(payload: Dict[str, Any]) -> None:
    """Write one compact JSON line to stdout as UTF-8 bytes."""
    text = json.dumps(payload, ensure_ascii=False) + "\n"
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        buffer.write(text.encode("utf-8"))
        buffer.flush()
    else:  # pragma: no cover - only for callers that replace sys.stdout
        sys.stdout.write(text)
        sys.stdout.flush()


def main(argv: Optional[List[str]] = None) -> int:
    """Run the relay bot CLI and return a process exit code."""
    args = _parse_args(argv)
    token = getattr(args, "token", None) or getattr(args, "admin_token", None)

    try:
        _validate(args)
        if args.command == "serve":
            return _serve(args)
        if args.command == "poll":
            payload = _poll(args)
        elif args.command == "send":
            payload = _send(args)
        elif args.command == "create-channel":
            payload = _create_channel(args)
        else:  # argparse restricts the command set
            raise UsageError("unknown command: {}".format(args.command))
    except UsageError as exc:
        _error(_redact(str(exc), token))
        return 2
    except RelayBotError as exc:
        _error(_redact(str(exc), token))
        return 1
    except Exception as exc:  # CLI boundary: never dump a traceback or token
        _error(
            _redact(
                "{}: {}".format(type(exc).__name__, exc),
                token,
            )
        )
        return 1

    try:
        _write_json(payload)
    except (OSError, ValueError):
        # stdout was closed, for example by a downstream ``head``.
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
