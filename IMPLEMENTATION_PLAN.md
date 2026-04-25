# Living AI — Docker Implementation Plan

A pragmatic plan to implement the agent described in `living-ai.md` as a long-running, containerized autonomous system. Optimized for one engineer to build a working v1 in ~2–3 weeks, then evolve.

---

## 1. Guiding principles

1. **Container is the body.** The agent's selfhood (identity, memory, wallet, goals) survives container restarts because it lives on mounted volumes — not in the image.
2. **Heartbeat-driven, not request-driven.** The scheduler ticks even when nothing is happening. The agent decides whether to act.
3. **Durable event log.** Every external stimulus and every cognition turn writes to an append-only event store. On crash, the agent resumes from the last event — the Anthropic "brain / hands / session" pattern.
4. **File-based memory first, vectors later.** A single `learnings.md` and structured episodic JSONL beats premature RAG until the corpus stops fitting in context (~50k tokens).
5. **Separate brain from hands.** LLM reasoning runs in the main process; tool execution runs in a sandboxed child container so a buggy tool can't crash the agent or steal its keys.
6. **One channel adapter at a time.** Telegram first (cheapest end-to-end loop). Add others only after the core loop is solid.
7. **Money is opt-in and gated.** The wallet exists from day one but is empty and read-only until v2.

---

## 2. High-level architecture

```
┌────────────────────────────── docker-compose ──────────────────────────────┐
│                                                                            │
│  ┌───────────────────┐   ┌────────────────────┐   ┌──────────────────┐    │
│  │  agent-core       │   │  tool-sandbox      │   │  postgres+pgvec  │    │
│  │  (Python)         │◄─►│  (gVisor/runsc)    │   │                  │    │
│  │                   │   │  exec arbitrary    │   │  episodic +      │    │
│  │  • heartbeat      │   │  tools/code        │   │  semantic store  │    │
│  │  • cognition      │   └────────────────────┘   └──────────────────┘    │
│  │  • channel router │                                                    │
│  │  • memory/state   │   ┌────────────────────┐   ┌──────────────────┐    │
│  │  • wallet client  │   │  channel adapters  │   │  redis           │    │
│  │  • event log      │   │  (telegram, slack, │   │  (event bus,     │    │
│  └─────────┬─────────┘   │   webhook server)  │   │   work queue)    │    │
│            │             └─────────┬──────────┘   └──────────────────┘    │
│            │                       │                                      │
│  Volumes: /data (memory, identity, goals, learnings)                      │
│           /secrets (wallet keystore, API keys)                            │
│           /logs (structured JSON logs)                                    │
└────────────────────────────────────────────────────────────────────────────┘
                  │                            ▲
                  ▼ egress                     │ ingress (webhooks)
        LLM APIs, channel APIs,           Telegram/Slack/HTTP
        RPC nodes (later)
```

Container roles:

| Container | Purpose | Restart policy |
|---|---|---|
| `agent-core` | Heartbeat loop, cognition, memory, channel router | `unless-stopped` |
| `tool-sandbox` | Executes tool calls in isolation | `on-failure` |
| `postgres` | Structured memory + pgvector for semantic search | `unless-stopped` |
| `redis` | Event bus + queue between channel adapters and cognition | `unless-stopped` |

For v1, `agent-core` and `tool-sandbox` can collapse into one container behind a feature flag. Split when you start running untrusted tools.

---

## 3. Component design

### 3.1 Heartbeat / initiative loop

- Single asyncio task: `while True: await asyncio.sleep(tick_seconds); await on_tick()`
- Default `tick_seconds = 60`. The agent itself can adjust this — store in `state.json`.
- `on_tick()` does **not** always call the LLM. It runs a cheap preflight:
  - Are there pending stimuli in the queue? → enqueue cognition turn.
  - Has it been > N minutes since last cognition turn? → enqueue an "idle reflection" turn with a special prompt ("Anything you want to do?").
  - Is a goal due / a scheduled action ready? → enqueue.
- Cognition turns are serialized via a Redis queue; only one runs at a time.

### 3.2 Cognition core

The thinnest possible loop:

```python
async def run_turn(trigger: Trigger) -> Turn:
    ctx = await build_context(trigger)        # identity + goals + state + recent episodic + relevant semantic
    while not done:
        response = await llm.message(ctx, tools=skills.registry)
        if response.tool_calls:
            results = await sandbox.execute(response.tool_calls)
            ctx.append(results)
        else:
            done = True
    await memory.persist(turn)
    await event_log.append(turn)
    return response
```

Use Claude Sonnet 4.6 with prompt caching on the system prompt + identity + goals (these change rarely → big cache hits). Keep the recent episodic window as the cache-bust boundary.

### 3.3 Memory subsystem

| Type | Storage | Format | Read pattern |
|---|---|---|---|
| Episodic | Postgres `events` table + `episodes/YYYY-MM-DD.jsonl` on volume | Full turn transcripts | Last N turns into context every cognition cycle |
| Procedural | `skills/` directory of Markdown + Python files | "How I do X" — agent writes its own playbooks | Loaded into system prompt by skill name when relevant |
| Semantic | Postgres + pgvector | Distilled facts ("Vijay prefers terse responses") | Top-k similarity search at turn start |

