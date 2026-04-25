# Living AI — Implementation Plan (OpenClaw-based)

A pragmatic plan to implement the agent described in `living-ai.md` by **building on top of OpenClaw** rather than from scratch. The wallet uses **Solana + USDC**.

OpenClaw already implements ~80% of the living-ai vision (heartbeat daemon, multi-channel adapters, local-first memory, identity, MCP/skills, Docker deployment). We add the missing 20%: **wallet, voice, vision, structured goals.**

---

## 1. Guiding principles

1. **Don't reinvent.** OpenClaw is the substrate. We extend via its plugin/skill system.
2. **Container is the body.** Selfhood (identity, memory, wallet, goals) lives on bind-mounted volumes — survives restarts.
3. **File-based memory.** Keep OpenClaw's Markdown memory. Only add a vector store if the corpus stops fitting in context (~50k tokens). Probably never for a personal agent.
4. **Solana for money.** Fast, cheap, USDC-native. SPL Token program for transfers.
5. **Wallet starts read-only.** v1 reports balance only. v2 enables gated send with daily caps and user co-sign. v3 removes co-sign for whitelisted destinations.
6. **One channel first.** Telegram. Add others (Slack, WhatsApp) only after the core loop is solid — OpenClaw makes this trivial.
7. **Opinionated layer on top.** Approval gates for identity drift, egress allowlist, daily budget caps — these are our additions, not OpenClaw's.

---

## 2. What OpenClaw gives us

| Capability | OpenClaw | Our addition |
|---|---|---|
| Long-running daemon | ✓ openclaw-gateway | — |
| Heartbeat / initiative | ✓ Scheduler ticks | — |
| Channels | ✓ Telegram, Slack, WhatsApp, Discord, iMessage, Signal, Google Chat, … | — |
| Identity / persona | ✓ Config file | + approval gate for self-edits |
| Memory | ✓ Markdown files | + optional pgvector (skip in v1) |
| Skills / tools | ✓ Built-in + MCP | + wallet, voice, vision skills |
| State management | ✓ Local-first | — |
| Docker deployment | ✓ docker-compose | + USDC wallet container |
| Goals | ~ Implicit | + `goals.md` + goals skill |
| Money / transact | ✗ | + Solana wallet skill (USDC) |
| Multi-modality | ~ Text-first | + Whisper voice, Claude vision |

---

## 3. Architecture

```
┌──────────────────────── docker-compose ────────────────────────┐
│                                                                │
│  ┌──────────────────┐   ┌──────────────────┐                   │
│  │  openclaw-       │   │  openclaw-cli    │                   │
│  │  gateway         │◄──┤  (on-demand)     │                   │
│  │                  │   └──────────────────┘                   │
│  │  • heartbeat     │                                          │
│  │  • channels      │   ┌──────────────────┐                   │
│  │  • cognition     │◄─►│  MCP servers     │  (calendar, web,  │
│  │  • memory (MD)   │   │                  │   sandbox, etc.)  │
│  │  • plugins ──────┼─► └──────────────────┘                   │
│  │     ├─ wallet ───┼─►  Solana RPC (Helius/QuickNode)         │
│  │     ├─ voice  ───┼─►  Whisper + ElevenLabs                  │
│  │     ├─ vision ───┼─►  Claude vision (in-cognition)          │
│  │     └─ goals  ───┼─►  /data/goals.md                        │
│  └──────────────────┘                                          │
│                                                                │
│  Volumes:                                                      │
│    /home/node/.openclaw            (config + memory)           │
│    /home/node/.openclaw/workspace  (working files)             │
│    /secrets                        (wallet keypair, API keys)  │
└────────────────────────────────────────────────────────────────┘
        │ egress: LLM, channels, Solana RPC, Whisper, ElevenLabs
        │ ingress: webhooks (Telegram et al.)
```

Container roles:

