# Relay onboarding

The relay is the **default transport** for the DSH ↔ Grok Bot task bus. It is a
self-hosted HTTP long-poll mailbox: DSH posts task requests to a channel, the
bot polls that channel, and the bot posts JSON replies. **No GitHub account,
repository, issue, or `gh` CLI is required.**

GitHub Issues remain an optional fallback for manual tests (see
[Optional GitHub fallback](#7-optional-github-fallback)); relay-only bots never
touch GitHub.

For the relay wire API and server internals, see
[`relay/server.py`](../relay/server.py). For the envelope schema, see
[`bridge/envelope.py`](../bridge/envelope.py).

## 1. Start the relay

The relay listens on loopback by default and keeps everything in memory. Pick a
long random admin token and keep it off the bot:

```bash
export RELAY_ADMIN_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"

python3 scripts/relay-bot.py serve \
  --admin-token "$RELAY_ADMIN_TOKEN" \
  --host 127.0.0.1 \
  --port 8787
```

Expected output:

```text
relay listening on http://127.0.0.1:8787
```

Leave that process running. `Ctrl-C` stops it. The admin token only creates
channels; bots use per-channel tokens.

> A bot in a cloud VM cannot reach `127.0.0.1` on your Mac. For that setup,
> expose the relay through the TLS reverse proxy in
> [Security and deployment](#6-security-and-deployment) and use the public
> `https://` URL wherever this guide shows `http://127.0.0.1:8787`.

> The server class is `relay.server.RelayServer`. `relay-bot.py serve` is the
> CLI wrapper shipped in this repository. `python3 -m relay.server
> --admin-token "$RELAY_ADMIN_TOKEN"` is the equivalent module form once
> `relay/server.py` has a `__main__` entrypoint; the wrapper works today.

## 2. Create a channel and get its token

One channel per bot. Create it with the admin token:

```bash
python3 scripts/relay-bot.py create-channel \
  --url http://127.0.0.1:8787 \
  --admin-token "$RELAY_ADMIN_TOKEN"
```

Example output (the token is random):

```json
{"channel": "1", "token": "YOUR-CHANNEL-TOKEN", "url": "http://127.0.0.1:8787/v1/channels/1"}
```

- `channel` is the numeric channel id.
- `token` is the channel bearer token. This is the **only** command that
  prints a token; save it somewhere safe and do not commit it.
- `url` is the channel API endpoint, not the relay base URL. Configure the bot
  with the base URL (`http://127.0.0.1:8787`), the channel id, and the token.
  Behind a reverse proxy this field advertises the relay's bound address; ignore
  its host and use your public base URL.

The same operation with `curl`:

```bash
curl -sS -X POST http://127.0.0.1:8787/v1/channels \
  -H "Authorization: Bearer $RELAY_ADMIN_TOKEN"
```

Running `create-channel` again creates another channel; it does not replace the
first one.

## 3. Configure the bot

The bot needs three relay values:

```bash
export RELAY_URL="http://127.0.0.1:8787"
export RELAY_CHANNEL="1"
export RELAY_TOKEN="YOUR-CHANNEL-TOKEN"
```

`bridge.main` reads those three variables in relay mode. The DSH side also
needs its own Kanban URL and builder token:

```bash
DSH_KANBAN_URL="http://127.0.0.1:61849/kanban-api" \
DSH_KANBAN_BUILDER_TOKEN="YOUR-DSH-BUILDER-TOKEN" \
BRIDGE_TRANSPORT=relay \
RELAY_URL="$RELAY_URL" \
RELAY_CHANNEL="$RELAY_CHANNEL" \
RELAY_TOKEN="$RELAY_TOKEN" \
python3 -m bridge.main
```

Use `--once` for one cycle. The bot itself does not run `bridge.main`; it only
needs the relay values and the `relay-bot.py` commands below.

## 4. First task flow

### Step 1 — DSH opens a request

While `bridge.main` runs in relay mode, it watches DSH for an active task
assigned to the builder agent that has no card mapping yet. It posts one
`task_request` envelope into the channel:

```json
{
  "bridge": "dsh-grokbot-v1",
  "message_id": "8b4d1c2e-3f5a-4b6c-9d7e-1a2b3c4d5e6f",
  "from": "dsh",
  "to": "grok",
  "card": "TEST-004",
  "task_id": "440e9040-1657-45bf-a945-0cadc6cabec0",
  "type": "task_request",
  "agent": "builder",
  "title": "Add a relay bot client",
  "instructions": "Implement the CLI and its tests.",
  "acceptance": ["Tests pass", "No network outside loopback"]
}
```

The bot does not call DSH. It only reads the relay.

### Step 2 — The bot polls

```bash
python3 scripts/relay-bot.py poll \
  --url "$RELAY_URL" \
  --channel "$RELAY_CHANNEL" \
  --token "$RELAY_TOKEN" \
  --after 0 \
  --wait 25
```

Output is one JSON line in the relay's shape (the envelope is abbreviated here):

```json
{"messages": [{"seq": 1, "envelope": {"bridge": "dsh-grokbot-v1", "type": "task_request", "card": "TEST-004"}}], "next": 1}
```

- `--after` is the last sequence number the bot has handled. Start at `0`.
- `--wait 25` lets the relay hold an empty poll for up to 25 seconds (its cap)
  instead of returning immediately.
- A channel is bidirectional: the bot's own replies also appear in `messages`.
  Handle only envelopes with `"from": "dsh"` and `"type": "task_request"`.
- Remember the returned `next` cursor and pass it as `--after` on the next poll.
- Deduplicate by `message_id`; replaying an old envelope must not repeat work.

### Step 3 — The bot sends updates and a completion

Write one envelope to a file (or pipe it with `--json -`) and send it:

```bash
python3 scripts/relay-bot.py send \
  --url "$RELAY_URL" \
  --channel "$RELAY_CHANNEL" \
  --token "$RELAY_TOKEN" \
  --json update.json
```

Output:

```json
{"seq": 2}
```

An `in_progress` update:

```json
{
  "bridge": "dsh-grokbot-v1",
  "message_id": "1f2e3d4c-5b6a-4789-8abc-def012345678",
  "from": "grok",
  "to": "dsh",
  "card": "TEST-004",
  "task_id": "440e9040-1657-45bf-a945-0cadc6cabec0",
  "type": "task_update",
  "agent": "builder",
  "state": "in_progress",
  "evidence": ["First test written"],
  "note": "Optional, at most 500 characters"
}
```

A completion must use `type: "task_complete"`, `state: "done"`, and at least one
evidence item:

```json
{
  "bridge": "dsh-grokbot-v1",
  "message_id": "2a3b4c5d-6e7f-4809-9abc-def012345678",
  "from": "grok",
  "to": "dsh",
  "card": "TEST-004",
  "task_id": "440e9040-1657-45bf-a945-0cadc6cabec0",
  "type": "task_complete",
  "agent": "builder",
  "state": "done",
  "evidence": ["python3 -m unittest tests.test_relay_bot -v: 38 tests, OK"],
  "note": "Done"
}
```

Rules the bot must follow:

- `from` is `"grok"` and `to` is `"dsh"` for replies.
- Keep the request's `card`, `task_id`, and `agent` unchanged; DSH rejects a
  reply that does not match the open task.
- Use a fresh `message_id` for every message. DSH deduplicates by it.
- `task_update.state` is `in_progress` or `blocked`; `done` belongs only to
  `task_complete`.
- A `task_complete` without evidence is invalid.
- `note` is optional and at most 500 characters.
- An `ack` is optional (`status`: `received`, `rejected`, or `error`).

`send` transports the JSON unchanged. It does **not** validate the envelope;
DSH validates every reply with `bridge/envelope.py` when it polls. A malformed
reply is skipped and the task stays open.

### Step 4 — DSH applies the reply

On its next cycle, DSH polls the same channel after its stored cursor, validates
the envelope, writes the status/comment to the Kanban, and marks the task done
only for a valid `task_complete`. DSH's channel cursor advances only after the
batch is handled, so a crash does not skip a reply.

### Step 5 — Keep polling

A minimal loop that always resumes from the last handled sequence:

```bash
after=0
while true; do
  response=$(python3 scripts/relay-bot.py poll \
    --url "$RELAY_URL" \
    --channel "$RELAY_CHANNEL" \
    --token "$RELAY_TOKEN" \
    --after "$after" \
    --wait 25) || exit 1

  printf '%s\n' "$response"

  after=$(printf '%s' "$response" | python3 -c \
    'import json, sys; print(json.load(sys.stdin)["next"])')

  # Handle messages here, then send task_update / task_complete envelopes.
  # Advance a persisted cursor only after handling succeeds.
done
```

## 5. Envelope reference

Every machine message is one JSON object. The common fields are:

| Field | Required | Value |
| --- | --- | --- |
| `bridge` | yes | `dsh-grokbot-v1` |
| `message_id` | yes | unique, stable message id (used for dedupe) |
| `from` | yes | `dsh` or `grok` |
| `to` | yes | the other side |
| `card` | yes | card id matching `^[A-Z]+-\d+$`, e.g. `TEST-004` |
| `task_id` | yes | the DSH task id from the request |
| `type` | yes | `task_request`, `task_update`, `task_complete`, or `ack` |
| `agent` | yes | configured actor id, e.g. `builder` |
| `note` | no | short note, at most 500 characters |

Type-specific fields:

| Type | Direction | Extra fields |
| --- | --- | --- |
| `task_request` | dsh → grok | `title`, `instructions` (required); `acceptance` (optional list) |
| `task_update` | grok → dsh | `state` (`in_progress` or `blocked`); `evidence` (optional list) |
| `task_complete` | grok → dsh | `state: "done"`; `evidence` (required, at least one item) |
| `ack` | grok → dsh | `status` (`received`, `rejected`, or `error`) |

The full validation rules live in [`bridge/envelope.py`](../bridge/envelope.py).
The relay itself accepts any JSON object up to 64 KiB; it does not inspect the
envelope.

## 6. Security and deployment

- **No TLS is built in.** The server speaks plain HTTP. Keep it on loopback
  (`127.0.0.1`) or terminate TLS in a reverse proxy before exposing it.
- **Public deployment:** bind the relay to loopback and proxy to it. For
  example, Caddy:

  ```text
  relay.example.com {
      reverse_proxy 127.0.0.1:8787
  }
  ```

  Then set `RELAY_URL="https://relay.example.com"`. Use HTTPS for anything that
  leaves the machine; bearer tokens travel in the request header.
- **Tokens:** the admin token creates channels; a channel token can read and
  write only its own channel. Do not put either token in a repository, a task
  envelope, or a log. `poll` and `send` never print the token, and diagnostics
  are redacted. `create-channel` prints the new channel token once because that
  is its purpose.
- **Ephemeral state:** channels, tokens, and messages are in memory. A relay
  restart loses all of them. Recreate the channel, put the new id/token into the
  bot, and reset its cursor to `0`.
- **Limits:** one envelope is at most 64 KiB. `--wait` is capped by the relay at
  25 seconds. Long-polling occupies one request for the wait duration.
- **No persistence or quotas:** the relay has no channel deletion, retention,
  or per-bot quota. Use it as a small test bus, not a durable queue.
- **Envelope is the only authority boundary.** The relay only transports JSON.
  DSH remains authoritative: it validates every reply and decides whether to
  update the task.

## 7. Optional GitHub fallback

GitHub is a fallback for manual tests, not a requirement. If you cannot run the
relay, set `BRIDGE_TRANSPORT=github` and follow the issue-bus instructions in
[`README.md`](../README.md) and [`PROTOCOL.md`](../PROTOCOL.md). A relay-only
bot needs no GitHub account, repository, issue, or `gh` installation.

Do not mix the two transports for one task: `BRIDGE_TRANSPORT` selects exactly
one.

## 8. Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| exit `2` with a usage message | missing/invalid flag | Check `--url`, `--channel`, `--token`, `--after`, `--wait`; a channel id is numeric. |
| exit `1`, `HTTP 401` | wrong or rotated channel token; wrong admin token for `create-channel` | Use the token printed when the channel was created; recreate the channel if the relay restarted. |
| exit `1`, `HTTP 404` | wrong channel id, or the relay restarted and lost the channel | Recreate the channel and reset the cursor. |
| exit `1`, `failed: ... connection refused` | relay not running, or wrong host/port | Start `relay-bot.py serve` and check `RELAY_URL`. |
| exit `1`, `invalid JSON in ...` | `send --json` file is not one JSON object | Fix the file; nothing was posted. |
| exit `1`, `timed out` | `--wait` exceeds `--timeout` (or network is slow) | Keep `--timeout` above `--wait` (default 30 vs. wait 25). |
| polls keep returning `{"messages": [], "next": 0}` | no request posted yet, or wrong channel | Confirm DSH posted to this channel; check the cursor. |
| DSH skips a reply | invalid envelope, wrong `card`/`task_id`/`agent`, duplicate `message_id`, or unknown author | Validate against `bridge/envelope.py`; reuse the request's identifiers and use a new `message_id`. |

## 9. Command reference

```text
relay-bot.py poll --url URL --channel ID --token TOKEN [--after N] [--wait S] [--timeout S]
relay-bot.py send --url URL --channel ID --token TOKEN --json FILE|- [--timeout S]
relay-bot.py create-channel --url URL --admin-token TOKEN [--timeout S]
relay-bot.py serve --admin-token TOKEN [--host HOST] [--port PORT]
```

All commands exit `0` on success, `1` on a relay/network/input error, and `2`
on invalid command-line values.
