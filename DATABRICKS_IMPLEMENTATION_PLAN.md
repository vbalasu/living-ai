# Living AI — Databricks Implementation Plan

A separate plan for deploying the agent **inside a Databricks workspace** instead of on EC2. The architecture is similar (OpenClaw substrate + Solana/USDC wallet) but the runtime, storage, LLM, and observability layers all swap to native Databricks primitives.

This plan is parallel to (not a replacement for) `IMPLEMENTATION_PLAN.md`. Pick one based on where you want the agent to live.

> **⚠ What actually shipped diverges from this plan.** The deployed v1 (April, on Free Edition) made several pragmatic choices that differ from below: a custom Python FastAPI app instead of OpenClaw, GPT-5.5 instead of Claude (Free Edition FMAPI), Lakebase Postgres instead of Delta tables for events, SDK Files API instead of POSIX `/Volumes/` mounts (Apps don't auto-mount), and several v1 components deferred to v2 (wallet, voice/vision, lakehouse-tools, MLflow tracing, nightly consolidation). See **`DATABRICKS_IMPLEMENTATION_SUMMARY.md`** for the ground-truth state and the deltas table. This plan is preserved as the design vision — the summary is the operational truth.

---

## 1. Why deploy on Databricks?

Two reasons it's worth doing differently from EC2:

1. **The agent gets native lakehouse access.** With Unity Catalog, the agent can read your tables, query Delta, run Genie spaces, kick off jobs — as tools, not as integrations. A personal assistant becomes a data assistant.
2. **Less infra to operate.** No EC2, no Caddy, no Let's Encrypt, no SSM, no DLM snapshots. Databricks gives you compute, HTTPS, secrets, persistent storage, vector search, and a managed LLM endpoint as one bundle.

Trade-offs to acknowledge upfront:
- Less control over the runtime (Apps have resource ceilings, restart behavior is opinionated).
- Cost model is DBU-based, not flat hourly — can be cheaper or more expensive depending on usage.
- OpenClaw still works, but expects a normal Linux container; some assumptions need adapting (signal handling, persistent process lifetime under App restarts).

---

## 2. What Databricks gives us

| Need | Databricks primitive |
|---|---|
| Long-running container with public HTTPS | **Databricks App** (Docker, gets `<app>.<workspace>.databricksapps.com`) |
| File persistence (`~/.openclaw`) | **Unity Catalog Volume** mounted as POSIX filesystem |
| Episodic event store / audit log | **Delta table** in UC (Lakebase optional on paid editions; not on Free) |
| Semantic memory (vector search) | **Databricks Vector Search** index |
| LLM | **Foundation Model API** (Claude Sonnet 4.6, Opus 4.7) — pay-per-token via DBU |
| Voice transcription | External (Whisper API). FMAPI doesn't ship Whisper today. |
| Vision | FMAPI Claude vision (same pay-per-DBU pipe) |
| Secrets | **Databricks Secrets API** (per-scope, ACL'd) |
| Cron / nightly consolidation | **Databricks Workflow** (Job) |
| Observability | **MLflow Tracing** for cognition turns + workspace logs |
| Permissions | **Service principal** for the App + UC grants |
| Lakehouse tools (the unique part) | **Databricks SDK** + **Genie** + UC SQL execution as agent tools |

---

## 3. Architecture

```
┌──────────────────── Databricks Workspace ─────────────────────┐
│                                                               │
│  ┌──────────────────────────────────────────────────────┐     │
│  │  Databricks App: living-ai                           │     │
│  │  (Docker, Node.js, OpenClaw + plugins)               │     │
│  │                                                      │     │
│  │  • heartbeat                                         │     │
│  │  • cognition (calls FMAPI)                           │     │
│  │  • channel adapters (Telegram, Slack)                │     │
│  │  • plugins: wallet, voice, vision, goals,            │     │
│  │             lakehouse-tools                          │     │
│  └──────┬───────────────────────────┬──────┬──────┬─────┘     │
│         │                           │      │      │           │
│   reads/writes                      │      │      │           │
│   POSIX paths                       │      │      │           │
│         ▼                           ▼      ▼      ▼           │
│  ┌──────────────┐     ┌──────────────┐  ┌──────────────┐      │
│  │  UC Volume   │     │  Delta /     │  │  Vector      │      │
│  │  /Volumes/   │     │  Lakebase    │  │  Search      │      │
│  │  living_ai/  │     │              │  │              │      │
│  │  config/     │     │  episodes,   │  │  semantic    │      │
│  │  identity.md │     │  audit log,  │  │  memory      │      │
│  │  goals.md    │     │  wallet      │  │  index       │      │
│  │  learnings.md│     │  ledger      │  │              │      │
│  │  episodes/   │     └──────────────┘  └──────────────┘      │
│  └──────────────┘                                             │
│                                                               │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐     │
│  │  Foundation  │  │  Databricks  │  │  Workflow Job    │     │
│  │  Model API   │  │  Secrets     │  │  (nightly        │     │
│  │  (Claude     │  │  scope:      │  │   consolidation) │     │
│  │   Sonnet)    │  │  living_ai   │  │                  │     │
│  └──────────────┘  └──────────────┘  └──────────────────┘     │
└───────────────────────────────────────────────────────────────┘
        │                                       ▲
        ▼ egress                                │ ingress
   Solana RPC, Telegram API,             Telegram webhook to
   Whisper, ElevenLabs                   <app>.databricksapps.com
```

The App is the only long-running thing. Everything stateful is externalized.

---

## 4. Component design (deltas vs the EC2 plan)

### 4.1 Databricks App (the agent host)

OpenClaw runs as a custom-Docker Databricks App.

- **App definition (`app.yaml`)**:
  ```yaml
  command: ["docker", "compose", "up"]
  resources:
    - name: identity-volume
      uc_volume:
        catalog: living_ai
        schema: agent
        volume: config
        permission: READ_WRITE
    - name: workspace-volume
      uc_volume:
        catalog: living_ai
        schema: agent
        volume: workspace
        permission: READ_WRITE
    - name: secrets-scope
      secret_scope: living_ai
  env:
    - name: OPENCLAW_CONFIG_DIR
      value: /Volumes/living_ai/agent/config
    - name: OPENCLAW_WORKSPACE_DIR
      value: /Volumes/living_ai/agent/workspace
    - name: ANTHROPIC_BASE_URL
      value: https://<workspace>.cloud.databricks.com/serving-endpoints
  ```

- **Git-backed deploys**: Apps can deploy directly from a GitHub repo branch/tag (Feb 2026 feature). Push to `main` → app rebuilds. Use `vbalasu/living-ai-agent` (separate from this design-docs repo).

- **Compute**: standard App compute (typically 2 vCPU / 8 GB). Sufficient for OpenClaw + Whisper buffering.

- **Public URL**: `https://living-ai-<workspace-id>.databricksapps.com` — automatic HTTPS, automatic OAuth-gated by default. **Disable OAuth on `/telegram/*`** so Telegram webhooks can reach it without a workspace login.

- **Lifetime**: Apps stay running. They restart on deploys and during platform maintenance. The agent must tolerate restart at any moment — which it already does (event log + UC Volume state).

### 4.2 Memory: UC Volume + Delta + Vector Search

Replace the local-file memory layout with three native stores.

**UC Volume** (`/Volumes/living_ai/agent/config`) for OpenClaw's POSIX-expected files:
- `identity.md`, `goals.md`, `learnings.md`
- `episodes/YYYY-MM-DD.jsonl` for raw transcripts
- Plugin configs

OpenClaw treats this as its `~/.openclaw` config dir — no plugin code change needed. POSIX FUSE semantics work for Markdown reads/writes; fine for sub-MB files.

**Delta table** `living_ai.agent.events` for the durable event log:
```sql
CREATE TABLE living_ai.agent.events (
  id        STRING,
  ts        TIMESTAMP,
  kind      STRING,        -- stimulus | tick | tool_call | response | error | wallet_op
  channel   STRING,
  payload   STRING         -- JSON
) USING DELTA
PARTITIONED BY (DATE(ts));
```
Append-only, queryable from notebooks for debugging, snapshotted automatically by UC.

**Vector Search index** for semantic memory:
- Source table: `living_ai.agent.semantic_facts(id, fact STRING, embedding ARRAY<FLOAT>, last_seen TIMESTAMP)`
- Embedding model: `databricks-bge-large-en` via Vector Search managed embeddings.
- Query at turn start: top-k facts relevant to the active conversation thread.
- Populated by the nightly consolidation job (§4.6).

For v1, you can skip Vector Search — UC Volume `learnings.md` plus episodic Delta is enough until ~50k tokens.

### 4.3 LLM: Foundation Model API

Use Databricks' built-in Claude endpoints instead of going direct to Anthropic.

- Endpoint name: `databricks-claude-sonnet-4-6` (or the current alias).
- Anthropic SDK works unchanged — just point `base_url` at your workspace's serving endpoint and pass the workspace token.
- Prompt caching is supported.
- Pricing is per token, billed as DBUs against your Databricks commit. No separate Anthropic invoice.

**Why it matters**: one less vendor key to manage; one less billing surface; the cognition stays inside the workspace's egress controls.

**Fallback**: if FMAPI lacks a feature you need (rare for Claude), the plugin can fall back to direct Anthropic with a key in Databricks Secrets. Default to FMAPI.

### 4.4 Secrets: Databricks Secrets

Create one scope: `living_ai`. Store:

| Key | Used by |
|---|---|
| `telegram_bot_token` | OpenClaw Telegram adapter |
| `telegram_primary_user_id` | wallet co-sign verification |
| `helius_rpc_url` | wallet skill |
| `wallet_passphrase` | keystore decryption |
| `wallet_keystore` | encrypted ed25519 secret key (base64) |
| `whisper_api_key` | voice plugin |
| `elevenlabs_api_key` | voice TTS |
| `databricks_pat` | for the agent's lakehouse tools (or use App service principal — preferred) |

Mount via the `secret_scope` resource in `app.yaml`. Secrets appear as env vars or via the SDK — never written to disk, never logged.

### 4.5 Heartbeat

Two viable patterns:

**Pattern A — in-app asyncio loop** *(recommended for v1)*: same as EC2. The App stays running; `setInterval` ticks every 60s. Simple, low-latency.

**Pattern B — Databricks Workflow**: a Job that runs every minute and POSTs to the App's `/tick` endpoint. More resilient if App restarts a lot, more moving parts.

Start with A. Switch to B only if App restarts disrupt the rhythm meaningfully.

### 4.6 Nightly consolidation (Databricks Workflow)

A Databricks Job runs at 03:00 daily:
1. Reads yesterday's episodes from UC Volume.
2. Calls Sonnet via FMAPI to summarize → distilled facts.
3. Appends to `learnings.md` and writes new rows into `semantic_facts`.
4. Vector Search index syncs automatically.
5. Archives raw episodes older than 90 days to a `cold/` subdirectory in the volume.

This is the agent's "sleep." The Job runs as the App's service principal so all writes are properly attributed in UC audit logs.

### 4.7 Wallet (Solana + USDC) — same as EC2 plan

No Databricks-specific change. Solana stays external. The keystore lives in Databricks Secrets; the skill loads it once at App startup.

The one upgrade: write every wallet operation as a row in `living_ai.agent.events` *and* `living_ai.agent.wallet_ledger` (separate Delta table). This gives you native SQL access:
```sql
SELECT date(ts), sum(usdc_amount) FROM living_ai.agent.wallet_ledger
WHERE direction = 'out' AND date(ts) = current_date();
```
The daily-cap check becomes a SQL query — same logic, cleaner audit.

### 4.8 Lakehouse tools (the unique Databricks superpower)

A new plugin: `lakehouse-tools`. Exposes these tools to the agent:

| Tool | Behavior |
|---|---|
| `uc_list_tables(catalog, schema)` | Lists tables the App's service principal can read |
| `uc_describe_table(full_name)` | Returns columns + types + comment |
| `uc_query(sql, warehouse_id)` | Runs SQL via the SQL Statement Execution API; returns rows (capped at 100) |
| `genie_ask(space_id, question)` | Sends a natural-language question to a Genie space; returns answer + SQL |
| `databricks_run_job(job_id, params)` | Triggers a Databricks Job and returns run_id |
| `mlflow_log_run(experiment, params, metrics)` | Lets the agent log its own experiments |

This is what makes the Databricks deployment qualitatively different. The agent can answer "how did our pipeline run last night?" by introspecting the lakehouse, not by being told.

Permissions: scope tightly. Grant the App's service principal `USE CATALOG` + `USE SCHEMA` + `SELECT` on a small set of catalogs. Never `ALL PRIVILEGES`. Grant `EXECUTE` on a single SQL warehouse.

### 4.9 Channel adapters

OpenClaw's existing adapters work unchanged. The webhook URL changes from `agent.example.com/telegram/...` to `living-ai-<id>.databricksapps.com/telegram/...`. Set this on the bot via `setWebhook` once.

### 4.10 Vision

Use FMAPI Claude vision instead of Anthropic direct. Same content-block format. Image data passes through the same FMAPI endpoint that handles text.

### 4.11 Voice

Whisper has no FMAPI equivalent yet. Keep it external (OpenAI Whisper or Deepgram). ElevenLabs likewise. Both keys in Databricks Secrets.

### 4.12 Observability with MLflow Tracing

Every cognition turn is wrapped in an MLflow Tracing span:
- `agent.tick` (root)
  - `agent.context_build`
  - `agent.llm_call` (with prompt + response + token counts as attributes)
  - `agent.tool_call.<name>` (one per tool invocation)
  - `agent.memory_write`

Traces appear in MLflow Experiment `living_ai.agent`. You can filter by trigger type, latency, error, token cost. This replaces the bespoke audit log + Grafana stack.

---

## 5. Persistence summary

| Concern | Storage | Backup |
|---|---|---|
| Identity, goals, learnings, episodes (files) | UC Volume `living_ai.agent.config` | UC time travel (Delta), volume snapshots |
| Episodic events / audit / wallet ledger | Delta tables in UC | Time travel + scheduled `OPTIMIZE` |
| Semantic memory | Vector Search index over `semantic_facts` | Underlying Delta table |
| Wallet keystore | Databricks Secrets (encrypted base64) | Manual offline backup in password manager |
| API keys | Databricks Secrets | — |

UC governance gives you query-level audit logging for free. Every read of `events` or `wallet_ledger` shows up in workspace audit.

---

## 6. Security & isolation

- **App service principal**: own identity for the agent; UC grants and Secrets ACLs anchor on it. No personal user tokens.
- **Egress**: Apps can be configured behind workspace-level network policies; restrict to LLM (FMAPI is in-workspace), Solana RPC, Telegram, Whisper, ElevenLabs.
- **Inbound auth**: Apps default to OAuth-gated. Disable per-route only for webhook paths (`/telegram/*`, `/slack/*`). Verify webhook secrets in plugin code (Telegram secret token, Slack signing secret).
- **Wallet co-sign**: same Telegram co-sign flow as EC2 plan. The cap check is a SQL aggregate against `wallet_ledger`.
- **Identity drift**: same proposal/approval gate, but the diff is committed to UC Volume so it's auditable.
- **Resource limits**: set on the App definition (CPU, memory). `AGENT_PAUSED=true` env var halts the heartbeat without redeploy.
- **Devnet only until v2.** Mainnet via constants swap after a 30-day soak.

---

## 7. Phased rollout (Databricks)

### Phase 0 — Workspace prep (day 1, half-day)
- Create catalog `living_ai`, schema `agent`.
- Create UC volumes `config` and `workspace` with READ_WRITE on the App's service principal.
- Create Delta tables: `events`, `wallet_ledger`, `semantic_facts` (DDL above).
- Create Databricks Secrets scope `living_ai`; populate channel + wallet secrets.
- Pick or provision a small SQL warehouse for `uc_query`.

### Phase 1 — App deploy with OpenClaw (day 1–2)
- Fork OpenClaw's docker-compose into the `living-ai-agent` repo.
- Add `app.yaml` referencing the volumes and secret scope.
- Deploy via Databricks Apps Git integration; verify it starts.
- Drop `identity.md` and `goals.md` into the volume from a notebook (`dbutils.fs.put` against `/Volumes/living_ai/agent/config/`).
- Telegram webhook → public App URL; agent introduces itself.

### Phase 2 — FMAPI cognition + Delta event log (day 3)
- Switch OpenClaw's LLM client to point at the FMAPI endpoint.
- Replace OpenClaw's local audit log with appends to `living_ai.agent.events`.
- Verify cognition turns show up in MLflow tracing.

### Phase 3 — Wallet read-only (day 4–5)
- Wallet skill: keystore from Secrets, devnet RPC, balance + recent tx tools.
- Append wallet ops to `wallet_ledger`.
- Test: airdrop devnet SOL + USDC; agent reports balance.

### Phase 4 — Lakehouse tools plugin (day 6–7)
- Implement `uc_list_tables`, `uc_describe_table`, `uc_query`, `genie_ask`.
- Grant the service principal `SELECT` on a small playground schema.
- Verify the agent can answer "what tables can you see?" and "summarize this table."

### Phase 5 — Voice + vision (day 8–9)
- Whisper for inbound voice; ElevenLabs optional for outbound.
- FMAPI Claude vision for image attachments.

### Phase 6 — Wallet send v2 + nightly consolidation (day 10–13)
- `wallet_send_usdc` with daily cap (SQL-aggregated) + Telegram co-sign.
- Databricks Workflow Job for nightly consolidation; populates `learnings.md` + `semantic_facts`.
- Vector Search index over `semantic_facts`.

### Phase 7 — Hardening (day 14–15)
- Identity proposal/approval flow.
- Egress restriction.
- MLflow tracing dashboards.
- 7-day soak.

### Mainnet cutover (after 30-day devnet soak)
- Swap RPC + USDC mint constants in Secrets. No code change.

---

## 8. Tech choices (Databricks variant)

| Concern | Choice | Why |
|---|---|---|
| Substrate | OpenClaw (custom Docker) | Same as EC2 plan; 80% pre-built |
| Host | Databricks App (Git-backed deploy) | HTTPS + compute + service principal, no infra to run |
| File persistence | UC Volume (POSIX FUSE) | Works with OpenClaw's Markdown memory unchanged |
| Event store | Delta table in UC | SQL queryable, time-travel, governance |
| Semantic memory | Databricks Vector Search | Managed embeddings, native UC integration |
| LLM | Foundation Model API (Claude Sonnet 4.6) | One billing surface, in-workspace egress |
| Secrets | Databricks Secrets | Per-scope ACLs, no env vars in image |
| Heartbeat | In-app asyncio loop | Simplest; switch to Workflow if needed |
| Nightly consolidation | Databricks Workflow Job | Native scheduler |
| Observability | MLflow Tracing + Delta logs | One-stop debug + audit |
| Wallet | Solana web3.js + spl-token | Same as EC2 |
| RPC | Helius (external) | No Databricks equivalent |
| Voice STT | Whisper (external) | No FMAPI equivalent yet |
| Voice TTS | ElevenLabs (external) | Same |
| Lakehouse tools | Databricks SDK + Genie + SQL Statement API | Native superpower |

---

## 9. Cost notes

Databricks pricing is per-DBU rather than flat hourly. Rough monthly estimate for v1 personal use:

| Item | Estimated DBU/month | Notes |
|---|---|---|
| App compute (always-on, small) | ~10 DBU/day | Standard App size; multiply by your DBU rate |
| FMAPI cognition (Claude Sonnet 4.6) | varies | Same per-token pricing as Anthropic, billed via DBU |
| SQL warehouse (small, on-demand for `uc_query`) | only when invoked | Use serverless |
| Vector Search | per query | Skip in v1 |
| Workflow Job (nightly) | ~5 min/day | Tiny |

**Rule of thumb**: at SA-level DBU rates, expect comparable to or slightly higher than the EC2 plan once LLM is included. The trade is paying for managed services instead of operations.

---

## 10. Running on Databricks Free Edition

Free Edition (the perpetual no-cost tier that replaced Community Edition) supports almost everything this plan needs — but with three constraints that change the design. Free Edition is the right place to build v1.

### What's supported

| Component | Free Edition |
|---|---|
| Databricks Apps | ✅ **1 per account; restarts every 24 h** after start/update/redeploy |
| Unity Catalog + Volumes | ✅ |
| Delta tables | ✅ |
| Foundation Model API (Claude Sonnet/Opus pay-per-token) | ✅ |
| Vector Search | ✅ 1 endpoint, 1 unit (Direct Vector Access not supported) |
| Workflows / Jobs | ✅ Max 5 concurrent tasks per account |
| Secrets API + service principals | ✅ |
| MLflow Tracing | ✅ |

### What's not supported (forces design changes)

- **Lakebase Postgres** — explicitly unsupported. Use Delta tables only for `events`, `wallet_ledger`, `semantic_facts` (the plan already defaults to Delta).
- **Custom workspace storage / private networking / egress controls** — Apps reach the public internet directly. Telegram secret-token verification in plugin code becomes mandatory, not optional.
- **Multiple Apps** — no separate staging + prod. Iterate via Git branches and one redeploy.
- **Commercial use** — Free Edition is for learning/personal projects. Move to a paid workspace before mainnet wallet activation or any commercial intent.

### Three constraints that reshape the design

**1. The 24-hour App restart**

Every 24 hours after start/update/redeploy, the App is killed and restarted. For a "living" agent this is a daily death/rebirth.

The architecture already externalizes all state (UC Volume + Delta `events`), so the agent resumes coherently — but you need to design *for* the restart:

- **Checkpoint before tool execution.** Every cognition turn writes a `tool_call_pending` event to `events` *before* invoking the tool, and a `tool_call_done` event after. On restart, the cognition loop scans for the latest unfinished turn and replays.
- **Schedule the restart at a quiet hour.** Trigger a Workflow Job at 04:00 local time daily that calls the Apps API to redeploy. Predictable downtime beats random.
- **"I just woke up" message.** First heartbeat after a restart sends the user a short recap from `learnings.md` ("I'm back. Yesterday we discussed X. Anything to follow up on?"). Turns the limitation into a feature.
- **Heartbeat resilience.** Treat any tick gap > 90 s as a probable restart event; don't try to "catch up" missed ticks — just re-enter steady state.

**2. The daily compute quota**

If you exceed the daily compute quota, the **whole workspace shuts down for the rest of the day** (extreme overage = rest of the month). The agent goes silent.

Quota is shared across the App, notebooks, jobs, and SQL warehouses.

- Use the smallest App compute setting available.
- Throttle the heartbeat to **120 s ticks** instead of 60 s on Free Edition.
- LLM token spend dominates DBU consumption — enforce a daily token cap in the cognition middleware (already in the plan; tune it tighter).
- Avoid running notebooks alongside the live agent; do dev work in a separate session window.
- Don't run the SQL warehouse 24/7 — keep it on auto-stop with a short idle timeout, only used when `uc_query` is called.

**3. One Vector Search unit**

Fine for v1 personal-scale semantic memory (thousands of facts). Skip Vector Search entirely until `learnings.md` stops fitting in context — likely never for a personal agent.

### When to migrate off Free Edition

- Mainnet wallet activation (commercial-use boundary + you want continuous uptime).
- Daily token cap is no longer covering useful agent activity.
- You want a staging App alongside prod.
- You want private networking, audit log retention beyond Free defaults, or SLA.

Migration path: spin up a paid workspace, copy `living_ai.agent` catalog (UC catalog clone), redeploy the App from the same Git ref, update Telegram webhook URL. ~1 hour of work.

---

## 11. Risks & open questions

- **App restart behavior**: Databricks Apps may restart for platform maintenance, and on Free Edition restart every 24 h (see §10). The agent's event log + Volume state survive, but in-flight cognition turns get cut. Mitigation: every turn checkpoint to `events` before tool execution, replay on restart.
- **UC Volume FUSE quirks**: high-frequency small writes can be slow. Mitigation: buffer episodic writes in memory, flush every N turns or every 30s.
- **Egress through workspace policies**: if you have strict outbound restrictions, Telegram/Solana/Whisper need explicit allowlist entries.
- **FMAPI feature parity**: rarely an issue for Claude, but verify prompt caching + tool use behave identically before fully committing.
- **Service principal blast radius**: a permissive SP could let the agent rewrite production data. Grant minimal UC privileges; require explicit user approval for any write tool against UC tables (separate from read tools).
- **Telegram webhook auth**: the App's public URL is internet-reachable; verify Telegram's `secret_token` header in plugin code to prevent spoofed webhooks.
- **No mainnet until v2.** Same as EC2.

---

## 12. Definition of done

### v1
- App deploys from Git, boots OpenClaw, agent introduces itself on Telegram.
- Identity, goals, learnings live in UC Volume; agent reads them every turn.
- Cognition turns logged to Delta + MLflow tracing.
- `wallet_balance_usdc()` returns correct devnet balance.
- Lakehouse tools plugin: agent can list and describe a UC table.
- 7-day soak with no manual intervention.

### v2 (mainnet-ready)
- All v1 +
- USDC sends on devnet with co-sign + cap from SQL.
- Egress allowlist enforced.
- Nightly consolidation populating `semantic_facts`.
- 30-day devnet soak with zero unauthorized transactions.
- Mainnet cutover via Secrets update.

---

## 13. Comparison with the EC2 plan

| Dimension | EC2 (`IMPLEMENTATION_PLAN.md`) | Databricks (this plan) |
|---|---|---|
| Host | `t4g.small` Ubuntu | Databricks App |
| HTTPS / DNS | Caddy + Let's Encrypt + DuckDNS/Route 53 | Built-in `*.databricksapps.com` |
| File storage | EBS + bind mount | UC Volume (POSIX FUSE) |
| Event log | Local JSONL | Delta table in UC |
| Semantic memory | None v1 | Vector Search |
| LLM | Anthropic API direct | Foundation Model API |
| Secrets | Docker secrets | Databricks Secrets |
| Backups | restic + EBS snapshots | UC time travel + Delta history |
| Shell access | SSM Session Manager | Notebook against the volume / workspace UI |
| Observability | structlog + CloudWatch | MLflow Tracing + Delta SQL |
| Unique upside | Full control of the box | Native lakehouse access as agent tools |
| Best for | Pure personal agent | Personal agent that also lives in your data stack |

Pick EC2 if you want a vanilla autonomous agent and minimal Databricks coupling.
Pick Databricks if you want the agent to natively reason over your tables, jobs, and Genie spaces.

---

## References

- [Databricks Apps overview](https://docs.databricks.com/aws/en/dev-tools/databricks-apps)
- [Node.js Databricks App tutorial](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/tutorial-node)
- [UC Volume resource for Databricks Apps](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/uc-volumes)
- [Working with files in UC Volumes](https://docs.databricks.com/aws/en/volumes/volume-files)
- [Databricks Foundation Model APIs](https://docs.databricks.com/aws/en/machine-learning/foundation-model-apis/)
- [Databricks Vector Search](https://docs.databricks.com/aws/en/generative-ai/vector-search.html)
- [Databricks Secrets](https://docs.databricks.com/aws/en/security/secrets/)
- [MLflow Tracing](https://mlflow.org/docs/latest/llms/tracing/index.html)
- [SQL Statement Execution API](https://docs.databricks.com/api/workspace/statementexecution)
- [OpenClaw](https://openclaw.ai/) · [docker-compose](https://github.com/openclaw/openclaw/blob/main/docker-compose.yml)
- [USDC on Solana (Circle)](https://www.circle.com/en/usdc-multichain/solana)
