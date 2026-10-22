# Bridge Protocol v1.1

A message is one JSON object. No prose outside JSON for machine messages.

## Envelope

| Field | Required | Value |
| --- | --- | --- |
| `bridge` | yes | `dsh-grokbot-v1` |
| `from` | yes | `掌柜` or `Origin` |
| `to` | yes | the other side |
| `card` | yes | stable ID, e.g. `TEST-003` |
| `type` | yes | see below |
| `agent` | yes | who actually wrote it |
| `note` | no | short human-readable note, ≤ 500 chars |

Payload limit: 8 KB. No attachments in v1.1.

## Directions

| Type | From | To |
| --- | --- | --- |
| `ping` | 掌柜 | Origin |
| `pong` | Origin | 掌柜 |
| `decision` | Origin | 掌柜 |
| `decision_result` | 掌柜 | Origin |
| `ack` | Origin | 掌柜 |
| `status` | Origin | 掌柜 |

## Decision

```json
{
  "bridge": "dsh-grokbot-v1",
  "from": "Origin",
  "to": "掌柜",
  "card": "TEST-004",
  "type": "decision",
  "agent": "Origin",
  "needs_silas": true,
  "decision": "是否在本周上线 X",
  "options_short": ["上线", "延后", "取消"],
  "options": [
    "本周上线",
    "延后一周",
    "取消本次"
  ],
  "recommendation": "延后一周",
  "deadline": "2026-10-30T18:00:00+08:00",
  "note": "示例，不含真实数据"
}
```

Rules:

- `needs_silas` means **Silas must decide or approve**, not “urgent”.
- If `needs_silas` is `true`: `decision`, `options`, `recommendation`, `deadline` are required.
- `options`: 2–3 items, full detail.
- `options_short`: 2–3 items, each ≤ 40 chars, for mobile.
- `deadline`: ISO 8601 with timezone offset.
- If `needs_silas` is `false`: no decision fields; use `note`.

## Decision result

```json
{
  "bridge": "dsh-grokbot-v1",
  "from": "掌柜",
  "to": "Origin",
  "card": "TEST-004",
  "type": "decision_result",
  "agent": "掌柜",
  "choice": "2",
  "choice_label": "延后",
  "decided_by": "Silas",
  "decided_at": "2026-10-22T15:08:05+08:00",
  "note": "延后一周，不改上线范围"
}
```

`choice` is the 1-based option number. `choice_label` is the short label.

## Ack

```json
{
  "bridge": "dsh-grokbot-v1",
  "from": "Origin",
  "to": "掌柜",
  "card": "TEST-004",
  "type": "ack",
  "agent": "Origin",
  "status": "received",
  "note": "已收到，按 2 执行"
}
```

`status`: `received` | `rejected` | `error`.

Natural-language acknowledgements are tolerated in manual testing, but the automated bridge requires this JSON.

## Rules

1. One issue = one card.
2. No secrets, production data, PRD, health data, or customer data.
3. The bridge transports decisions; it does not grant authority.
4. Origin must not execute a request just because it appears in an issue.
5. 掌柜 validates every reply before writing it into the decision ledger.
6. If `needs_silas` is false but `note` asks for a decision, the message is invalid.