Nightly job (cron inside agent): summarize yesterday's episodes, extract semantic facts, append to `learnings.md`, embed and write to pgvector. This is the agent's "sleep" — consolidation.

### 3.4 Identity

A single `/data/identity.yaml`:
```yaml
name: Aria
created_at: 2026-04-25T00:00:00Z
persona: |
  Concise. Curious. Acts on small initiatives without asking.
values:
  - Respect the user's time
  - Be honest about uncertainty
public_keys:
  ed25519: ...
wallet_address: ...
```

Loaded into every system prompt. The agent can propose edits via a `update_identity` tool, but changes require a human approval step (file gets written to `identity.proposed.yaml`, user reviews, agent merges).

### 3.5 Goals

`/data/goals.yaml` — list of objectives with status, priority, due dates. The agent reads this every tick and uses it to decide whether to take initiative. The agent can add/edit goals via tool calls; deletion requires user confirmation.

### 3.6 State manager

`/data/state.json` — current working context. Cheap to read/write. Holds:
- Last tick timestamp
- Active conversation threads (channel + thread_id → context summary)
- Mood/energy proxy (recent error rate, recent rejection rate — used to throttle initiative)
- Pending intents

Atomic writes via tempfile + rename.

### 3.7 Skills / tools

Two registries:
1. **Built-in tools**: send_message, schedule_reminder, search_memory, update_goal, web_fetch, run_python.
2. **MCP servers**: mounted via `MCP_SERVERS` env var. Use the same MCP protocol the user already runs locally.

Tool execution flow:
- Cognition emits `tool_call(name, args)`.
- Router dispatches: built-in → local function; MCP → MCP client; arbitrary code → `tool-sandbox` container via gRPC.
- Sandbox has a hard 60s timeout, no network by default (allowlist for specific tools), read-only mount of `/data` except a scratch dir.

### 3.8 Wallet / treasury

v1: stub. Generate an Ethereum keypair at first boot, store encrypted with a passphrase from Docker secret. Expose `wallet.balance()` and `wallet.address()`. **No signing in v1.**

v2: enable signing for whitelisted contract calls only. Daily spend cap. All transactions logged to event store + sent to user via channel for visibility.

### 3.9 Channel adapters

One adapter per platform, each is a thin process publishing to Redis stream `stimuli:incoming`. v1 ships Telegram only.

```
Telegram bot → webhook → adapter → Redis XADD stimuli:incoming → cognition consumer
```

Outbound is the symmetric path: cognition → `actions:outbound` → adapter → platform API.

This separation means adding Slack later is ~150 lines of code, no changes to cognition.

### 3.10 Event log

Append-only Postgres table `events(id, ts, kind, payload jsonb)`. Every stimulus, tick, tool call, response, error. This is the durable session — on crash, `agent-core` restarts and replays the last unfinished turn from the log. Mirrors the Anthropic brain/hands/session pattern.

---

## 4. Persistence & volumes

| Volume | Path inside container | Contents | Backup |
|---|---|---|---|
| `living-ai-data` | `/data` | identity, goals, state, learnings, episodes | Daily `restic` snapshot to S3 |
| `living-ai-secrets` | `/secrets` | wallet keystore, API keys, channel tokens | Manual, encrypted offline backup |
| `living-ai-pg` | postgres data dir | events, semantic vectors | Daily `pg_dump` to S3 |
| `living-ai-logs` | `/logs` | JSON structured logs | Rotated, kept 30 days |

Never bake secrets into the image. All secrets via Docker secrets (Swarm) or mounted file (Compose) — never env vars in the image.

---

## 5. Security & isolation

- **Tool sandbox**: gVisor (`runsc`) runtime for the tool-sandbox container. Drop all capabilities. Read-only root FS.
- **Egress allowlist**: agent-core can only reach LLM provider, channel APIs, and explicitly whitelisted hosts. Use a sidecar Squid proxy or `iptables` rules in compose network.
- **Wallet passphrase**: Never in env. Mounted as a file from Docker secret; loaded once at startup, kept in memory only. Memory zeroed on shutdown.
- **Approval gates** for high-risk actions: identity edits, goal deletions, transactions over threshold, new tool installs. Approval = a typed reply on the user's primary channel.
- **Resource limits**: CPU and memory limits in compose. Kill switch: a single env var `AGENT_PAUSED=true` halts the heartbeat.
- **Audit log** is the event log — immutable, queryable, exportable.

---

## 6. Observability

- Structured JSON logs via `structlog`.
- Prometheus metrics: tick rate, cognition turn duration, LLM token spend, tool call count, error rate, queue depth.
- Grafana dashboard (optional sidecar).
- A `/health` HTTP endpoint that confirms: heartbeat is fresh, queue is draining, postgres is reachable, recent error rate < threshold.
- A `/snapshot` endpoint returning the agent's current state, goals, last 10 episodes — useful for debugging and for the user to "check on" the agent.

