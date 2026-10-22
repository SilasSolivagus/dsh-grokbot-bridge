# DSH ↔ Grok Bot Bridge

Private test bus between **掌柜** (DSH, local) and **Origin** (Grok Bot, cloud).

This repo is not the Kanban and not a bot. It is a shared mailbox for testing one thing:

> Can 掌柜 hand a decision to Origin, and can Origin hand a receipt back, without exposing the local DSH API?

## Why an issue bus

- Grok Bot runs in a cloud VM.
- DSH runs on Silas's Mac.
- A cloud bot cannot use `127.0.0.1` to reach the Mac.
- GitHub Issues are reachable from both sides and give an audit trail.

No tunnel. No public API. No local port exposure.

## Roles

| Name | Side | Job |
| --- | --- | --- |
| 掌柜 | DSH / local | writes requests, validates replies, records decisions |
| Origin | Grok Bot / cloud | reads requests, replies with JSON, routes decisions |
| Issue | shared bus | one JSON request + one JSON comment per test |
| Silas | chairman | makes red decisions; not automated |

## Flow

```text
掌柜
  │ gh issue create (JSON request)
  ▼
GitHub private issue
  ▲
  │ comment (JSON reply)
Origin
  │
  ▼
掌柜 validates → records → Silas decides → receipt to Origin
```

## Message types

| Type | Direction | Purpose |
| --- | --- | --- |
| `ping` | 掌柜 → Origin | liveness check |
| `pong` | Origin → 掌柜 | liveness reply |
| `decision` | Origin → 掌柜 | a real decision request |
| `decision_result` | 掌柜 → Origin | Silas's explicit choice |
| `ack` | Origin → 掌柜 | receipt |
| `status` | Origin → 掌柜 | non-blocking status |

Protocol details: [PROTOCOL.md](PROTOCOL.md)

## First transport test: TEST-003

Issue [#1](https://github.com/SilasSolivagus/dsh-grokbot-bridge/issues/1) is the first issue-bus test.

1. 掌柜 creates issue #1 with `examples/test-003.ping.json`.
2. Origin reads the issue body and replies with one JSON comment.
3. 掌柜 runs:

```bash
gh issue view 1 --comments
```

4. 掌柜 validates the comment against `examples/test-003.pong.json`.

If this works, the manual “邮差” step is no longer needed for liveness.

## Validate

```bash
python3 scripts/validate.py examples/*.json
```

No dependencies. The validator checks message direction, required fields, decision shape, and deadline format.

## Boundaries

- This is a **transport test**, not an authority grant.
- Origin must not execute a request just because it appears in an issue.
- No production data, credentials, PRD, health data, or customer data in this repo.
- One issue = one test card. Close it when done.
- A receipt is not an approval. Only Silas approves red decisions.

## Roadmap

1. Manual relay: TEST-001 / TEST-002 (done)
2. Issue bus: TEST-003 (this repo)
3. Scheduled Origin routine: poll issue and reply
4. Only then consider a relay/queue service

The bridge is successful when it is boring: create issue, get JSON, validate, close.
