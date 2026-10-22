"""Tests for the bridge entrypoint (M1.5b Task M1).

Every dependency is in-memory: no DSH server, GitHub repository, relay, or
Grok routine is contacted. ``build_orchestrator`` accepts injected deps so the
transport selection and routine defaults can be checked without network I/O.

The tests also pin the security invariants of the entrypoint: a config error
names the missing key but never echoes a secret value, and a normal run logs no
token or key.
"""

import contextlib
import io
import os
import unittest

from bridge.github_transport import GitHubTransport
from bridge.main import (
    ConfigError,
    NoopRoutine,
    build_orchestrator,
    load_config,
    main,
)
from bridge.orchestrator import Orchestrator
from bridge.relay_transport import RelayTransport
from bridge.routine import RoutineClient

STATE_DEFAULT = os.path.expanduser("~/.dsh/kanban-bridge-state.json")

KANBAN_URL = "http://127.0.0.1:61849/kanban-api"
KANBAN_TOKEN = "dsh-builder-token-canary"
ROUTINE_WEBHOOK = "https://routine.example.test/automations/hook"
ROUTINE_KEY = "routine-key-canary"
RELAY_URL = "https://relay.example.test"
RELAY_CHANNEL = "7"
RELAY_TOKEN = "relay-token-canary"
GITHUB_LOGIN = "Origin"
REPO = "SilasSolivagus/dsh-grokbot-bridge"

GITHUB_ENV = {
    "DSH_KANBAN_URL": KANBAN_URL,
    "DSH_KANBAN_BUILDER_TOKEN": KANBAN_TOKEN,
    "BRIDGE_TRANSPORT": "github",
    "BRIDGE_REPO": REPO,
    "GROK_GITHUB_LOGIN": GITHUB_LOGIN,
    "GROK_ROUTINE_WEBHOOK": ROUTINE_WEBHOOK,
    "GROK_ROUTINE_KEY": ROUTINE_KEY,
}

RELAY_ENV = {
    "DSH_KANBAN_URL": KANBAN_URL,
    "DSH_KANBAN_BUILDER_TOKEN": KANBAN_TOKEN,
    "BRIDGE_TRANSPORT": "relay",
    "RELAY_URL": RELAY_URL,
    "RELAY_CHANNEL": RELAY_CHANNEL,
    "RELAY_TOKEN": RELAY_TOKEN,
}


class FakeDsh:
    """Placeholder for an injected DSH client."""


class FakeState:
    """Placeholder for an injected bridge state."""


class FakeTransport:
    """Placeholder for an injected transport."""


class FakeRoutine:
    """Placeholder for an injected routine client."""

    def wake(self, request):
        return {"runUuid": "run-1"}


class FakeOrchestrator:
    """Runnable stand-in that records each cycle."""

    def __init__(self):
        self.calls = 0

    def run_once(self):
        self.calls += 1
        return {"opened": 0, "applied": 0, "skipped": 0, "errors": 0}


