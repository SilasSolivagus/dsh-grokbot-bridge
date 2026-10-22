"""Runnable entrypoint for the M1.5 DSH <-> Grok Bot task bridge.

One process runs one transport adapter for one builder agent. The entrypoint
only reads configuration, wires the injected collaborators, and either runs a
single poll cycle (``--once``) or loops with a fixed interval::

    # GitHub issue bus (M1.5a)
    DSH_KANBAN_URL=http://127.0.0.1:61849/kanban-api \
    DSH_KANBAN_BUILDER_TOKEN=... \
    BRIDGE_TRANSPORT=github \
    BRIDGE_REPO=owner/repo \
    GROK_GITHUB_LOGIN=grok-bot \
    GROK_ROUTINE_WEBHOOK=https://... \
    GROK_ROUTINE_KEY=... \
    python3 -m bridge.main --once

    # Self-hosted relay (M1.5b, the bot polls the relay)
    DSH_KANBAN_URL=http://127.0.0.1:61849/kanban-api \
    DSH_KANBAN_BUILDER_TOKEN=... \
    BRIDGE_TRANSPORT=relay \
    RELAY_URL=https://relay.example.test \
    RELAY_CHANNEL=7 \
    RELAY_TOKEN=... \
    python3 -m bridge.main

Environment (read by :func:`load_config`):

* ``DSH_KANBAN_URL`` -- DSH Kanban API root, including ``/kanban-api``.
* ``DSH_KANBAN_BUILDER_TOKEN`` -- builder agent token.
* ``BRIDGE_TRANSPORT`` -- ``github`` or ``relay`` (required; no default).
* ``BRIDGE_STATE_FILE`` -- state path (default
  ``~/.dsh/kanban-bridge-state.json``).
* ``BRIDGE_BUILDER_AGENT`` -- DSH agent id to serve (default ``builder``).
* ``BRIDGE_INTERVAL`` -- seconds between cycles in loop mode (default ``15``).
* GitHub: ``BRIDGE_REPO``, ``GROK_GITHUB_LOGIN``, ``GROK_ROUTINE_WEBHOOK``,
  ``GROK_ROUTINE_KEY``.
* Relay: ``RELAY_URL``, ``RELAY_CHANNEL``, ``RELAY_TOKEN``; the routine pair
  ``GROK_ROUTINE_WEBHOOK``/``GROK_ROUTINE_KEY`` is optional. Without it a
  :class:`NoopRoutine` is used because the bot polls the relay on its own.

Security rules:

* A missing or blank required key raises :class:`ConfigError` naming the key.
  Values are never copied into an error message, so a token or key cannot leak
  through configuration diagnostics.
* The entrypoint logs only the transport name, the interval, and per-cycle
  counters. It never logs the config mapping, tokens, or routine keys.

Tests inject ``deps`` into :func:`build_orchestrator`; no live DSH, GitHub,
relay, or Grok endpoint is contacted here.
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import time
from typing import Any, Callable, Dict, Mapping, Optional

from bridge.dsh import DshClient
from bridge.github_transport import GitHubTransport
from bridge.orchestrator import Orchestrator
from bridge.relay_transport import RelayTransport
from bridge.routine import RoutineClient
from bridge.state import BridgeState

__all__ = [
    "ConfigError",
    "NoopRoutine",
    "build_orchestrator",
    "load_config",
    "main",
]

LOGGER = logging.getLogger(__name__)
LOGGER.addHandler(logging.NullHandler())

DEFAULT_STATE_FILE = "~/.dsh/kanban-bridge-state.json"
DEFAULT_BUILDER_AGENT = "builder"
DEFAULT_INTERVAL = 15.0
SUPPORTED_TRANSPORTS = ("github", "relay")

GITHUB_REQUIRED = (
    "BRIDGE_REPO",
    "GROK_GITHUB_LOGIN",
    "GROK_ROUTINE_WEBHOOK",
    "GROK_ROUTINE_KEY",
)
RELAY_REQUIRED = ("RELAY_URL", "RELAY_CHANNEL", "RELAY_TOKEN")
RELAY_ROUTINE_KEYS = ("GROK_ROUTINE_WEBHOOK", "GROK_ROUTINE_KEY")

TRANSPORT_FLAG = "BRIDGE_TRANSPORT"


class ConfigError(ValueError):
    """Raised when the bridge configuration is missing or invalid.

    The message names the offending environment key but never includes the
    value, because values can be tokens or keys.
    """


class NoopRoutine:
    """Routine stand-in for relay mode when the bot polls the relay itself.

    The orchestrator still calls ``wake`` after opening a thread; returning an
    empty mapping records that no remote run was started.
    """

    def wake(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Do nothing and report that no routine run was created."""
        return {}

    def __repr__(self) -> str:
        return "NoopRoutine()"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _value(environment: Mapping[str, Any], key: str) -> str:
    """Return a stripped environment value; missing and blank yield ``""``.

    Non-string values are treated as missing: a process environment is always
    text, so accepting other types would hide a caller mistake.
    """
    raw = environment.get(key)
    if not isinstance(raw, str):
        return ""
    return raw.strip()


