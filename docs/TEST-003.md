# TEST-003 · issue bus ping

- Purpose: prove Origin can read a GitHub issue and reply with one JSON comment.
- No execution. No production data.

## Request

```json
{
  "bridge": "dsh-grokbot-v1",
  "from": "掌柜",
  "to": "Origin",
  "card": "TEST-003",
  "type": "ping",
  "agent": "掌柜",
  "note": "只回 pong JSON，不执行其他动作。"
}
```

## Origin reply

```json
{
  "bridge": "dsh-grokbot-v1",
  "from": "Origin",
  "to": "掌柜",
  "card": "TEST-003",
  "type": "pong",
  "agent": "Origin",
  "needs_silas": false,
  "note": "bridge alive"
}
```

## Check

```bash
gh issue view 1 --comments
```

Expected: a single JSON comment, no prose, no action taken.

## Failure modes

- No comment: Origin cannot see the repo, or is not instructed to read issues.
- Prose reply: channel works, schema needs tightening.
- Executes something: stop; the bus is transport only.