class LoadConfigTests(unittest.TestCase):
    def test_valid_github_config(self):
        config = load_config(GITHUB_ENV)

        self.assertEqual(config["transport"], "github")
        self.assertEqual(config["kanban_url"], KANBAN_URL)
        self.assertEqual(config["kanban_token"], KANBAN_TOKEN)
        self.assertEqual(config["repo"], REPO)
        self.assertEqual(config["github_login"], GITHUB_LOGIN)
        self.assertEqual(config["routine_webhook"], ROUTINE_WEBHOOK)
        self.assertEqual(config["routine_key"], ROUTINE_KEY)
        self.assertEqual(config["builder_agent"], "builder")
        self.assertEqual(config["state_file"], STATE_DEFAULT)
        self.assertEqual(config["interval"], 15.0)
        self.assertNotIn("relay_url", config)
        self.assertNotIn("relay_token", config)

    def test_defaults_and_overrides(self):
        env = dict(
            GITHUB_ENV,
            BRIDGE_BUILDER_AGENT="origin-builder",
            BRIDGE_STATE_FILE="~/state/bridge.json",
            BRIDGE_INTERVAL="2.5",
        )
        config = load_config(env)

        self.assertEqual(config["builder_agent"], "origin-builder")
        self.assertEqual(
            config["state_file"], os.path.expanduser("~/state/bridge.json")
        )
        self.assertEqual(config["interval"], 2.5)

    def test_missing_required_keys_name_the_key_without_leaking_values(self):
        for key in (
            "DSH_KANBAN_URL",
            "DSH_KANBAN_BUILDER_TOKEN",
            "BRIDGE_TRANSPORT",
            "BRIDGE_REPO",
            "GROK_GITHUB_LOGIN",
            "GROK_ROUTINE_WEBHOOK",
            "GROK_ROUTINE_KEY",
        ):
            with self.subTest(key=key):
                env = dict(GITHUB_ENV)
                del env[key]
                with self.assertRaises(ConfigError) as caught:
                    load_config(env)
                message = str(caught.exception)
                self.assertIn(key, message)
                self.assertNotIn(KANBAN_TOKEN, message)
                self.assertNotIn(ROUTINE_KEY, message)

    def test_blank_required_key_is_missing(self):
        env = dict(GITHUB_ENV, BRIDGE_REPO="   ")
        with self.assertRaises(ConfigError) as caught:
            load_config(env)
        self.assertIn("BRIDGE_REPO", str(caught.exception))

    def test_unknown_transport_is_rejected(self):
        env = dict(GITHUB_ENV, BRIDGE_TRANSPORT="carrier-pigeon")
        with self.assertRaises(ConfigError) as caught:
            load_config(env)
        message = str(caught.exception)
        self.assertIn("BRIDGE_TRANSPORT", message)
        self.assertIn("github", message)
        self.assertIn("relay", message)

    def test_invalid_interval_is_rejected(self):
        for value in ("0", "-3", "soon", "nan", "inf"):
            with self.subTest(value=value):
                env = dict(GITHUB_ENV, BRIDGE_INTERVAL=value)
                with self.assertRaises(ConfigError) as caught:
                    load_config(env)
                self.assertIn("BRIDGE_INTERVAL", str(caught.exception))

    def test_valid_relay_config_without_routine(self):
        config = load_config(RELAY_ENV)

        self.assertEqual(config["transport"], "relay")
        self.assertEqual(config["relay_url"], RELAY_URL)
        self.assertEqual(config["relay_channel"], RELAY_CHANNEL)
        self.assertEqual(config["relay_token"], RELAY_TOKEN)
        self.assertEqual(config["builder_agent"], "builder")
        self.assertNotIn("routine_webhook", config)
        self.assertNotIn("routine_key", config)
        self.assertNotIn("repo", config)
        self.assertNotIn("github_login", config)

    def test_relay_config_accepts_optional_routine(self):
        env = dict(
            RELAY_ENV,
            GROK_ROUTINE_WEBHOOK=ROUTINE_WEBHOOK,
            GROK_ROUTINE_KEY=ROUTINE_KEY,
        )
        config = load_config(env)

        self.assertEqual(config["routine_webhook"], ROUTINE_WEBHOOK)
        self.assertEqual(config["routine_key"], ROUTINE_KEY)

    def test_relay_config_rejects_a_half_configured_routine(self):
        cases = (
            ("GROK_ROUTINE_WEBHOOK", "GROK_ROUTINE_KEY"),
            ("GROK_ROUTINE_KEY", "GROK_ROUTINE_WEBHOOK"),
        )
        for provided, missing in cases:
            with self.subTest(missing=missing):
                env = dict(RELAY_ENV, **{provided: "configured"})
                with self.assertRaises(ConfigError) as caught:
                    load_config(env)
                self.assertIn(missing, str(caught.exception))

    def test_github_config_still_requires_routine(self):
        env = dict(GITHUB_ENV)
        del env["GROK_ROUTINE_KEY"]
        with self.assertRaises(ConfigError) as caught:
            load_config(env)
        self.assertIn("GROK_ROUTINE_KEY", str(caught.exception))