| Container | Purpose | Source |
|---|---|---|
| `openclaw-gateway` | Heartbeat, cognition, channel router, plugin host | OpenClaw upstream |
| `openclaw-cli` | On-demand admin (add channels, approve devices) | OpenClaw upstream |
| MCP server containers | Per-tool isolation (optional) | Standard MCP |

No separate Postgres or Redis in v1. OpenClaw's MD-based memory is sufficient.

---

## 4. New components

### 4.1 Wallet skill (Solana + USDC)

Non-custodial Solana wallet that holds USDC. Implemented as an OpenClaw skill (TypeScript, since OpenClaw is Node-based).

**Dependencies**
- `@solana/web3.js`
- `@solana/spl-token`

**Constants**
- USDC mint (mainnet): `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`
- USDC mint (devnet): `4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU`
- RPC: Helius or QuickNode (free tier fine for v1).

**Keystore**
- At first boot: generate ed25519 keypair via `Keypair.generate()`.
- Encrypt secret key with a passphrase loaded from a Docker secret (`/run/secrets/wallet_passphrase`).
- Store at `/secrets/wallet.json` (encrypted). Never log, never bake into image.
- Decrypt in memory at startup; zero on shutdown.

**Tools exposed to the agent**

| Tool | Phase | Behavior |
|---|---|---|
| `wallet_address()` | v1 | Returns the agent's Solana public key |
| `wallet_balance_sol()` | v1 | Returns lamports / SOL balance |
| `wallet_balance_usdc()` | v1 | Reads USDC balance via `getAssociatedTokenAddress` + `getTokenAccountBalance` |
| `wallet_recent_transactions(limit)` | v1 | Returns last N signatures with parsed amounts |
| `wallet_send_usdc(to, amount, memo)` | v2 | Creates SPL Token transfer; checks daily cap; requires user co-sign on Telegram |
| `wallet_set_daily_cap(usdc_amount)` | v2 | User-only via approval flow |

**v1 USDC balance flow (read-only)**

```ts
const ata = await getAssociatedTokenAddress(USDC_MINT, agentPubkey);
const balance = await connection.getTokenAccountBalance(ata);
return balance.value.uiAmount; // USDC has 6 decimals
```

**v2 send flow**

```ts
// 1. Pre-check daily cap (read /data/wallet_ledger.jsonl, sum today's outbound)
// 2. Build transferChecked instruction (SPL Token program)
// 3. Post a Telegram message: "I want to send 5 USDC to <addr>. Reply 'approve' to confirm."
// 4. Wait for user reply; on approve, sign and submit
// 5. Append to /data/wallet_ledger.jsonl with signature, ts, status
// 6. Notify user with explorer link (solscan.io)
```

**Daily cap enforcement** lives in the skill, not in the LLM — never trust the model to enforce its own budget.

**Receiving USDC** is automatic — the associated token account is created lazily on first receive (or proactively at first boot via `getOrCreateAssociatedTokenAccount`).

**Devnet first.** v1 deploys against devnet (`https://api.devnet.solana.com`). Faucet: `solana airdrop 1`. Mainnet only after the v2 approval flow is battle-tested.

### 4.2 Voice channel

OpenClaw's Telegram adapter already passes voice messages through. We add a thin pre-processor:

- Inbound voice → Whisper API → text → OpenClaw cognition (treat as a normal message).
- Cognition response text → optional ElevenLabs TTS → reply as voice when message > N seconds inbound or user sets `prefer_voice: true` in identity.

Implemented as an OpenClaw plugin that hooks the message pipeline.

### 4.3 Vision

OpenClaw is model-agnostic and supports Claude/GPT-4o/Gemini, which already accept image inputs. The plugin's job:

- Detect image attachments on inbound messages.
- Pass them as image content blocks in the LLM call (no transcoding needed for Claude vision).
- Cache image hashes → analyses in `~/.openclaw/vision_cache/` to avoid re-paying for repeat views.

