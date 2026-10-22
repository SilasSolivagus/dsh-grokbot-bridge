# TEST-004 — real Grok routine over relay

**Date:** 2026-10-23  
**Result:** PASS

## What was tested

One real Grok routine (`relay-worker`) was triggered by the bridge webhook and used the relay to:

1. receive a `task_request`
2. post `task_update` (`in_progress`)
3. post `task_complete` (`done`, evidence `relay smoke`)

No GitHub issue was used. No local port was exposed.

## Setup

- DSH Kanban live at `127.0.0.1:61849`
- self-hosted relay on loopback
- bridge `feat/m1.5-task-bus`
- routine webhook configured in Grok Bot
- builder actor: `builder`
- executor: Grok routine `relay-worker`

## Observed

```text
DSH-6 created, assignee builder
open cycle: opened=1
poll cycle: applied=1 -> in_progress
poll cycle: applied=1 -> done
activity: office, builder, builder
comments: builder: relay smoke
          builder: Evidence: relay smoke
```

The DSH card was archived after the test.

## What this proves

- The bridge can wake a real Grok routine over HTTPS.
- The routine can read the relay with the channel token from the wake payload.
- The routine can write valid envelopes back to the relay.
- The bridge can validate and apply them to DSH with the builder token.
- The default transport is relay; GitHub is not required.

## What this does NOT prove

- It does not prove the **扳子 worker** did real work. The routine was the protocol/herald routine doing a relay smoke.
- It does not prove long-running work, artifacts, or handoff.
- Relay is in-memory; restart loses channels.
- The routine key is local-only and never committed.
