# Databricks Implementation Summary — what's actually deployed

This is the ground-truth picture of the **April** agent as it runs today on Databricks Free Edition. The original `DATABRICKS_IMPLEMENTATION_PLAN.md` describes the full v1+v2 vision; this document describes the actual v1 that shipped, the deltas from the plan, and how to test, debug, and extend it.

> **Workspace**: `dbc-d5a49298-395d.cloud.databricks.com` (Free Edition)
> **Agent**: April — Telegram-facing AI agent with persistent memory
> **App URL**: `https://living-ai-657435001811035.aws.databricksapps.com`

---

## 1. What was actually built

A minimal Python FastAPI agent (Path B from the plan) deployed as a Databricks App with:

- **Heartbeat loop** — 120s asyncio tick (raised from 60s for Free Edition quota safety)
- **Cognition** — Foundation Model API → `databricks-gpt-5-5` (Free Edition has no Claude on FMAPI; we use GPT-5.5 instead)
- **Memory**
  - UC Volume Markdown files (identity, goals, learnings) accessed via the **SDK Files API** — Databricks Apps don't auto-mount `/Volumes/...` as POSIX, contrary to the plan's assumption
  - **Lakebase Postgres** for events, wallet ledger, semantic facts (replacing the planned Delta tables — Lakebase is now supported on Free Edition)
- **Telegram channel** — webhook into FastAPI; verified primary user by handle (`@vbalasu`); secret-token header check
- **Databricks Asset Bundle** — declarative deploy of schema, volumes, App, setup job
- **`.pex` deployer** — single-file binary that prompts for env config and provisions the full stack into any workspace

Not yet built (plan called for these as v1; pushed to v2 backlog):

- Solana/USDC wallet skill
- Voice (Whisper/ElevenLabs) and vision plugins
- Goals tooling (`goals_add`, `goals_complete`)
- Identity proposal/approval flow
- Lakehouse tools plugin (`uc_query`, `genie_ask`, etc.)
- Nightly consolidation job → `semantic_facts` + Vector Search
- MLflow tracing on cognition turns

---

## 2. Architecture (actual)

```
┌─────────────── Databricks Workspace (Free Edition) ───────────────┐
│                                                                   │
│   ┌────────────────────── Databricks App: living-ai ──────────┐   │
│   │  FastAPI / uvicorn (Python 3.11, ~10 MB image)            │   │
│   │  • async heartbeat (120s ticks, idle reflection)          │   │
│   │  • /telegram/webhook (Telegram bot adapter)               │   │
│   │  • /health, /, /snapshot                                  │   │
│   │  • cognition → FMAPI client                               │   │
│   │  • memory → Files API + Lakebase                          │   │
│   │  • lakebase → psycopg with rotating OAuth credential      │   │
│   └─────┬──────────────────┬────────────────┬──────────────┬──┘   │
│         │                  │                │              │      │
│         ▼                  ▼                ▼              ▼      │
│   ┌──────────┐     ┌──────────────┐   ┌──────────┐   ┌────────┐   │
│   │ FMAPI    │     │ UC Volume    │   │ Lakebase │   │Secrets │   │
│   │ GPT-5.5  │     │ workspace.   │   │ april-db │   │living_ │   │
│   │          │     │ living_ai.   │   │ (Postgres│   │ai      │   │
│   │          │     │ config       │   │  CU_1)   │   │        │   │
│   │          │     │ (md files)   │   │          │   │        │   │
│   └──────────┘     └──────────────┘   └──────────┘   └────────┘   │
│                                                                   │
└───────────────────────────────────────────────────────────────────┘
                                ▲
                                │ Telegram webhook (HTTPS)
                                │
                          @ your_bot
```

---

## 3. Resources created in the workspace

| Type | Name | Notes |
|---|---|---|
| Catalog | `workspace` | Pre-existing managed catalog |
| Schema | `workspace.living_ai` | Created by bundle |
| UC Volume | `workspace.living_ai.config` | identity.md, goals.md, learnings.md, episodes/ |
| UC Volume | `workspace.living_ai.workspace_dir` | Working files (currently unused; reserved for voice/vision cache) |
| Lakebase instance | `april-db` | Capacity `CU_1`, Postgres 16, autoscaling enabled |
| Lakebase tables | `events`, `wallet_ledger`, `semantic_facts` | Created by setup job; in `databricks_postgres.public` |
| Postgres role | App SP `312ecaa9-…` | Created via `databricks_create_role()`, granted SELECT/INSERT/UPDATE on public schema |
| Secrets scope | `living_ai` | 3 keys: `telegram_bot_token`, `telegram_primary_user_handle`, `telegram_webhook_secret` |
| App | `living-ai` | Standard compute size, daily 24h restart on Free Edition |
| Job | `living-ai-setup-tables` | One-time idempotent DDL + role grant |
| Service principal | `app-14anva living-ai` | Auto-created by App; granted UC + FMAPI + Secrets + Lakebase access |