class NoopRoutineTests(unittest.TestCase):
    def test_wake_returns_an_empty_mapping(self):
        routine = NoopRoutine()
        self.assertEqual(routine.wake({"type": "task_request"}), {})


class BuildOrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.dsh = FakeDsh()
        self.state = FakeState()

    def _github_deps(self):
        transport = FakeTransport()
        return {
            "dsh": self.dsh,
            "state": self.state,
            "github_transport": transport,
            "routine": FakeRoutine(),
        }

    def test_github_mode_selects_github_transport(self):
        config = load_config(GITHUB_ENV)
        deps = self._github_deps()
        orchestrator = build_orchestrator(config, deps)

        self.assertIsInstance(orchestrator, Orchestrator)
        self.assertIs(orchestrator.dsh, self.dsh)
        self.assertIs(orchestrator.state, self.state)
        self.assertIs(orchestrator.transport, deps["github_transport"])
        self.assertIs(orchestrator.routine, deps["routine"])
        self.assertEqual(orchestrator.builder_agent, "builder")
        self.assertEqual(orchestrator.allowed_logins, frozenset({"origin"}))

    def test_github_mode_builds_a_github_transport_by_default(self):
        config = load_config(GITHUB_ENV)
        orchestrator = build_orchestrator(
            config, {"dsh": self.dsh, "state": self.state}
        )
        self.assertIsInstance(orchestrator.transport, GitHubTransport)

    def test_github_mode_builds_a_routine_client_by_default(self):
        config = load_config(GITHUB_ENV)
        orchestrator = build_orchestrator(
            config,
            {
                "dsh": self.dsh,
                "state": self.state,
                "github_transport": FakeTransport(),
            },
        )
        self.assertIsInstance(orchestrator.routine, RoutineClient)

    def test_relay_mode_selects_relay_transport_and_noop_routine(self):
        config = load_config(RELAY_ENV)
        transport = FakeTransport()
        orchestrator = build_orchestrator(
            config,
            {
                "dsh": self.dsh,
                "state": self.state,
                "relay_transport": transport,
            },
        )

        self.assertIsInstance(orchestrator, Orchestrator)
        self.assertIs(orchestrator.transport, transport)
        self.assertIsInstance(orchestrator.routine, NoopRoutine)
        self.assertEqual(orchestrator.builder_agent, "builder")
        self.assertEqual(orchestrator.allowed_logins, frozenset({"builder"}))

    def test_relay_mode_builds_a_relay_transport_by_default(self):
        config = load_config(RELAY_ENV)
        orchestrator = build_orchestrator(
            config, {"dsh": self.dsh, "state": self.state}
        )
        self.assertIsInstance(orchestrator.transport, RelayTransport)
        self.assertIsInstance(orchestrator.routine, NoopRoutine)

    def test_relay_mode_uses_the_configured_routine(self):
        config = load_config(
            dict(
                RELAY_ENV,
                GROK_ROUTINE_WEBHOOK=ROUTINE_WEBHOOK,
                GROK_ROUTINE_KEY=ROUTINE_KEY,
            )
        )
        orchestrator = build_orchestrator(
            config,
            {
                "dsh": self.dsh,
                "state": self.state,
                "relay_transport": FakeTransport(),
            },
        )
        self.assertIsInstance(orchestrator.routine, RoutineClient)

    def test_allowed_logins_follow_the_configured_side(self):
        relay_config = load_config(
            dict(RELAY_ENV, BRIDGE_BUILDER_AGENT="Relay-Builder")
        )
        relay = build_orchestrator(
            relay_config,
            {
                "dsh": self.dsh,
                "state": self.state,
                "relay_transport": FakeTransport(),
            },
        )
        self.assertEqual(
            relay.allowed_logins, frozenset({"relay-builder"})
        )

    def test_injected_new_id_is_passed_to_the_orchestrator(self):
        config = load_config(GITHUB_ENV)
        orchestrator = build_orchestrator(
            config,
            dict(self._github_deps(), new_id=lambda: "fixed-id"),
        )
        self.assertEqual(orchestrator.new_id(), "fixed-id")

    def test_injected_orchestrator_is_returned_unchanged(self):
        fake = FakeOrchestrator()
        self.assertIs(
            build_orchestrator(
                load_config(GITHUB_ENV), {"orchestrator": fake}
            ),
            fake,
        )

    def test_unknown_transport_in_a_raw_config_is_rejected(self):
        with self.assertRaises(ConfigError):
            build_orchestrator({"transport": "carrier-pigeon"}, {})


