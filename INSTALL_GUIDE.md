# Living-AI install guide

Detailed reference for installing, reconfiguring, and uninstalling the
Living-AI agent. If you just want the 3-step quickstart, see
[README.md](./README.md).

## Table of contents

1. [Prerequisites by OS](#1-prerequisites-by-os)
2. [Download the installer](#2-download-the-installer)
3. [First-time install](#3-first-time-install)
4. [Reconfiguring later](#4-reconfiguring-later)
5. [Choosing a different LLM](#5-choosing-a-different-llm)
6. [Verifying the agent is alive](#6-verifying-the-agent-is-alive)
7. [Uninstalling](#7-uninstalling)
8. [How Telegram I/O actually works](#8-how-telegram-io-actually-works-long-polling-not-webhooks)
9. [Troubleshooting](#9-troubleshooting)
10. [Rebuilding the deployer](#10-rebuilding-the-deployer)

---

## 1. Prerequisites by OS

The installer is a Python `.pex` file — a self-extracting Python executable.
PEX runs natively on macOS and Linux. **On Windows, you need WSL2** (Windows
Subsystem for Linux); PEX does not support running directly on Windows.

You'll need:

- **Python 3.11+** — usually already installed; check with `python3 --version`
- **Databricks CLI** — the installer will offer to install this for you on
  first run, or install it yourself ahead of time using the OS-specific
  command below

Terraform is **not required** — the Databricks CLI bundles its own copy of
Terraform and uses it transparently. (If you happen to have a `terraform` on
PATH, the installer will use it — that's faster than the CLI's first-time
download — but it isn't required.)

### macOS

```bash
brew install python@3.11
brew tap databricks/tap && brew install databricks
```

### Linux

```bash
# Python (usually already installed)
sudo apt install -y python3 curl

# Databricks CLI
curl -fsSL https://raw.githubusercontent.com/databricks/setup-cli/main/install.sh | sudo sh
```

### Windows (via WSL2)

One-time setup:

1. Open **PowerShell as Administrator** and run:
   ```powershell
   wsl --install
   ```
   This installs WSL2 and an Ubuntu distro. Reboot when prompted (~5 min).

2. Open the **Ubuntu** app from the Start menu. The first launch asks you to
   create a Linux username/password.

3. From the Ubuntu prompt, install the same tools as the Linux section above
   (Python, Databricks CLI).

4. Move the downloaded `.pex` from your Windows downloads into your WSL home
   directory before running it. The WSL `/mnt/c/...` mount doesn't honor
   `chmod +x`, so executing from there fails:
   ```bash
   mv /mnt/c/Users/<your-windows-username>/Downloads/living-ai-deploy.pex ~/
   cd ~
   ```

You'll also need:

- A **Databricks workspace** — Free Edition is fine (no credit card needed):
  sign up at <https://www.databricks.com/learn/free-edition>
- A **Personal Access Token** from that workspace
- A **Telegram bot** created via [@BotFather](https://t.me/BotFather)
  (`/newbot`, pick a name, copy the token)
- Your **Telegram username** (without the `@`) — find it in Telegram → menu
  (≡) → your name at top → **Username**

---

## 2. Download the installer

The installer is checked into the repo as a single self-contained file at the
root.

**Direct download link (recommended):**
<https://github.com/vbalasu/living-ai/raw/main/living-ai-deploy.pex>

Save the file somewhere convenient like your Downloads folder. The file is
about 10 MB.

> Want to inspect or rebuild from source? See [Section 10](#10-rebuilding-the-deployer).

---

## 3. First-time install

From the directory where you saved `living-ai-deploy.pex`:

```bash
chmod +x living-ai-deploy.pex
./living-ai-deploy.pex
```

The installer walks you through three short rounds of questions:

### Round 1 — Databricks workspace

- **Workspace URL** — paste `https://dbc-xxxx.cloud.databricks.com` (visible
  in your browser after logging in to Databricks).
- **Personal Access Token** — input is hidden. The installer immediately
  validates the token by calling `/api/2.0/preview/scim/v2/Me`, so a wrong
  token fails fast.

### Round 2 — Telegram bot

- **Bot token** — paste the `numbers:letters` token from @BotFather. The
  installer calls Telegram's `getMe` to verify and prints `✓ verified bot
  @your_bot_name`.
- **Your Telegram username** — without the `@`. The agent only replies to
  messages from this user (whitelist).

### Round 3 — Agent personality

- **Agent name** — defaults to `April`. The agent introduces itself with
  this name.

### Behind the scenes

The installer then:

1. Writes a Databricks CLI profile to `~/.databrickscfg`
2. Stores the Telegram bot token + your username in Databricks Secrets
3. Runs `databricks bundle deploy` to provision the App, Lakebase database,
   schema, and UC volumes
4. Runs `databricks bundle run living_ai_app` to start the App compute and
   push the source code
5. Runs the Lakebase setup job to create conversation memory tables
6. Calls Telegram's `deleteWebhook` (the agent uses long-polling — see
   [Section 8](#8-how-telegram-io-actually-works-long-polling-not-webhooks))

When it finishes, you get a printed summary including a `https://t.me/...`
link to your bot. Open it, tap **Start**, and DM your agent.

A non-secret snapshot of your answers is saved to `~/.living-ai/config.json`
so subsequent runs prefill defaults.

### Advanced settings

Run `./living-ai-deploy.pex --advanced` to also be prompted for:

- Databricks App name (default `living-ai`)
- Unity Catalog catalog (default `workspace`)
- Schema (default `living_ai`)
- Secrets scope (default `living_ai`)
- Lakebase instance name (default `<agent-name>-db`)
- LLM serving endpoint (default `databricks-qwen3-next-80b-a3b-instruct`)
- Heartbeat seconds (default 120)
- Daily LLM token cap (default 100,000)

---

## 4. Reconfiguring later

Run the installer again — it loads `~/.living-ai/config.json` and uses your
previous answers as defaults. Press **ENTER** at any prompt to keep the
existing value.

```bash
./living-ai-deploy.pex
# or, equivalently:
./living-ai-deploy.pex configure
```

Smart skips:

- If `~/.databrickscfg` already has the chosen profile, it offers to **reuse
  the existing PAT** (answer `n` to rotate)
- Asks whether to **rotate the Telegram bot token** (default `n` — only say
  yes if you actually got a new token from @BotFather)

After your edits, the installer re-runs `bundle deploy` (idempotent) so the
new config is applied to the running App.

### Common reconfigurations

| You want to…                            | Pick / change                                                                |
| --------------------------------------- | ---------------------------------------------------------------------------- |
| Rename the agent                        | At the **Agent name** prompt, type a new name                                |
| Switch to a different FMAPI model       | Use `--advanced`, change **LLM serving endpoint name**                       |
| Switch to OpenAI/Anthropic via external | Set up an external endpoint (Section 5), then `--advanced` → endpoint name   |
| Rotate Telegram bot token               | Answer `y` to "Rotate Telegram bot token?"                                   |
| Move to a new workspace                 | Use `--reset` (next subsection)                                              |

### Print or reset saved config

```bash
./living-ai-deploy.pex --print-config       # show ~/.living-ai/config.json
./living-ai-deploy.pex --reset              # ignore saved defaults; full re-onboard
```

The saved config never contains secrets — those live only in Databricks
Secrets and your local `~/.databrickscfg`.

---

## 5. Choosing a different LLM

### Other FMAPI OSS endpoints

The default `databricks-qwen3-next-80b-a3b-instruct` is a strong general-purpose
model and works on Free Edition. Other FMAPI endpoints in your workspace work
too. Set the endpoint name via `./living-ai-deploy.pex --advanced`.

To list endpoints available in your workspace:

```bash
databricks --profile living-ai serving-endpoints list \
  | head -40
```

### External models (OpenAI / Anthropic / Bedrock / …)

You bring your own provider API key and Databricks proxies the calls.

1. Read the official guide:
   <https://docs.databricks.com/aws/en/generative-ai/external-models/>

2. Store the provider key as a Databricks secret. Example for OpenAI:
   ```bash
   databricks --profile living-ai secrets put-secret living_ai openai_api_key \
     --string-value sk-...
   ```

3. Create the external endpoint via UI (**Serving > Create > External model**)
   or SDK.

4. Run `./living-ai-deploy.pex --advanced` and enter the endpoint name at the
   **LLM serving endpoint name** prompt.

The agent code is unchanged — both FMAPI and external endpoints expose the
OpenAI-protocol API surface.

---

## 6. Verifying the agent is alive

After deploy, three quick checks:

1. **App health:** open the App URL printed by the installer; you should see
   a JSON payload like `{"agent": "April", "llm_endpoint": "databricks-qwen3-...", ...}`.

2. **Logs:**
   ```bash
   databricks --profile living-ai apps logs living-ai
   ```
   Look for `agent <name> online; tick=120s; llm=<endpoint>; telegram=polling`.
   (Tailing apps logs requires an OAuth profile, not a PAT profile.)

3. **Telegram:** DM your bot (the link is in the installer's final summary).
   The agent should reply within a few seconds. Conversation history persists
   for the last 60 exchanges and survives app restarts.

If the agent replies with `"<name> hit an error and is recovering."`, tail
the logs — usually it's an endpoint quota / permission issue.

---

## 7. Uninstalling

```bash
./living-ai-deploy.pex uninstall
```

The uninstaller reads `~/.living-ai/config.json` and walks you through:

| Action                                       | Prompt / default                                         |
| -------------------------------------------- | -------------------------------------------------------- |
| Delete the Telegram webhook                  | `Y/n` (default Y)                                        |
| Run `databricks bundle destroy`              | required (after typing the confirmation phrase)          |
| Delete the entire secrets scope              | `y/N` (default N — preserves other apps' secrets)        |
| Delete just the agent's keys in that scope   | `Y/n` if you said no above                               |
| Remove the CLI profile from `~/.databrickscfg` | `y/N` (default N)                                      |
| Remove `~/.living-ai/config.json`            | always (final step)                                      |

You'll be asked to type a confirmation phrase (`uninstall <app_name>`) before
anything destructive happens.

---

## 8. How Telegram I/O actually works (long-polling, not webhooks)

Databricks Apps cannot be made public — the workspace OAuth gate fronts every
route, and Telegram doesn't follow OAuth redirects. Inbound webhooks therefore
never reach the agent. We sidestep this by running Telegram in **long-polling
mode**: the App reaches *outbound* to `api.telegram.org/getUpdates`, which is
unrestricted, and processes messages itself.

Practical implications:

- The installer doesn't register a webhook URL with Telegram.
- The agent's `poll_loop` calls `deleteWebhook` on startup before polling, so
  flipping a bot from a previous webhook-mode deployment "just works."
- A bot can have either a webhook **or** a polling consumer, never both. If
  you point another tool at the same bot via `setWebhook`, the agent's polling
  will start failing with `Conflict: terminated by other getUpdates request`.

---

## 9. Troubleshooting

| Symptom                                                                | Fix                                                                                                                        |
| ---------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| `databricks CLI not found`                                             | The installer offers to install it for you (default Y). Or install manually per Section 1                                  |
| `bundle deploy` fails with `unable to verify checksums signature: openpgp: key expired` | Databricks CLI's bundled terraform downloader hit an expired GPG key. Install Terraform yourself: `brew install terraform` (macOS) or `sudo apt install terraform` (after the HashiCorp APT repo). |
| `token check failed: ... 401`                                          | The PAT is invalid or expired. Generate a new one in the Databricks UI                                                     |
| `couldn't reach Telegram with that token`                              | Token is wrong or revoked. Re-create it via @BotFather (`/token` command lists existing tokens)                            |
| `PERMISSION_DENIED ... rate limit of 0`                                | Default Qwen endpoint should work on Free Edition. If you changed to a non-OSS FMAPI model, switch back or set up external (Section 5) |
| `Endpoint <name> does not exist`                                       | Run `databricks ... serving-endpoints list` and pick a real endpoint name; then `./living-ai-deploy.pex --advanced`        |
| `Could not find env entry for X in app.yaml`                           | The bundled `app.yaml` is out of sync with the deployer. Rebuild the .pex (Section 10)                                     |
| Agent runs but never replies on Telegram                               | See "Telegram bot doesn't respond" below                                                                                   |
| Agent DMs you a "checking in" message every heartbeat                  | Pull the latest .pex and redeploy — older versions had a bug where the 1800s skip window misfired                          |
| You want to wipe and start over                                        | `./living-ai-deploy.pex uninstall`, then `./living-ai-deploy.pex --reset`                                                  |
| Windows: `Permission denied` on `./living-ai-deploy.pex`               | You're outside WSL. PEX doesn't run on native Windows — open the Ubuntu app and run from inside WSL                        |
| WSL: `chmod` succeeds but `./living-ai-deploy.pex` says "not found"    | WSL on `/mnt/c` doesn't honor exec permissions. Move the file to your WSL home: `mv living-ai-deploy.pex ~/ && cd ~`       |

### Telegram bot doesn't respond

The agent uses long-polling (Section 8). A working setup needs three things:
app is running, bot has no webhook registered, and the message comes from
your configured username.

```bash
# 1. App is RUNNING + ACTIVE?
databricks --profile living-ai apps get living-ai \
  | python3 -c "import sys,json; d=json.load(sys.stdin); \
       print('app:', d['app_status']['state']); \
       print('compute:', d['compute_status']['state'])"
# Expected: app: RUNNING / compute: ACTIVE
```

```bash
# 2. No webhook registered? (long-polling requires this)
TOKEN=...   # your bot token
curl -sS "https://api.telegram.org/bot$TOKEN/getWebhookInfo"
# Expected: "url": ""  (empty string)
# If "url" is non-empty, clear it:
curl -sS -X POST "https://api.telegram.org/bot$TOKEN/deleteWebhook"
# Then redeploy:  ./living-ai-deploy.pex configure
```

```bash
# 3. Are you DM'ing as the primary user?
./living-ai-deploy.pex --print-config | grep telegram_user_handle
# If you DM from a different Telegram username, the agent replies:
# "Sorry, I only respond to @<handle>." Reconfigure to change.
```

```bash
# 4. Is polling actually running? (needs OAuth profile)
databricks --profile <oauth-profile> apps logs living-ai | tail -100
# Look for: "agent <name> online; ... telegram=polling"
# If you see "telegram=pending", the bot token secret isn't readable —
# rerun installer and rotate the token.
```

---

## 10. Rebuilding the deployer

Only needed if you change anything under `agent/src/`, `agent/resources/`, or
`agent/databricks.yml`. The build script copies fresh sources into the .pex.

```bash
cd agent/deploy
pipx install pex                # one-time (or: pip install --user pex)
./build.sh                      # writes ../../living-ai-deploy.pex
```

If your network blocks the public PyPI (corporate proxy, etc.), pass the
proxy URL and a pre-installed pip version that pex can find:

```bash
PIP_INDEX_URL=https://pypi-proxy.your-org/simple \
  PEX_PIP_VERSION=26.0.1 \
  ./build.sh
```

The build script bundles:

- `databricks-sdk==0.40.0`
- `agent/databricks.yml`, `agent/resources/`, `agent/src/`, `agent/sql/`

Output: `living-ai-deploy.pex` at the project root.