---

## 4. Repo / bundle layout

```
living-ai/                              # this repo
├── agent/                              # Databricks Asset Bundle
│   ├── databricks.yml                  # bundle root, vars, targets (free, prod)
│   ├── resources/
│   │   ├── schema.yml                  # workspace.living_ai
│   │   ├── volumes.yml                 # config + workspace_dir
│   │   ├── setup_job.yml               # one-time DDL job
│   │   └── app.yml                     # App with bound resources
│   ├── src/                            # FastAPI agent source
│   │   ├── app.py                      # FastAPI entrypoint, lifespan
│   │   ├── app.yaml                    # App runtime command + env vars
│   │   ├── config.py                   # env-var-driven runtime config
│   │   ├── memory.py                   # Files API + Lakebase memory
│   │   ├── lakebase.py                 # psycopg + SDK credential pooling
│   │   ├── cognition.py                # FMAPI client + system prompt builder
│   │   ├── heartbeat.py                # async tick loop + idle reflection
│   │   ├── telegram.py                 # webhook handler + outbound send
│   │   └── requirements.txt
│   ├── sql/
│   │   └── setup_notebook.py           # Lakebase DDL + SP role provisioning
│   ├── deploy/                         # .pex deployer source
│   │   ├── pyproject.toml
│   │   ├── build.sh                    # rebuild .pex from current bundle
│   │   └── living_ai_deploy/
│   │       ├── deployer.py             # main deployer logic
│   │       └── prompts.py              # interactive prompts
│   └── README.md                       # bundle usage
└── living-ai-deploy.pex                # 10 MB single-file deployer
```

---

## 5. How to test

### 5.1 Health check (no LLM cost)

The App exposes three GET endpoints. Behind workspace OAuth — easiest to hit them from a browser logged into Databricks:

| Endpoint | What it returns |
|---|---|
| `/`        | Agent name, FMAPI endpoint, heartbeat seconds, telegram status |
| `/health`  | `{"status": "alive"}` if uvicorn is up |
| `/snapshot`| identity + goals + last 10 events (debug view) |

For programmatic checks, use the OAuth-authenticated CLI:

```bash
databricks auth login --host https://dbc-d5a49298-395d.cloud.databricks.com --profile free-oauth
databricks --profile free-oauth apps logs living-ai | tail -40
```

### 5.2 Telegram round-trip (the real test)

1. DM your bot from the Telegram account whose handle is in `telegram_primary_user_handle`.
2. April should reply within ~3–8 seconds (FMAPI cold latency on Free is the bottleneck).
3. The response should be in-persona (concise, curious — see `identity.md`).

Verify the turn was recorded:

```sql
-- Run from a Databricks notebook against the SQL warehouse OR connect to Lakebase
SELECT ts, kind, channel, payload FROM events ORDER BY ts DESC LIMIT 10;
```