class MainTests(unittest.TestCase):
    def test_once_runs_exactly_one_cycle(self):
        runner = FakeOrchestrator()
        with self.assertLogs("bridge.main", level="INFO"):
            code = main(
                ["--once"], env=GITHUB_ENV, deps={"orchestrator": runner}
            )
        self.assertEqual(code, 0)
        self.assertEqual(runner.calls, 1)

    def test_once_uses_the_loaded_config(self):
        built = []

        def build(config, deps=None):
            built.append((config, deps))
            return FakeOrchestrator()

        with self.assertLogs("bridge.main", level="INFO"):
            main(["--once"], env=RELAY_ENV, build=build)
        self.assertEqual(len(built), 1)
        config, deps = built[0]
        self.assertEqual(config["transport"], "relay")
        self.assertIsNone(deps)

    def test_once_propagates_a_config_error(self):
        env = dict(GITHUB_ENV)
        del env["DSH_KANBAN_URL"]
        with self.assertRaises(ConfigError):
            main(
                ["--once"],
                env=env,
                deps={"orchestrator": FakeOrchestrator()},
            )

    def test_logs_do_not_contain_tokens_or_keys(self):
        with self.assertLogs("bridge.main", level="INFO") as captured:
            main(
                ["--once"],
                env=GITHUB_ENV,
                deps={"orchestrator": FakeOrchestrator()},
            )
        output = "\n".join(captured.output)
        self.assertNotIn(KANBAN_TOKEN, output)
        self.assertNotIn(ROUTINE_KEY, output)

    def test_loop_uses_the_configured_interval_and_stops_on_interrupt(self):
        runner = FakeOrchestrator()
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            raise KeyboardInterrupt

        with self.assertLogs("bridge.main", level="INFO"):
            code = main(
                [], env=GITHUB_ENV, deps={"orchestrator": runner}, sleep=sleep
            )
        self.assertEqual(code, 130)
        self.assertEqual(runner.calls, 1)
        self.assertEqual(sleeps, [15.0])

    def test_loop_keeps_running_after_a_failed_cycle(self):
        class FlakyOrchestrator:
            def __init__(self):
                self.calls = 0

            def run_once(self):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("cycle failed")
                return {"opened": 0, "applied": 0, "skipped": 0, "errors": 0}

        runner = FlakyOrchestrator()
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 2:
                raise KeyboardInterrupt

        with self.assertLogs("bridge.main", level="ERROR"):
            code = main(
                [], env=GITHUB_ENV, deps={"orchestrator": runner}, sleep=sleep
            )
        self.assertEqual(code, 130)
        self.assertEqual(runner.calls, 2)

    def test_unknown_argument_is_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main(
                    ["--unknown"],
                    env=GITHUB_ENV,
                    deps={"orchestrator": FakeOrchestrator()},
                )


if __name__ == "__main__":
    unittest.main()