def _required(environment: Mapping[str, Any], key: str) -> str:
    """Return a required value or raise :class:`ConfigError` naming the key."""
    value = _value(environment, key)
    if not value:
        raise ConfigError("missing required config key: {}".format(key))
    return value


def _optional(environment: Mapping[str, Any], key: str, default: str) -> str:
    """Return an optional value, falling back to ``default`` when blank."""
    return _value(environment, key) or default


def _interval(environment: Mapping[str, Any]) -> float:
    """Read ``BRIDGE_INTERVAL`` as a positive, finite number of seconds."""
    raw = _value(environment, "BRIDGE_INTERVAL")
    if not raw:
        return DEFAULT_INTERVAL
    try:
        seconds = float(raw)
    except ValueError:
        raise ConfigError(
            "BRIDGE_INTERVAL must be a positive number of seconds"
        ) from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise ConfigError(
            "BRIDGE_INTERVAL must be a positive number of seconds"
        )
    return seconds


def _relay_routine(
    environment: Mapping[str, Any],
) -> Dict[str, str]:
    """Return the optional relay routine pair, requiring both keys together."""
    webhook = _value(environment, RELAY_ROUTINE_KEYS[0])
    key = _value(environment, RELAY_ROUTINE_KEYS[1])
    if not webhook and not key:
        return {}
    if not webhook or not key:
        missing = (
            RELAY_ROUTINE_KEYS[0] if not webhook else RELAY_ROUTINE_KEYS[1]
        )
        raise ConfigError(
            "relay routine config requires both {} and {}; missing {}".format(
                RELAY_ROUTINE_KEYS[0], RELAY_ROUTINE_KEYS[1], missing
            )
        )
    return {"routine_webhook": webhook, "routine_key": key}


