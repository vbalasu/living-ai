# Living-AI install guide

This guide walks through installing the **living-ai** agent for the first time,
reconfiguring it later, and uninstalling cleanly. Everything ships as one
self-contained file: `living-ai-deploy.pex`.

The default LLM is the FMAPI OSS endpoint **`databricks-qwen3-next-80b-a3b-instruct`**,
which is available on Databricks Free Edition with no extra setup. To use a
non-OSS provider (OpenAI, Anthropic, Bedrock, etc.), create an *external model*
serving endpoint first per the
[Databricks docs](https://docs.databricks.com/aws/en/generative-ai/external-models/),
then enter that endpoint's name during onboarding.

---

## 1. Prerequisites

You need the following on the machine you're running the installer from:

| Tool             | How to install                                                             |
| ---------------- | -------------------------------------------------------------------------- |
| Python 3.11+     | `brew install python@3.11`  (or your distro's package manager)             |
| Databricks CLI   | https://docs.databricks.com/dev-tools/cli/install.html                     |
| Terraform 1.5+   | `brew install terraform`  (only needed for the bundle deploy step)         |

You also need:

- A **Databricks workspace** (Free Edition is fine) and a **Personal Access Token**
- A **Telegram bot** created via [@BotFather](https://t.me/BotFather) (you'll get a token)

---

## 2. Get the deployer

The repository ships a single artifact at the project root:

```
living-ai-deploy.pex
```

A `.pex` file is a self-extracting Python executable. You **don't** unzip it
manually — Python knows how to run it directly. Just make it executable:

```bash
chmod +x living-ai-deploy.pex
```

> Want to inspect or rebuild it? See [Section 9](#9-rebuilding-the-deployer).

---

## 3. First-time install

Run the deployer:

```bash
./living-ai-deploy.pex
```

You'll be walked through an interactive session. The prompts (in order):

| Prompt                          | What to enter                                                              |
| ------------------------------- | -------------------------------------------------------------------------- |
| CLI profile name                | A name for the profile in `~/.databrickscfg` (default `living-ai`)         |
| Workspace host                  | `https://<your-workspace>.cloud.databricks.com`                            |
| Personal access token           | Paste your PAT (input is hidden)                                           |
| Agent name                      | What the agent calls itself (e.g. `April`)                                 |
| Databricks App name             | `living-ai` is fine                                                        |
| Catalog / Schema                | UC catalog + schema that will hold the agent's volumes & tables            |
| Secrets scope name              | Where channel keys go                                                      |
| Lakebase instance name          | Lakebase Postgres instance name                                            |
| Serving endpoint name           | Default: `databricks-qwen3-next-80b-a3b-instruct` (Free Edition friendly)  |
| Heartbeat seconds               | `120` is safe on Free Edition                                              |
| Daily LLM token cap             | `100000` is safe to start                                                  |
| Telegram bot token              | From @BotFather                                                            |
| Primary Telegram user handle    | Your Telegram username, without the `@`                                    |
| Set Telegram webhook now?       | `Y` (the deployer will register the webhook with Telegram)                 |

The deployer then:

1. Writes the CLI profile to `~/.databrickscfg`
2. Stores Telegram secrets in Databricks Secrets
3. Extracts the bundled Asset Bundle and substitutes your config into it
4. Runs `databricks bundle deploy` to provision the App, Lakebase, volumes, and tables
5. Runs the Lakebase setup job
6. Registers the Telegram webhook

A non-secret snapshot of your answers is saved to `~/.living-ai/config.json` so
that subsequent runs can prefill defaults.

When it finishes, it prints the App URL. DM your bot to greet the agent.

---

## 4. Choosing a different LLM

### Other FMAPI OSS endpoints

The default `databricks-qwen3-next-80b-a3b-instruct` is a strong general-purpose
model and works on Free Edition. Other FMAPI endpoints in your workspace (e.g.
`databricks-meta-llama-3-1-8b-instruct`, `databricks-gpt-5-5`) work too — just
enter the endpoint name at the **Serving endpoint name** prompt, or change it
later via `./living-ai-deploy.pex configure`.

To list the FMAPI endpoints available in your workspace:

```bash
databricks --profile living-ai serving-endpoints list \
  | grep '^name' | head
```

### External models (OpenAI / Anthropic / Bedrock / …)

You bring your own provider API key and Databricks proxies the calls. Your
ChatGPT Plus / Pro subscription doesn't apply directly here — for programmatic
access OpenAI requires a separate API key (different but same account, billed
separately).

Steps:

1. Read the official guide:
   https://docs.databricks.com/aws/en/generative-ai/external-models/
2. Store the provider key as a Databricks secret. Example for OpenAI:
   ```bash
   databricks --profile living-ai secrets put-secret living_ai openai_api_key \
     --string-value sk-...
   ```
3. Create the external endpoint via UI (**Serving > Create > External model**)
   or SDK. For example with Python:
   ```python
   from databricks.sdk import WorkspaceClient
   from databricks.sdk.service.serving import (
       EndpointCoreConfigInput, ServedEntityInput, ExternalModel,
       ExternalModelProvider, OpenAiConfig,
   )

   w = WorkspaceClient(profile="living-ai")
   w.serving_endpoints.create(
       name="openai-gpt-4o-mini",
       config=EndpointCoreConfigInput(
           served_entities=[
               ServedEntityInput(
                   name="openai-gpt-4o-mini",
                   external_model=ExternalModel(
                       provider=ExternalModelProvider.OPENAI,
                       name="gpt-4o-mini",
                       task="llm/v1/chat",
                       openai_config=OpenAiConfig(
                           openai_api_key="{{secrets/living_ai/openai_api_key}}",
                       ),
                   ),
               ),
           ],
       ),
   )
   ```
4. Run `./living-ai-deploy.pex configure` and enter the endpoint name
   (e.g. `openai-gpt-4o-mini`) at the **Serving endpoint name** prompt.

The agent code is unchanged — it speaks the OpenAI-protocol API surface that
both FMAPI and external endpoints expose, so switching is just a config change.

---

## 5. Reconfiguring later

Run the deployer again — it loads `~/.living-ai/config.json` and uses your
previous answers as defaults. Press **ENTER** at any prompt to keep the
existing value.

```bash
./living-ai-deploy.pex
# or, equivalently:
./living-ai-deploy.pex configure
```

The deployer is smart about reusing credentials:

- If `~/.databrickscfg` already has the chosen profile, it offers to **reuse
  the existing PAT** (you can answer `n` to rotate)
- It asks whether to **update the Telegram bot token / handle** (default
  `n` — only say yes if you actually rotated the bot token)

After your edits, the deployer re-runs `bundle deploy` (idempotent) so the
new config is applied to the running App.

### Common reconfigurations

| You want to…                            | Pick / change                                            |
| --------------------------------------- | -------------------------------------------------------- |
| Switch to a different FMAPI model       | Change **Serving endpoint name**                         |
| Switch to OpenAI/Anthropic via external | Create the endpoint first (Section 4), then change **Serving endpoint name** |
| Lower the daily token cap               | Change **Daily LLM token cap**                           |
| Speed up heartbeat ticks                | Change **Heartbeat seconds**                             |
| Rename the agent                        | Change **Agent name**                                    |
| Move to a new workspace                 | Use `--reset` (next section)                             |

### Print or reset saved config

```bash
./living-ai-deploy.pex --print-config       # show ~/.living-ai/config.json
./living-ai-deploy.pex --reset              # ignore saved defaults; full re-onboard
```

The saved config never contains secrets — those live only in Databricks Secrets
and your local `~/.databrickscfg`.

---

## 6. Verifying the agent is alive

After deploy, three quick checks:

1. **App health:** open the App URL printed by the deployer; you should see a
   JSON payload like `{"agent": "April", "llm_endpoint": "databricks-qwen3-...", ...}`.
2. **Logs:**
   ```bash
   databricks --profile living-ai apps logs living-ai
   ```
   Look for `agent <name> online; tick=120s; llm=<endpoint>; telegram=configured`.
3. **Telegram:** DM your bot. The agent should reply within a few seconds.

If the agent replies with `"<name> hit an error and is recovering."`, tail the
logs — usually it's an endpoint quota / permission issue.

---

## 7. Uninstalling

```bash
./living-ai-deploy.pex uninstall
```

The uninstaller reads `~/.living-ai/config.json` to know what was deployed,
then walks you through:

| Action                                       | Prompt / default                                         |
| -------------------------------------------- | -------------------------------------------------------- |
| Delete the Telegram webhook                  | `Y/n` (default Y)                                        |
| Run `databricks bundle destroy`              | required (after typing the confirmation phrase)          |
| Delete the entire secrets scope              | `y/N` (default N — preserves other apps' secrets)        |
| Delete just the agent's keys in that scope   | `Y/n` if you said no above                               |
| Remove the CLI profile from `~/.databrickscfg` | `y/N` (default N)                                      |
| Remove `~/.living-ai/config.json`            | always (final step)                                      |

You'll be asked to type a confirmation phrase (`uninstall <app_name>`) before
anything destructive happens. After that, the uninstaller:

1. Calls Telegram's `deleteWebhook` API (using the bot token from secrets)
2. Runs `databricks bundle destroy -t free --auto-approve`, which removes the App,
   Lakebase instance, schema, volumes, and the Lakebase setup job
3. Deletes the chosen secrets (or the whole scope)
4. Removes `~/.living-ai/config.json`
5. Optionally strips the CLI profile from `~/.databrickscfg`

If `bundle destroy` partially fails (e.g. one resource was already deleted),
the uninstaller logs the error and continues with the remaining steps.

### Force-clean by hand

If you need to wipe state without going through the uninstaller (e.g. the
saved config is gone), do it manually:

```bash
# 1. Bundle destroy from a checkout of the agent dir
cd agent
databricks --profile living-ai bundle destroy -t free \
  --var app_name=living-ai --var catalog=workspace --var schema=living_ai \
  --var llm_endpoint=databricks-qwen3-next-80b-a3b-instruct \
  --var secrets_scope=living_ai --var lakebase_instance=april-db \
  --var agent_name=April --var heartbeat_seconds=120 --var daily_token_cap=100000

# 2. Delete the secrets scope (or just the keys you set)
databricks --profile living-ai secrets delete-scope living_ai

# 3. Delete the Telegram webhook
curl -X POST "https://api.telegram.org/bot<TOKEN>/deleteWebhook"

# 4. Remove local state
rm -rf ~/.living-ai
```

---

## 8. Troubleshooting

| Symptom                                                                | Fix                                                                                                                        |
| ---------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| `databricks CLI not found`                                             | Install Databricks CLI from the docs link above                                                                            |
| `PERMISSION_DENIED ... rate limit of 0`                                | Default Qwen endpoint should work on Free Edition. If you changed to a non-OSS FMAPI model, switch back or set up an external endpoint (Section 4). |
| `Endpoint <name> does not exist`                                       | Run `databricks ... serving-endpoints list` and pick a real endpoint name; then `./living-ai-deploy.pex configure`.        |
| `Could not find env entry for X in app.yaml`                           | The bundled `app.yaml` is out of sync with the deployer. Rebuild the .pex (Section 9).                                     |
| `bundle deploy` fails with a Terraform error                           | Install Terraform 1.5+ and put it on your `PATH`.                                                                          |
| Agent runs but never replies on Telegram                               | Re-run the deployer and answer `y` to "Update Telegram…" and `Y` to "Set webhook"; confirm the handle has no leading `@`.  |
| You want to wipe and start over                                        | `./living-ai-deploy.pex uninstall`, then `./living-ai-deploy.pex --reset`.                                                 |

---

## 9. Rebuilding the deployer

Only needed if you change anything under `agent/src/`, `agent/resources/`, or
`agent/databricks.yml`. The build script copies fresh sources into the .pex.

```bash
cd agent/deploy
pip install --user pex          # one-time
./build.sh                      # writes ../../living-ai-deploy.pex
```

The build script bundles:

- `databricks-sdk==0.40.0`
- `agent/databricks.yml`, `agent/resources/`, `agent/src/`, `agent/sql/`

Output: `living-ai-deploy.pex` at the project root.
