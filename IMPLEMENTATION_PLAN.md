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

## 8. Cloud deployment — EC2

The agent runs 24/7, so it lives on a cloud VM, not a laptop. Smallest viable AWS box that runs OpenClaw + plugins comfortably.

### 8.1 Instance choice

| Option | Specs | $/mo | Notes |
|---|---|---|---|
| **`t4g.small`** *(recommended)* | 2 vCPU ARM Graviton, 2 GB RAM | ~$12 | Cheapest sweet spot. Node.js + Docker + Claude API calls fit in 2 GB. ARM64 image works for OpenClaw. |
| `t3.small` | 2 vCPU x86, 2 GB RAM | ~$15 | Fallback if any OpenClaw plugin ships only x86 native binaries. |
| `t4g.micro` | 2 vCPU ARM, 1 GB RAM | ~$6 | Risky — Docker pull + Whisper buffering can OOM. Skip. |

- **OS**: Ubuntu 24.04 LTS (Canonical AMI for arm64).
- **Root volume**: 20 GB gp3 (~$1.60/mo). Plenty for OS + Docker images + memory files for years.
- **Region**: closest to your channel APIs and yourself; `us-west-2` or `us-east-1` typical.

**Total v1 infra cost: ~$15/mo** (instance + storage + minimal egress). LLM and Solana RPC costs are separate.

### 8.2 Networking — making the agent reachable

The agent needs:
- **Inbound HTTPS** for channel webhooks (Telegram, Slack, Discord) and OpenClaw admin endpoints.
- **Egress** to LLM, Solana RPC, channel APIs, Whisper, ElevenLabs.
- **SSH access** for the operator — but not from the entire Internet.

**Public address**:
- Allocate one **Elastic IP** (free while attached) and bind to the instance.
- DNS: a single A record `agent.example.com → <EIP>`. Use Route 53 ($0.50/mo) or any provider; for a free option, **DuckDNS** works for v1.

**Reverse proxy + TLS**:
- **Caddy** as the front door. Auto-provisions Let's Encrypt certs on first request. One config block, no certbot dance.
- Caddy listens on 80 (redirect → 443) and 443. Routes:
  - `/telegram/*` → OpenClaw webhook port
  - `/slack/*` → OpenClaw webhook port
  - `/health` → OpenClaw health endpoint
  - everything else → 404

**Security group**:

| Port | Source | Purpose |
|---|---|---|
| 22 | *Operator IP only* — or **closed** if using SSM Session Manager | SSH |
| 80 | `0.0.0.0/0` | HTTP → HTTPS redirect for Let's Encrypt + Caddy |
| 443 | `0.0.0.0/0` | Channel webhooks |

All other inbound denied. All outbound allowed (or restrict per the egress allowlist in §6).

**Recommended**: skip port 22 entirely. Use **AWS SSM Session Manager** for shell access — no public SSH surface, IAM-controlled, audit-logged. Attach role `AmazonSSMManagedInstanceCore` to the instance and install `amazon-ssm-agent` (preinstalled on Ubuntu AWS AMIs).

### 8.3 Bootstrap (cloud-init `user-data`)

Pasted into the Launch Instance wizard's *User data* field. Runs once on first boot.

```bash
#!/bin/bash
set -euo pipefail

# 1. Base packages
apt-get update -y
apt-get install -y ca-certificates curl gnupg ufw fail2ban unattended-upgrades

# 2. Docker (official repo, arm64-aware)
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update -y
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
usermod -aG docker ubuntu

# 3. Caddy
apt-get install -y debian-keyring debian-archive-keyring apt-transport-https
curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
apt-get update -y
apt-get install -y caddy

# 4. Firewall (defense in depth alongside the SG)
ufw default deny incoming
ufw default allow outgoing
ufw allow 80/tcp
ufw allow 443/tcp
# ufw allow 22/tcp from <YOUR_IP>   # uncomment only if not using SSM
ufw --force enable

# 5. Auto security upgrades
dpkg-reconfigure -f noninteractive unattended-upgrades

# 6. Clone and configure
sudo -u ubuntu bash <<'EOSU'
cd ~
git clone https://github.com/openclaw/openclaw.git
cd openclaw
# Place living-ai plugins next to OpenClaw's plugin dir
mkdir -p ~/.openclaw
EOSU

# 7. Caddyfile (replace agent.example.com with your domain)
cat > /etc/caddy/Caddyfile <<'EOF'
agent.example.com {
    encode gzip
    reverse_proxy /telegram/* localhost:3000
    reverse_proxy /slack/*    localhost:3000
    reverse_proxy /health     localhost:3000
}
EOF
systemctl reload caddy

# 8. systemd unit to keep OpenClaw running
cat > /etc/systemd/system/openclaw.service <<'EOF'
[Unit]
Description=OpenClaw living-ai agent
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/home/ubuntu/openclaw
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down
User=ubuntu

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable openclaw.service
```