Expected: a `stimulus` row (your message), a `response` row (April's reply), interleaved `tick` rows from the heartbeat.

### 5.3 Persistence across restarts

Free Edition restarts the App every 24 h. To validate state survives:

```bash
databricks --profile free apps stop  living-ai
databricks --profile free apps start living-ai
# Wait ~60s for compute to boot
```

Then DM April again. She should reference yesterday's conversation in context (loaded from Lakebase + UC Volume).

### 5.4 The .pex deployer

To verify the deployer works against a fresh workspace, run it on any laptop with `databricks` CLI on PATH:

```bash
./living-ai-deploy.pex
```

It prompts for host, PAT, agent name, Telegram token, etc., then provisions the full stack. ~3 minutes end-to-end.

---

## 6. How to debug

### 6.1 Tail the App logs

```bash
databricks --profile free-oauth apps logs living-ai | tail -50
```

OAuth profile required — PAT auth doesn't work for the apps logs endpoint.

Look for:
- `[BUILD]` lines on first deploy / after a `requirements.txt` change
- `[APP]` lines from your code (uvicorn, structured logs)
- `[SYSTEM]` lines from the platform

### 6.2 Common failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `app crashed unexpectedly` | Code error during `lifespan` startup | `apps logs` → look for traceback |
| `PermissionError: '/Volumes'` | Tried to access UC Volume as POSIX | Apps don't auto-mount; use SDK Files API |
| `'WorkspaceClient' has no attribute 'database'` | databricks-sdk too old | Pin `databricks-sdk>=0.50` (we use 0.105) |
| `Requirements have not changed. Skipping installation.` | App caches dependency hashes | Run `databricks bundle deploy` (full upload) THEN `bundle run living_ai_app` |
| `Workspace … reached the maximum limit of 1 apps` | Free Edition cap | Delete other apps: `databricks apps delete <name>` |
| `Organization has been cancelled or is not active yet` | Workspace not fully activated | Open workspace UI; complete any pending T&C / activation |
| Telegram webhook silent | Wrong URL or missing secret token | `curl https://api.telegram.org/bot<TOKEN>/getWebhookInfo` |
| `Role <uuid> not found in instance april-db` | App SP not provisioned in Lakebase | Run `bundle run setup_tables` — it calls `databricks_create_role()` |

### 6.3 Inspecting state

**Identity / goals / learnings (UC Volume files)** — read or edit from a notebook:

```python
display(spark.sql("LIST '/Volumes/workspace/living_ai/config/'"))
print(open('/Volumes/workspace/living_ai/config/identity.md').read())
```

(Notebooks DO have `/Volumes` POSIX access; only Apps don't.)

**Events / wallet ledger / facts (Lakebase)** — connect from a notebook:

```python
import psycopg, uuid
from databricks.sdk import WorkspaceClient
w = WorkspaceClient()
inst = w.database.get_database_instance("april-db")
cred = w.database.generate_database_credential(
    request_id=str(uuid.uuid4()), instance_names=["april-db"]
)
conn = psycopg.connect(
    host=inst.read_write_dns, dbname="databricks_postgres",
    user=spark.sql("select current_user()").collect()[0][0],
    password=cred.token, sslmode="require"
)
with conn.cursor() as cur:
    cur.execute("SELECT ts, kind, channel, payload FROM events ORDER BY ts DESC LIMIT 20")
    for row in cur.fetchall():
        print(row)
```

**Webhook activity** — the App logs every inbound webhook as `INFO uvicorn.access`; filter:

```bash
databricks --profile free-oauth apps logs living-ai | grep '/telegram/webhook'
```

### 6.4 Resetting

To wipe April's memory and start fresh:

```python
# In a notebook
spark.sql("REMOVE '/Volumes/workspace/living_ai/config/identity.md'")
spark.sql("REMOVE '/Volumes/workspace/living_ai/config/goals.md'")
spark.sql("REMOVE '/Volumes/workspace/living_ai/config/learnings.md'")
# And in Lakebase:
# TRUNCATE events, wallet_ledger, semantic_facts;
```

The App will re-seed default identity/goals on next boot.

---

## 7. Deltas from the plan

| Plan section | Plan said | Reality |
|---|---|---|
| 4.1 — App host | OpenClaw daemon | Custom Python FastAPI app (Path B, simpler for Free Edition) |
| 4.2 — UC Volume access | POSIX `/Volumes/...` (assumed FUSE mount) | SDK Files API (`w.files.upload/download`) — Apps don't auto-mount |
| 4.2 — Event store | Delta table | **Lakebase Postgres** (newly supported on Free Edition; richer queries; SQL-native daily-cap aggregation) |
| 4.3 — LLM | Claude Sonnet 4.6 / Opus 4.7 via FMAPI | **GPT-5.5** — Free Edition FMAPI doesn't expose Claude |
| 4.5 — Heartbeat | 60s ticks | 120s ticks (Free Edition compute quota safety) |
| 4.6 — Nightly consolidation | Implemented in v1 | Deferred to v2 |
| 4.7 — Wallet (Solana/USDC) | v1 read-only | Deferred to v2 |
| 4.8 — Lakehouse tools | v1 plugin | Deferred to v2 |
| 4.10–4.11 — Voice / vision | v1 | Deferred to v2 |
| 4.12 — MLflow Tracing | v1 | Deferred — for now we log to Lakebase + structured stdout |

The plan has been updated with a "what actually shipped" callout at the top so future readers don't get whiplash.

---

## 8. v2 backlog (in suggested priority order)

1. **Lakehouse tools plugin** — `uc_query`, `genie_ask`, `databricks_run_job`. Highest unique upside vs. EC2 deployment; zero infra additions.
2. **Goals tooling** — `goals_add`, `goals_complete` slash commands the user can invoke from Telegram. Currently goals can only be edited via UC notebook.
3. **Solana/USDC wallet** — devnet keypair, balance read, then send with co-sign. Per the plan.
4. **Nightly consolidation** — Workflow Job that summarizes yesterday's events into `learnings.md` + `semantic_facts`. Vector Search wires up here.
5. **MLflow Tracing** — wrap each cognition turn in a span; replaces the bespoke event log for debugging.
6. **Voice in/out** — Whisper for inbound voice notes, ElevenLabs for outbound.
7. **Vision** — image attachments in Telegram → Claude vision (or whatever multimodal endpoint is available on FMAPI).
8. **Identity proposal/approval flow** — agent proposes self-edits, user approves via Telegram.
9. **Egress allowlist** — workspace network policy locking April to FMAPI + Telegram + Solana RPC + Whisper + ElevenLabs.

---

## 9. Pointers

- Bundle README: `agent/README.md` (deploy commands, target/var docs)
- Deployer README: `agent/deploy/` (pex build instructions inline)
- Plan: `DATABRICKS_IMPLEMENTATION_PLAN.md` — full v1+v2 vision
- User guide: `USER_GUIDE.md` — how to actually use April