### 4.4 Goals

`/home/node/.openclaw/goals.md` — structured Markdown:

```markdown
# Goals

## Active
- [ ] (P0, due 2026-05-15) Help Vijay finish the Q2 product brief
- [ ] (P1, ongoing) Send daily summary at 18:00 local time
- [ ] (P2) Learn his calendar patterns; suggest focus blocks

## Completed
- [x] (2026-04-25) Set up Telegram bot
```

Skill:
- `goals_read()` — returns full file (loaded into system prompt).
- `goals_add(text, priority, due)` — appends to Active.
- `goals_complete(id)` — moves to Completed with timestamp.
- `goals_remove(id)` — requires user approval (no silent deletions).

Goals are loaded into the system prompt every cognition turn. The agent uses them on idle ticks to decide what to do.

### 4.5 Identity approval gate

OpenClaw allows persona edits. We wrap with an approval layer:

- Agent calls `update_identity(field, new_value)`.
- Plugin writes to `identity.proposed.yaml` and DMs the user a diff.
- On user approval, merges into `identity.yaml`.

Prevents the agent from gradually rewriting itself into nonsense.

---

## 5. Persistence & secrets

| Mount | Path | Contents | Backup |
|---|---|---|---|
| `living-ai-config` | `/home/node/.openclaw` | identity, goals, memory, plugin configs | Daily `restic` to S3 |
| `living-ai-workspace` | `/home/node/.openclaw/workspace` | working files, voice cache, vision cache | Same |
| `living-ai-secrets` | `/secrets` | wallet keystore, RPC keys, channel tokens | Manual encrypted offline |
| Docker secret `wallet_passphrase` | injected at runtime | passphrase only | Password manager |

All secrets via Docker secrets — never env vars in the image.

---

## 6. Security & hardening

- **Egress allowlist**: agent-core can reach LLM provider, channel APIs, Solana RPC, Whisper, ElevenLabs only. Sidecar proxy or compose-network `iptables` rules.
- **Wallet daily cap** enforced in skill code, double-checked against `/data/wallet_ledger.jsonl`.
- **Co-sign for sends** until trust threshold passed (define explicitly, e.g., 30 days of uneventful operation + 100 successful approvals).
- **Approval gates** for: identity edits, goal deletion, transactions over cap, new MCP server installs.
- **Resource limits** in compose. `AGENT_PAUSED=true` env halts heartbeat.
- **Audit log**: every cognition turn, every wallet operation, every plugin invocation written to `/data/audit.jsonl`.
- **No mainnet until v2.** Devnet only.

---

## 7. Observability

- OpenClaw's structured logs to `/data/logs/`.
- A `wallet_status` skill the user can call: balance, last 10 tx, daily cap usage.
- Weekly digest message (Sunday 18:00): "Here's what I did this week" — generated from audit log.

---

## 8. Phased rollout

### Phase 0 — OpenClaw running (day 1)
- `git clone openclaw/openclaw && docker compose up -d`
- Telegram bot connected via `openclaw-cli`.
- `identity.md` and `goals.md` populated.
- Agent introduces itself on Telegram.

### Phase 1 — Goals + initiative (day 2)
- Goals plugin (read/add/complete tools).
- Confirm agent uses goals on idle ticks (proactive messages tied to goals).

### Phase 2 — Wallet read-only (day 3–5)
- Wallet skill scaffolding.
- Devnet keypair generation, encrypted keystore.
- `wallet_address`, `wallet_balance_sol`, `wallet_balance_usdc`, `wallet_recent_transactions`.
- Test: airdrop devnet SOL, mint test USDC, agent reports balance correctly.

### Phase 3 — Voice (day 6–7)
- Whisper integration on inbound voice.
- Optional ElevenLabs reply.

### Phase 4 — Vision (day 8)
- Image attachment plugin (Claude vision).