After boot, finish manually (one-time):
1. `aws ssm start-session --target <instance-id>` (or SSH if enabled).
2. Drop secrets into `/home/ubuntu/openclaw/secrets/` (channel tokens, RPC key, wallet passphrase).
3. Drop `identity.md` and `goals.md` into `~/.openclaw/`.
4. `systemctl start openclaw`.
5. Configure Telegram webhook: `curl -F "url=https://agent.example.com/telegram/<bot-id>" https://api.telegram.org/bot<TOKEN>/setWebhook`.

### 8.4 IAM role for the instance

Minimal `InstanceProfile` policies:
- `AmazonSSMManagedInstanceCore` — Session Manager access.
- A custom inline policy granting `s3:PutObject`/`GetObject` on `s3://living-ai-backups/<instance-id>/*` for backups (next section).

No AWS keys baked into the image.

### 8.5 Backups

- **Daily EBS snapshot** via AWS Data Lifecycle Manager (DLM) — 7 daily, 4 weekly, 3 monthly. Free policy; storage costs ~$0.05/GB/mo for changed blocks.
- **Application-level**: `restic` cron job on the instance writes encrypted snapshots of `~/.openclaw` and `/secrets` to S3 nightly. Restic password in SSM Parameter Store.
- **Wallet keystore**: also kept in an offline encrypted backup (your password manager). Don't rely solely on cloud snapshots.

### 8.6 Monitoring & operations

- **CloudWatch Agent** ships `/var/log/syslog` and `~/openclaw/logs/*.jsonl` to CloudWatch Logs. Free tier covers v1.
- **CloudWatch alarm**: alert if instance status check fails or if a custom heartbeat metric (pushed by OpenClaw every tick) goes stale > 5 min.
- **Uptime**: `systemd` keeps OpenClaw running; instance autostart on boot. EC2 itself stays up unless AWS schedules retirement (rare; gets a 2-week warning email).

### 8.7 Upgrade path

| Trigger | Move to |
|---|---|
| Sustained CPU > 70% or memory pressure | `t4g.medium` (4 GB), no other changes |
| Need HA / zero-downtime updates | Two instances behind ALB, shared EFS for `/data`, leader election |
| Plugin requires GPU (local Whisper, local model) | `g5g.xlarge` (ARM + T4g GPU) — but stay on cloud APIs in v1 |

### 8.8 Cost summary (v1, monthly)

| Item | Cost |
|---|---|
| `t4g.small` on-demand | ~$12 |
| 20 GB gp3 root | ~$1.60 |
| Elastic IP (attached) | $0 |
| Egress data (light) | ~$1 |
| EBS snapshots (delta) | ~$1 |
| Route 53 hosted zone (optional) | $0.50 |
| **Infra subtotal** | **~$15–17/mo** |
| LLM API (Claude, est.) | $20–100 |
| Solana RPC (Helius free tier) | $0 |
| Whisper / ElevenLabs (light usage) | $0–5 |

---

## 9. Phased rollout

### Phase 0 — EC2 + OpenClaw running (day 1)
- Launch `t4g.small` Ubuntu 24.04 with the cloud-init script above.
- Allocate Elastic IP, point DNS at it, verify Caddy serves HTTPS.
- `docker compose up -d` (auto via systemd).
- Telegram bot connected via `openclaw-cli`; webhook pointed at `https://agent.example.com/telegram/...`.
- `identity.md` and `goals.md` populated.
- Agent introduces itself on Telegram from the cloud.

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

## 10. Tech choices

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
| Backup | restic → S3 + EBS snapshots via DLM | Encrypted, deduplicated, off-instance |
| Host | AWS EC2 `t4g.small` Ubuntu 24.04 | Smallest viable; ARM = cheapest |
| Reverse proxy | Caddy | Auto Let's Encrypt, single config block |
| Shell access | AWS SSM Session Manager | No public SSH surface |

---

## 11. Risks & open questions

- **Cost runaway.** Misbehaving heartbeat could burn LLM tokens. Mitigation: daily token budget enforced by middleware; agent gets `budget_remaining` in context.
- **Wallet compromise.** Encrypted keystore + Docker secret passphrase is decent for a personal agent on a personal machine. For production, use a hardware signer or Solana's `solana-keygen` with FIDO2.
- **Phishing via channel.** Someone could DM the agent's number/handle pretending to be the user. Mitigation: every wallet send requires reply on the *primary* channel (configured in identity), and identity edits cross-verify via a second channel.
- **Daily cap bypass.** LLM might try to split sends to evade cap. Mitigation: cap enforced in skill, summed from on-disk ledger — model can't see or modify it.
- **OpenClaw upstream churn.** Project is young, may break plugin API. Mitigation: pin version, vendor critical pieces if needed.
- **Memory drift.** OpenClaw's `learnings.md` grows. Mitigation: agent's nightly "sleep" rewrites it as a structured summary; old raw episodes archived.
- **Self-modification scope.** Agent should write procedural skills (low risk) but not modify wallet code or cognition core. Enforce via filesystem permissions.

---

## 12. Definition of done for v1

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
- [Caddy — automatic HTTPS](https://caddyserver.com/docs/automatic-https)
- [AWS Graviton / t4g pricing](https://aws.amazon.com/ec2/instance-types/t4/)
- [AWS Systems Manager Session Manager](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager.html)