def load_config(
    environment: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Read and validate bridge configuration from ``environment``.

    ``environment`` defaults to :data:`os.environ`. The returned mapping uses
    lower-case keys (``kanban_url``, ``kanban_token``, ``transport``, ...).
    Only the keys that belong to the selected transport are present; the relay
    routine pair appears only when both of its variables are set.
    """
    env = os.environ if environment is None else environment

    transport = _required(env, TRANSPORT_FLAG).lower()
    if transport not in SUPPORTED_TRANSPORTS:
        raise ConfigError(
            "unknown {} {!r}; expected one of: {}".format(
                TRANSPORT_FLAG, transport, ", ".join(SUPPORTED_TRANSPORTS)
            )
        )

    config: Dict[str, Any] = {
        "transport": transport,
        "kanban_url": _required(env, "DSH_KANBAN_URL"),
        "kanban_token": _required(env, "DSH_KANBAN_BUILDER_TOKEN"),
        "state_file": os.path.expanduser(
            _optional(env, "BRIDGE_STATE_FILE", DEFAULT_STATE_FILE)
        ),
        "builder_agent": _optional(
            env, "BRIDGE_BUILDER_AGENT", DEFAULT_BUILDER_AGENT
        ),
        "interval": _interval(env),
    }

    if transport == "github":
        config["repo"] = _required(env, GITHUB_REQUIRED[0])
        config["github_login"] = _required(env, GITHUB_REQUIRED[1])
        config["routine_webhook"] = _required(env, GITHUB_REQUIRED[2])
        config["routine_key"] = _required(env, GITHUB_REQUIRED[3])
    else:
        config["relay_url"] = _required(env, RELAY_REQUIRED[0])
        config["relay_channel"] = _required(env, RELAY_REQUIRED[1])
        config["relay_token"] = _required(env, RELAY_REQUIRED[2])
        config.update(_relay_routine(env))

    return config


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def _transport_override(
    deps: Mapping[str, Any], mode_key: str
) -> Optional[Any]:
    """Return an explicitly injected transport, if any.

    ``transport`` overrides both modes; ``github_transport`` and
    ``relay_transport`` apply to the matching mode only. ``None`` means the
    real adapter should be constructed.
    """
    injected = deps.get("transport")
    if injected is not None:
        return injected
    return deps.get(mode_key)


def build_orchestrator(
    config: Mapping[str, Any],
    deps: Optional[Mapping[str, Any]] = None,
) -> Any:
    """Wire one orchestrator for ``config``.

    ``deps`` may inject any collaborator so tests never construct a live
    client:

    * ``orchestrator`` -- returned unchanged, bypassing all wiring.
    * ``dsh`` / ``state`` -- replace :class:`DshClient` / :class:`BridgeState`.
    * ``transport`` -- replace the transport for either mode.
    * ``github_transport`` / ``relay_transport`` -- replace the mode's
      transport.
    * ``routine`` -- replace the routine in either mode.
    * ``new_id`` -- passed to :class:`Orchestrator` for message ids.

    GitHub mode uses ``GROK_GITHUB_LOGIN`` as the only allowed reply author and
    a :class:`RoutineClient`. Relay mode uses ``BRIDGE_BUILDER_AGENT`` as the
    allowed author and falls back to :class:`NoopRoutine` unless a routine pair
    is configured or one is injected.
    """
    overrides: Dict[str, Any] = {} if deps is None else dict(deps)

    injected = overrides.get("orchestrator")
    if injected is not None:
        return injected

    transport_name = config.get("transport")
    if transport_name not in SUPPORTED_TRANSPORTS:
        raise ConfigError(
            "unknown {} {!r}; expected one of: {}".format(
                TRANSPORT_FLAG, transport_name, ", ".join(SUPPORTED_TRANSPORTS)
            )
        )

    dsh = overrides.get("dsh")
    if dsh is None:
        dsh = DshClient(config["kanban_url"], config["kanban_token"])

    state = overrides.get("state")
    if state is None:
        state = BridgeState(config["state_file"])

    routine = overrides.get("routine")

    if transport_name == "github":
        transport = _transport_override(overrides, "github_transport")
        if transport is None:
            transport = GitHubTransport(config["repo"])
        allowed_logins = [config["github_login"]]
        if routine is None:
            routine = RoutineClient(
                config["routine_webhook"], config["routine_key"]
            )
    else:
        transport = _transport_override(overrides, "relay_transport")
        if transport is None:
            transport = RelayTransport(
                config["relay_url"],
                config["relay_channel"],
                config["relay_token"],
            )
        allowed_logins = [config["builder_agent"]]
        if routine is None:
            webhook = config.get("routine_webhook")
            key = config.get("routine_key")
            if webhook and key:
                routine = RoutineClient(webhook, key)
            else:
                routine = NoopRoutine()

    return Orchestrator(
        dsh,
        transport,
        routine,
        state,
        builder_agent=config["builder_agent"],
        allowed_logins=allowed_logins,
        new_id=overrides.get("new_id"),
    )


# ---------------------------------------------------------------------------
# Run loop
# ---------------------------------------------------------------------------


def _log_counts(counts: Any) -> None:
    """Log per-cycle counters without touching any configuration value."""
    if not isinstance(counts, dict):
        LOGGER.info("cycle complete")
        return
    LOGGER.info(
        "cycle complete: opened=%s applied=%s skipped=%s errors=%s",
        counts.get("opened", 0),
        counts.get("applied", 0),
        counts.get("skipped", 0),
        counts.get("errors", 0),
    )


def _parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        prog="bridge.main",
        description="Run the DSH <-> Grok Bot task bridge.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run exactly one poll cycle and exit",
    )
    return parser


def main(
    argv: Optional[list] = None,
    *,
    env: Optional[Mapping[str, Any]] = None,
    deps: Optional[Mapping[str, Any]] = None,
    build: Optional[Callable[..., Any]] = None,
    sleep: Optional[Callable[[float], None]] = None,
) -> int:
    """Run the bridge; return a process exit code.

    ``--once`` runs one cycle and returns ``0`` (a cycle exception propagates
    so a failed one-shot run is visible to the caller). Otherwise cycles repeat
    every ``BRIDGE_INTERVAL`` seconds; a failed cycle is logged and retried,
    and ``Ctrl-C`` stops the bridge with exit code ``130``.

    ``env``, ``deps``, ``build``, and ``sleep`` are injection seams for tests;
    production code uses the process environment, the real adapters, and
    :func:`time.sleep`.
    """
    args = _parser().parse_args(argv)
    config = load_config(os.environ if env is None else env)
    builder = build_orchestrator if build is None else build
    orchestrator = builder(config, deps=deps)

    if args.once:
        _log_counts(orchestrator.run_once())
        return 0

    sleeper = time.sleep if sleep is None else sleep
    LOGGER.info(
        "bridge started: transport=%s interval=%ss",
        config["transport"],
        config["interval"],
    )
    try:
        while True:
            try:
                counts = orchestrator.run_once()
            except Exception as exc:  # keep looping; adapters log details
                LOGGER.error("bridge cycle failed: %s", exc)
            else:
                _log_counts(counts)
            sleeper(config["interval"])
    except KeyboardInterrupt:
        LOGGER.info("bridge stopped")
        return 130


def _entrypoint() -> int:
    """Configure logging for ``python3 -m bridge.main`` and run the bridge."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return main()


if __name__ == "__main__":  # pragma: no cover - exercised as a module
    raise SystemExit(_entrypoint())
