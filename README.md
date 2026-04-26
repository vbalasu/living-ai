# Living-AI

An autonomous AI agent that lives in your Databricks workspace and chats with
you on Telegram. Runs free on Databricks Free Edition — uses an open-source
LLM (Qwen 3) hosted by Databricks, so no LLM bills. Remembers the last 60
exchanges across restarts.

## Quickstart (3 steps)

### 1. Download

Click the link below and save `living-ai-deploy.pex` somewhere you can find
it (your Desktop or Downloads folder is fine).

**[⬇ Download living-ai-deploy.pex](https://github.com/vbalasu/living-ai/raw/main/living-ai-deploy.pex)**

### 2. Install

Open a terminal in the folder where you saved the file and run:

**macOS or Linux:**
```bash
chmod +x living-ai-deploy.pex
./living-ai-deploy.pex
```

**Windows:** the installer requires WSL2 (Windows Subsystem for Linux). One-time setup:
1. Open PowerShell as Administrator and run `wsl --install` (creates an Ubuntu environment, takes ~5 minutes; reboot when prompted)
2. Open the **Ubuntu** app from the Start menu
3. In the Ubuntu terminal, install the prerequisites once:
   ```bash
   sudo apt update && sudo apt install -y python3 unzip curl
   curl -fsSL https://raw.githubusercontent.com/databricks/setup-cli/main/install.sh | sudo sh
   curl -fsSL https://apt.releases.hashicorp.com/gpg | sudo gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg
   echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/hashicorp.list
   sudo apt update && sudo apt install -y terraform
   ```
4. From WSL Ubuntu, navigate to where you saved the file (e.g. `cd /mnt/c/Users/<you>/Downloads`), then:
   ```bash
   chmod +x living-ai-deploy.pex
   ./living-ai-deploy.pex
   ```

The installer will ask you three short rounds of questions (your Databricks
workspace, your Telegram bot, your agent's name). Each prompt explains what
it's asking and why. Defaults are filled in for everything else — just press
ENTER to accept.

### 3. Chat

When the installer finishes, it prints a `https://t.me/<your-bot>` link. Open
it in Telegram, tap **Start**, and DM your agent. It replies in seconds.

---

## What you'll need

Before running the installer, have these ready:

| Thing                         | How to get it                                                                                                |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------ |
| Databricks workspace          | Sign up free at <https://www.databricks.com/learn/free-edition>                                             |
| Databricks personal token     | In your workspace: avatar (top-right) → Settings → Developer → Access tokens → **Generate new token**       |
| Telegram bot token            | DM [@BotFather](https://t.me/BotFather), send `/newbot`, follow prompts (~30 seconds)                        |
| Your Telegram username        | Telegram → Settings — it's the line that starts with `@`                                                     |

The installer also needs three local tools — they're free and one-time installs:

| Tool             | macOS                            | Linux / WSL Ubuntu                                                                                           |
| ---------------- | -------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| Python 3.11+     | `brew install python@3.11`       | usually preinstalled; else `sudo apt install python3`                                                        |
| Databricks CLI   | `brew tap databricks/tap && brew install databricks` | `curl -fsSL https://raw.githubusercontent.com/databricks/setup-cli/main/install.sh \| sudo sh` |
| Terraform 1.5+   | `brew install terraform`         | see HashiCorp APT repo command in the Windows section above                                                  |

---

## Want the full reference?

[INSTALL_GUIDE.md](./INSTALL_GUIDE.md) — every flag, advanced options, troubleshooting, uninstall, switching LLMs (OpenAI / Anthropic / Bedrock via external models), and rebuilding the deployer.