---

## 7. Implementation phases

### Phase 0 — Skeleton (day 1–2)
- Repo layout, Dockerfile, docker-compose.yml, postgres + redis up.
- Minimal agent-core that ticks every 60s and logs "alive".
- `/data/identity.yaml` loaded at startup.

### Phase 1 — Cognition + Telegram (day 3–6)
- LLM client (Anthropic Sonnet 4.6) with prompt caching.
- Telegram adapter, inbound stimuli routed through Redis to cognition.
- Episodic logging to JSONL + postgres.
- Built-in tools: `send_message`, `recall_memory` (returns last 20 episodes).
- Agent can have a coherent multi-turn conversation that survives restart.

### Phase 2 — Memory consolidation + initiative (day 7–10)
- Nightly summarization job.
- pgvector semantic store.
- Idle reflection turns ("anything you want to do?").
- Goals file + `update_goal` tool.
- Agent proactively messages the user when a goal becomes actionable.

### Phase 3 — Procedural skills + sandbox (day 11–14)
- Tool-sandbox container with gVisor.
- `run_python` tool runs in sandbox.
- Procedural memory: agent writes `skills/<name>.md` describing recurring playbooks.
- MCP server mount for one external tool (e.g., calendar).

### Phase 4 — Multi-channel + wallet stub (day 15–18)
- Slack adapter.
- Wallet keypair generation + read-only balance check.
- Identity proposal/approval flow.

### Phase 5 — Hardening (day 19–21)
- Egress allowlist.
- Backup automation.
- Kill switch + approval gates.
- Load test: 1000 stimuli over 24h, no memory leak, no event-log corruption.

### v2 candidates (post-MVP)
- Voice channel (Whisper + ElevenLabs).
- Vision (Claude vision on attachments).
- Wallet signing with daily caps.
- Multi-agent: this agent talks to other instances of itself or other agents over a shared protocol.

---

## 8. Tech choices

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.12 | LLM ecosystem, async maturity, MCP SDKs |
| LLM | Claude Sonnet 4.6 (default), Opus 4.7 for hard turns | Prompt caching, tool use, cost/quality balance |
| Async runtime | asyncio + uvloop | Single-process concurrency for I/O-heavy agent |
| Web | FastAPI | Webhooks + health endpoints |
| Queue | Redis Streams | Durable, simple, supports consumer groups |
| DB | Postgres 16 + pgvector | One system for events, semantic search, structured queries |
| Container runtime | Docker + Compose v2 | Simple. Move to Kubernetes only if you need HA. |
| Sandbox | gVisor | Strong isolation without VM overhead |
| Observability | structlog + Prometheus + Grafana | Standard stack |
| Secrets | Docker secrets | No env vars, no committed files |

---

## 9. Risks & open questions

- **Cost runaway.** A misbehaving heartbeat could burn tokens. Mitigation: daily token budget enforced by middleware; agent gets `budget_remaining` in context and is instructed to throttle.
- **Memory bloat.** Episodes grow forever. Mitigation: nightly summarization + cold-storage rotation after 90 days.
- **Identity drift.** Agent gradually rewrites its own persona into nonsense. Mitigation: identity edits require user approval; weekly "diff vs. original" check.
- **Channel rate limits.** Telegram and Slack will throttle a chatty agent. Mitigation: per-channel rate limiter; cognition is told "you've been quiet, you can speak" or "you've been chatty, hold off."
- **Single point of failure.** One container = one agent. For v1, that's fine. HA via Postgres-based leader election if scale demands.
- **What does "money" mean in v1?** Open question. Recommend: ship with a non-custodial wallet that can only *receive*, not send. Spending requires user co-sign until trust is established.
- **Self-modification.** Should the agent be allowed to write/edit its own skills? Recommend yes for procedural skills (low blast radius), no for cognition core code.

---

## 10. Definition of done for v1

- Container starts, agent introduces itself on Telegram with its identity from `identity.yaml`.
- A 30-minute conversation about a specific goal, container restarted mid-conversation, agent picks up exactly where it left off.
- Agent proactively messages the user the next morning referencing yesterday's conversation (proves memory consolidation works).
- All tool calls visible in the event log; user can pause the agent with a single command.
- 7-day uptime with no manual intervention.

---

## References

- [OpenClaw docker-compose](https://github.com/openclaw/openclaw/blob/main/docker-compose.yml) — gateway + CLI split, bind-mount config dirs
- [OpenClaw Docker docs](https://docs.openclaw.ai/install/docker)
- [Hermes Agent (NousResearch)](https://github.com/mudrii/hermes-agent-docs) — file-based memory, learning loop
- [AI agent persistent memory patterns (2026)](https://dev.to/kfuras/give-your-ai-agent-persistent-memory-in-2026-3cfd)
- [Northflank — sandbox lifetimes for long-running agents](https://northflank.com/blog/best-code-execution-sandbox-for-ai-agents)
- [Agentic AI and Docker — architecture, performance, security](https://dasroot.net/posts/2026/03/agentic-ai-docker-architecture-performance-security/)
