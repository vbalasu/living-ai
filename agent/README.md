# Living AI Agent (April) — Databricks Asset Bundle

Personal AI agent deployed to Databricks Apps on Free Edition. FastAPI + FMAPI GPT-5.5 cognition, UC Volume memory, Telegram channel.

## Layout

```
agent/
├── databricks.yml        # bundle root (vars, targets)
├── resources/
│   ├── schema.yml        # workspace.living_ai schema
│   ├── volumes.yml       # config + workspace_dir UC Volumes
│   ├── setup_job.yml     # one-time DDL job
│   └── app.yml           # Databricks App (April)
├── src/                  # app source (deployed as the App's source code)
│   ├── app.py
│   ├── app.yaml          # uvicorn launch command
│   ├── cognition.py
│   ├── config.py
│   ├── heartbeat.py
│   ├── memory.py
│   ├── telegram.py
│   └── requirements.txt
├── sql/
│   ├── ddl.sql           # reference DDL (idempotent)
│   └── setup_notebook.py # actually runs the DDL via the setup job
└── seed/                 # initial identity.md, goals.md (uploaded to volume)
```

## Prereqs (one-time, manual)

1. Databricks CLI configured with profile `free` pointing at your workspace.
2. Create the secrets scope and add the Telegram bot token:
   ```bash
   databricks --profile free secrets create-scope living_ai
   databricks --profile free secrets put-secret living_ai telegram_bot_token --string-value <bot-token>
   databricks --profile free secrets put-secret living_ai telegram_primary_user_handle --string-value vbalasu
   databricks --profile free secrets put-secret living_ai telegram_webhook_secret --string-value <random-32-char-string>
   ```

## Deploy

```bash
cd agent
databricks bundle validate -p free
databricks bundle deploy -p free -t free
databricks bundle run setup_tables -p free -t free   # one-time DDL
```

The bundle creates the schema, volumes, table-setup job, and the App. After deploy, the App URL prints in the deploy output. Set Telegram's webhook to point at it:

```bash
APP_URL=$(databricks --profile free apps get living-ai | jq -r .url)
SECRET=$(databricks --profile free secrets get-secret living_ai telegram_webhook_secret | jq -r .value | base64 -d)
TOKEN=$(databricks --profile free secrets get-secret living_ai telegram_bot_token | jq -r .value | base64 -d)
curl -F "url=${APP_URL}/telegram/webhook" \
     -F "secret_token=${SECRET}" \
     "https://api.telegram.org/bot${TOKEN}/setWebhook"
```

## Targets

- `free` (default) — development mode, 120s heartbeat, 100k token/day cap.
- `prod` — production mode, 60s heartbeat, 500k token/day cap. Same workspace; flip with `-t prod`.

## Variables

Override at deploy time with `--var key=value`:

- `agent_name` (default: `April`)
- `catalog` (default: `workspace`)
- `schema` (default: `living_ai`)
- `app_name` (default: `living-ai`)
- `fmapi_endpoint` (default: `databricks-gpt-5-5`)
- `heartbeat_seconds`
- `daily_token_cap`
- `secrets_scope` (default: `living_ai`)

## Memory

The agent reads/writes:
- `/Volumes/<catalog>/<schema>/config/identity.md` — persona
- `/Volumes/<catalog>/<schema>/config/goals.md` — current goals
- `/Volumes/<catalog>/<schema>/config/learnings.md` — accumulated facts
- `/Volumes/<catalog>/<schema>/config/episodes/<date>.jsonl` — append-only event log

Initial identity.md and goals.md get auto-created by the agent on first run.

## Free Edition notes

- App restarts every 24 h. State is fully externalized (UC Volume) so the agent resumes coherently.
- Heartbeat default is 120s on Free to conserve daily compute quota.
- Only one App allowed; `app_name` defaults to `living-ai`.
- Lakebase unsupported; we use UC Volume JSONL + Delta tables instead.
- No Claude on FMAPI; using GPT-5.5.

## Migrating to a new workspace

```bash
# 1. Configure a new CLI profile pointing at the new workspace
databricks configure --profile <new-profile>

# 2. Create the secrets scope and add tokens
databricks --profile <new-profile> secrets create-scope living_ai
# ... put-secret commands as above

# 3. Override the host in databricks.yml or pass --target with a new target block
databricks bundle deploy -p <new-profile> -t free --var workspace.host=<new-host>
```