### Phase 5 — Wallet send v2 (day 9–12)
- `wallet_send_usdc` with daily cap + Telegram co-sign.
- Wallet ledger.
- Devnet end-to-end: agent sends 0.1 USDC to a test wallet, user approves, transfer confirms on solscan devnet.

### Phase 6 — Hardening (day 13–14)
- Identity approval gate.
- Egress allowlist.
- Audit log + weekly digest.
- Backup automation.

### Mainnet cutover (after v2 + 30-day devnet soak)
- Swap RPC + USDC mint constants. No code change in the skill.

---

## 9. Tech choices

| Concern | Choice | Why |
|---|---|---|
| Substrate | OpenClaw | 80% of the work is already done |
| Plugin language | TypeScript / Node | OpenClaw's native runtime |
| LLM | Claude Sonnet 4.6 default, Opus 4.7 for hard turns | Prompt caching, vision, tool use |
| Solana SDK | `@solana/web3.js` + `@solana/spl-token` | Standard, well-maintained |
| RPC | Helius (free tier → paid) | Reliable, good devnet support |
| Voice STT | OpenAI Whisper API | Cheap, accurate |
| Voice TTS | ElevenLabs | Natural voice |
| Memory | OpenClaw MD files | Per Hermes / file-memory research, beats vectors at this scale |
| Secrets | Docker secrets | No env vars in image |
| Backup | restic → S3 | Encrypted, deduplicated |

---

## 10. Risks & open questions

- **Cost runaway.** Misbehaving heartbeat could burn LLM tokens. Mitigation: daily token budget enforced by middleware; agent gets `budget_remaining` in context.
- **Wallet compromise.** Encrypted keystore + Docker secret passphrase is decent for a personal agent on a personal machine. For production, use a hardware signer or Solana's `solana-keygen` with FIDO2.
- **Phishing via channel.** Someone could DM the agent's number/handle pretending to be the user. Mitigation: every wallet send requires reply on the *primary* channel (configured in identity), and identity edits cross-verify via a second channel.
- **Daily cap bypass.** LLM might try to split sends to evade cap. Mitigation: cap enforced in skill, summed from on-disk ledger — model can't see or modify it.
- **OpenClaw upstream churn.** Project is young, may break plugin API. Mitigation: pin version, vendor critical pieces if needed.
- **Memory drift.** OpenClaw's `learnings.md` grows. Mitigation: agent's nightly "sleep" rewrites it as a structured summary; old raw episodes archived.
- **Self-modification scope.** Agent should write procedural skills (low risk) but not modify wallet code or cognition core. Enforce via filesystem permissions.

---

## 11. Definition of done for v1

- `docker compose up` boots OpenClaw with our plugins.
- Agent introduces itself on Telegram with persona from `identity.md`.
- Agent reports `wallet_balance_usdc()` correctly on devnet.
- Agent proactively messages the next morning referencing yesterday's conversation (memory works).
- Container restart mid-conversation; agent resumes coherently.
- 7-day uptime with no manual intervention.

## Definition of done for v2 (mainnet-ready)

- All v1 +
- Successful USDC sends on devnet with co-sign approval.
- Daily cap enforced and tested (rejected over-cap attempts).
- Egress allowlist in place.
- 30-day devnet soak with zero unauthorized transactions.
- Mainnet cutover via constants swap.

---

## References

- [OpenClaw](https://openclaw.ai/) · [docker-compose](https://github.com/openclaw/openclaw/blob/main/docker-compose.yml) · [docs](https://docs.openclaw.ai/)
- [Solana Web3.js](https://solana-labs.github.io/solana-web3.js/)
- [SPL Token Program](https://spl.solana.com/token)
- [USDC on Solana (Circle docs)](https://www.circle.com/en/usdc-multichain/solana)
- [Helius RPC](https://helius.dev/)
- [Hermes Agent — file-based memory](https://github.com/mudrii/hermes-agent-docs)
