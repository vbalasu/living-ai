"""Living-AI agent deployer.

Self-contained Python entry point that deploys the agent into a target
Databricks workspace. The bundle YAML + source files are packaged inside
this binary; we extract them to a temporary directory and shell out to the
`databricks` CLI for the bundle commands. Secrets are set directly via the
SDK so they never hit a config file or shell history.

Usage:
    living-ai-deploy.pex                    # interactive deploy / reconfigure
    living-ai-deploy.pex configure          # alias for the default
    living-ai-deploy.pex uninstall          # tear everything down
    living-ai-deploy.pex --reset            # ignore saved defaults
    living-ai-deploy.pex --print-config     # dump saved (non-secret) config

Default LLM endpoint: databricks-qwen3-next-80b-a3b-instruct (FMAPI OSS,
available on Databricks Free Edition). To use OpenAI / Anthropic / Bedrock,
create an external model serving endpoint per
https://docs.databricks.com/aws/en/generative-ai/external-models/ and pass
its name during onboarding.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

from databricks.sdk import WorkspaceClient

try:
    from importlib import resources as importlib_resources
except ImportError:  # pragma: no cover
    import importlib_resources  # type: ignore

from . import prompts


DEFAULT_LLM_ENDPOINT = "databricks-qwen3-next-80b-a3b-instruct"
EXTERNAL_MODELS_DOCS = "https://docs.databricks.com/aws/en/generative-ai/external-models/"


# --- bundle file packaging ---

def _bundle_files() -> dict[str, bytes]:
    """Read bundled agent source/yaml files keyed by relative path."""
    pkg_root = importlib_resources.files("living_ai_deploy") / "bundle_files"
    out: dict[str, bytes] = {}

    def walk(node, prefix: str = "") -> None:
        for entry in node.iterdir():
            sub = f"{prefix}/{entry.name}" if prefix else entry.name
            if entry.is_dir():
                walk(entry, sub)
            else:
                out[sub] = entry.read_bytes()

    walk(pkg_root)
    return out


def extract_bundle(target_dir: Path) -> None:
    files = _bundle_files()
    if not files:
        raise RuntimeError("No bundle files packaged in this binary.")
    for rel, content in files.items():
        path = target_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


# --- prereqs ---

def _platform_install_cmd_for_databricks() -> tuple[str, list[str]] | None:
    """Return (label, [shell-cmd, …]) for installing Databricks CLI on this OS."""
    if sys.platform == "darwin":
        if shutil.which("brew"):
            return ("Homebrew", ["bash", "-lc", "brew tap databricks/tap && brew install databricks"])
        return None
    if sys.platform.startswith("linux"):
        return ("Databricks setup-cli script (requires sudo)",
                ["bash", "-lc",
                 "curl -fsSL https://raw.githubusercontent.com/databricks/setup-cli/main/install.sh | sudo sh"])
    return None


def ensure_databricks_cli() -> str:
    """Find or install the Databricks CLI. Returns the path to the binary."""
    cli = shutil.which("databricks")
    if cli:
        return cli

    print()
    print("  Databricks CLI is required but not found on your PATH.")
    install = _platform_install_cmd_for_databricks()
    if install is None:
        print()
        print("  No automatic install available for this platform. Install manually:")
        print("    https://docs.databricks.com/dev-tools/cli/install.html")
        print("  Then re-run this installer.")
        sys.exit(2)

    label, cmd = install
    print(f"  I can install it via {label}:")
    print(f"    {cmd[-1]}")
    print()
    if not prompts.ask_yn("  Install Databricks CLI now?", default=True):
        print()
        print("  Install it manually and re-run this installer:")
        print("    https://docs.databricks.com/dev-tools/cli/install.html")
        sys.exit(2)

    print()
    print("  Running install (you may be prompted for your password)...")
    print()
    rc = subprocess.run(cmd).returncode
    if rc != 0:
        print()
        print("  Install command exited non-zero. Try installing manually and re-run:")
        print("    https://docs.databricks.com/dev-tools/cli/install.html")
        sys.exit(2)

    cli = shutil.which("databricks")
    if not cli:
        # PATH may not have refreshed in this shell; try common Homebrew + setup-cli locations.
        for candidate in ("/opt/homebrew/bin/databricks", "/usr/local/bin/databricks", "/usr/bin/databricks"):
            if Path(candidate).exists():
                cli = candidate
                break
    if not cli:
        print()
        print("  Databricks CLI installed but isn't on this shell's PATH yet.")
        print("  Open a new terminal and re-run the installer.")
        sys.exit(2)

    print(f"  ✓ Databricks CLI ready at {cli}")
    return cli


def check_prereqs() -> str:
    """Make sure Databricks CLI is available; install with consent if not.

    Terraform is not required as a separate dependency — the Databricks CLI
    bundles its own terraform downloader and uses it transparently. If the
    operator already has a terraform binary on PATH, run_databricks() will
    point the CLI at it (faster + avoids any download issues), but it isn't
    required.
    """
    return ensure_databricks_cli()


# --- saved config snapshot (no secrets) ---

CONFIG_DIR = Path.home() / ".living-ai"
CONFIG_FILE = CONFIG_DIR / "config.json"


def load_saved_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        cfg = json.loads(CONFIG_FILE.read_text())
    except Exception:
        return {}
    # Fill in any keys missing from older config snapshots so callers can
    # `cfg["x"]` without KeyErrors. Values come from current defaults.
    defaults = {
        "profile": "living-ai",
        "agent_name": "April",
        "app_name": "living-ai",
        "catalog": "workspace",
        "schema": "living_ai",
        "secrets_scope": "living_ai",
        "lakebase_instance": "april-db",
        "llm_endpoint": DEFAULT_LLM_ENDPOINT,
        "heartbeat_seconds": 120,
        "daily_token_cap": 100000,
    }
    for k, v in defaults.items():
        cfg.setdefault(k, v)
    return cfg


_SECRET_KEYS = {"pat", "bot_token", "telegram_bot_token", "openai_api_key", "webhook_secret"}


def save_config(snapshot: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    safe = {k: v for k, v in snapshot.items() if k not in _SECRET_KEYS}
    CONFIG_FILE.write_text(json.dumps(safe, indent=2))
    try:
        CONFIG_FILE.chmod(0o600)
    except Exception:
        pass


def remove_saved_config() -> bool:
    if CONFIG_FILE.exists():
        CONFIG_FILE.unlink()
        return True
    return False


# --- profile + secrets ---

def existing_profile(profile: str) -> str | None:
    """Return the host configured for `profile` in ~/.databrickscfg, or None."""
    cfg_path = Path.home() / ".databrickscfg"
    if not cfg_path.exists():
        return None
    in_section = False
    host = None
    for line in cfg_path.read_text().splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            in_section = (s == f"[{profile}]")
            continue
        if in_section and s.startswith("host"):
            _, _, val = s.partition("=")
            host = val.strip()
    return host


def configure_profile(host: str, pat: str, profile: str) -> None:
    cfg_path = Path.home() / ".databrickscfg"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    existing = cfg_path.read_text() if cfg_path.exists() else ""

    out_lines: list[str] = []
    skip = False
    for line in existing.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            skip = (stripped == f"[{profile}]")
            if not skip:
                out_lines.append(line)
            continue
        if not skip:
            out_lines.append(line)

    out_lines.append(f"\n[{profile}]")
    out_lines.append(f"host = {host}")
    out_lines.append(f"token = {pat}")
    cfg_path.write_text("\n".join(out_lines).strip() + "\n")


def remove_profile(profile: str) -> bool:
    """Strip [<profile>] section from ~/.databrickscfg. Returns True if removed."""
    cfg_path = Path.home() / ".databrickscfg"
    if not cfg_path.exists():
        return False
    out_lines: list[str] = []
    skip = False
    found = False
    for line in cfg_path.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if stripped == f"[{profile}]":
                skip = True
                found = True
                continue
            skip = False
        if not skip:
            out_lines.append(line)
    if found:
        cfg_path.write_text("\n".join(out_lines).strip() + "\n")
    return found


def ensure_secrets(w: WorkspaceClient, scope: str, kvs: dict[str, str]) -> None:
    scopes = [s.name for s in w.secrets.list_scopes()]
    if scope not in scopes:
        w.secrets.create_scope(scope=scope)
        print(f"  created secrets scope '{scope}'")
    else:
        print(f"  scope '{scope}' already exists")
    for k, v in kvs.items():
        if v:
            w.secrets.put_secret(scope=scope, key=k, string_value=v)
            print(f"  set secret '{scope}/{k}'")


def _instance_state(inst) -> str:
    """Normalize a DatabaseInstance.state value to a bare string like 'AVAILABLE'."""
    raw = getattr(inst, "state", None) or getattr(inst, "lifecycle_state", None)
    if raw is None:
        return ""
    # SDK returns an enum; .value works for that, str() works for plain strings.
    s = getattr(raw, "value", None) or str(raw)
    # str() on an enum yields 'DATABASEINSTANCESTATE.AVAILABLE'; trim that prefix.
    return s.split(".")[-1].upper()


def ensure_lakebase_instance(w: WorkspaceClient, instance_name: str,
                             capacity: str = "CU_1", timeout_seconds: int = 600) -> None:
    """Make sure a Lakebase Postgres instance exists, creating + waiting if needed."""
    import time

    try:
        existing = w.database.get_database_instance(instance_name)
        state = _instance_state(existing)
        print(f"  Lakebase instance '{instance_name}' already exists (state: {state or '?'})")
        if state == "AVAILABLE":
            return
        # Otherwise fall through and wait below.
    except Exception:
        # Doesn't exist — create it.
        print(f"  creating Lakebase instance '{instance_name}' (capacity {capacity}) …")
        try:
            from databricks.sdk.service.database import DatabaseInstance
            w.database.create_database_instance(
                DatabaseInstance(name=instance_name, capacity=capacity),
            )
        except Exception as exc:
            raise RuntimeError(
                f"Could not create Lakebase instance '{instance_name}': {exc}"
            ) from exc

    print(f"  waiting for Lakebase instance to become AVAILABLE (this can take ~3-5 minutes) …")
    deadline = time.time() + timeout_seconds
    last_state = None
    while time.time() < deadline:
        try:
            inst = w.database.get_database_instance(instance_name)
            state = _instance_state(inst)
            if state != last_state:
                print(f"    state: {state or '?'}")
                last_state = state
            if state == "AVAILABLE":
                return
            if state in ("FAILED", "DELETING", "DELETED"):
                raise RuntimeError(f"Lakebase instance '{instance_name}' is in state {state}")
        except Exception as exc:
            if "not found" in str(exc).lower():
                pass
            else:
                print(f"    poll error: {exc}")
        time.sleep(15)
    raise RuntimeError(
        f"Timed out after {timeout_seconds}s waiting for Lakebase instance '{instance_name}'"
    )


def read_secret(w: WorkspaceClient, scope: str, key: str) -> str | None:
    try:
        raw = w.secrets.get_secret(scope=scope, key=key).value
        return base64.b64decode(raw).decode()
    except Exception:
        return None


def delete_secrets(w: WorkspaceClient, scope: str, keys: list[str]) -> None:
    for k in keys:
        try:
            w.secrets.delete_secret(scope=scope, key=k)
            print(f"  deleted secret '{scope}/{k}'")
        except Exception as exc:
            print(f"  could not delete secret '{scope}/{k}': {exc}")


def delete_secret_scope(w: WorkspaceClient, scope: str) -> None:
    try:
        w.secrets.delete_scope(scope=scope)
        print(f"  deleted secrets scope '{scope}'")
    except Exception as exc:
        print(f"  could not delete secrets scope '{scope}': {exc}")


# --- bundle deploy via CLI ---

def run_databricks(cli: str, args: list[str], profile: str,
                   tf_exec_path: str | None = None,
                   cwd: Path | None = None) -> None:
    env = os.environ.copy()
    if tf_exec_path:
        env["DATABRICKS_TF_EXEC_PATH"] = tf_exec_path
        env["DATABRICKS_TF_VERSION"] = _terraform_version(tf_exec_path)
    cmd = [cli, "--profile", profile] + args
    print(f"  $ {' '.join(cmd)}")
    res = subprocess.run(cmd, env=env, cwd=str(cwd) if cwd else None)
    if res.returncode != 0:
        raise RuntimeError(f"`databricks {' '.join(args)}` failed (exit {res.returncode})")


def _terraform_version(tf_path: str) -> str:
    res = subprocess.run([tf_path, "version", "-json"], capture_output=True, text=True)
    if res.returncode != 0:
        return "1.5.7"
    try:
        return json.loads(res.stdout)["terraform_version"]
    except Exception:
        for line in res.stdout.splitlines():
            if line.startswith("Terraform v"):
                return line[len("Terraform v"):].strip()
    return "1.5.7"


def bundle_var_args(snapshot: dict) -> list[str]:
    return [
        "--var", f"agent_name={snapshot['agent_name']}",
        "--var", f"catalog={snapshot['catalog']}",
        "--var", f"schema={snapshot['schema']}",
        "--var", f"app_name={snapshot['app_name']}",
        "--var", f"llm_endpoint={snapshot['llm_endpoint']}",
        "--var", f"secrets_scope={snapshot['secrets_scope']}",
        "--var", f"lakebase_instance={snapshot['lakebase_instance']}",
        "--var", f"heartbeat_seconds={snapshot['heartbeat_seconds']}",
        "--var", f"daily_token_cap={snapshot['daily_token_cap']}",
    ]


# --- substitution ---

def substitute_bundle_profile(bundle_dir: Path, profile: str) -> None:
    """Patch databricks.yml so workspace.profile matches the CLI profile we just configured.

    Without this, a user installing into a workspace different from the one baked
    into the shipped bundle YAML hits "host in profile doesn't match host in bundle".
    """
    db_yml = bundle_dir / "databricks.yml"
    text = db_yml.read_text()
    pattern = re.compile(r"^(\s*profile:\s*)\S+$", re.MULTILINE)
    new_text, count = pattern.subn(rf"\g<1>{profile}", text)
    if count == 0:
        # No profile line found; harmless — the --profile CLI flag will be used.
        return
    db_yml.write_text(new_text)


def substitute_app_yaml(bundle_dir: Path, snapshot: dict) -> None:
    """Inject runtime config values into src/app.yaml so the App env reflects user input."""
    app_yaml = bundle_dir / "src" / "app.yaml"
    text = app_yaml.read_text()

    catalog = snapshot["catalog"]
    schema = snapshot["schema"]

    env_overrides = {
        "AGENT_NAME": snapshot["agent_name"],
        "CATALOG": catalog,
        "SCHEMA": schema,
        "CONFIG_VOLUME_PATH": f"/Volumes/{catalog}/{schema}/config",
        "WORKSPACE_VOLUME_PATH": f"/Volumes/{catalog}/{schema}/workspace_dir",
        "LLM_ENDPOINT": snapshot["llm_endpoint"],
        "HEARTBEAT_SECONDS": str(snapshot["heartbeat_seconds"]),
        "DAILY_TOKEN_CAP": str(snapshot["daily_token_cap"]),
        "SECRETS_SCOPE": snapshot["secrets_scope"],
        "LAKEBASE_INSTANCE": snapshot["lakebase_instance"],
    }

    def replace_value(t: str, key: str, value: str) -> str:
        pattern = re.compile(
            rf'(- name: {re.escape(key)}\n    value: ")[^"]*(")', re.MULTILINE
        )
        new_t, count = pattern.subn(rf'\g<1>{value}\g<2>', t)
        if count == 0:
            raise RuntimeError(f"Could not find env entry for {key} in app.yaml")
        return new_t

    for k, v in env_overrides.items():
        text = replace_value(text, k, v)

    app_yaml.write_text(text)


# --- Telegram (long-polling mode) ---
#
# Databricks Apps don't accept anonymous inbound traffic, so the agent uses
# Telegram long-polling instead of webhooks. The deployer only needs to make
# sure no webhook is registered on the bot — Telegram refuses getUpdates if
# a webhook is configured.

def delete_telegram_webhook(token: str) -> bool:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/deleteWebhook",
        data=urllib.parse.urlencode({"drop_pending_updates": "false"}).encode(),
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode()
            print(f"  telegram response: {body[:200]}")
            return '"ok":true' in body
    except Exception as exc:
        print(f"  telegram deleteWebhook failed: {exc}")
        return False


def _wait_for_app_gone(w: WorkspaceClient, app_name: str,
                       timeout_seconds: int = 1800) -> None:
    """Poll until `apps get` 404s.

    `bundle destroy` returns as soon as the API has accepted the delete, but
    Databricks Apps cleanup can stall in DELETING and even flip to ERROR
    before the resource fully disappears. When that happens, an explicit
    `apps delete` after the platform's 20-min cooldown unsticks it.
    """
    import time
    start = time.time()
    last_state = None
    last_explicit_delete = 0.0
    while time.time() - start < timeout_seconds:
        try:
            app = w.apps.get(app_name)
        except Exception:
            print(f"  app '{app_name}' is gone")
            return
        compute = getattr(getattr(app, "compute_status", None), "state", None)
        state = getattr(compute, "value", None) or str(compute or "?")
        state = state.split(".")[-1].upper()
        if state != last_state:
            print(f"  compute={state}")
            last_state = state
        # If the platform reported ERROR or any non-DELETING terminal state,
        # nudge it with an explicit delete (rate-limited to once per ~5 min so
        # we don't trip the platform's 20-min cooldown error storm).
        if state and state != "DELETING":
            now = time.time()
            if now - last_explicit_delete > 300:
                last_explicit_delete = now
                try:
                    w.apps.delete(app_name)
                    print("  retry-delete sent")
                except Exception as exc:
                    msg = str(exc)
                    if "Cannot delete" not in msg:
                        print(f"  retry-delete error: {msg[:120]}")
        time.sleep(60)
    print(f"  WARNING: app '{app_name}' still present after {timeout_seconds}s; "
          f"continue manually with `databricks apps delete {app_name}`")


def telegram_get_me(token: str) -> dict | None:
    """Call Telegram getMe and return {'username': ..., 'first_name': ...} on success."""
    try:
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/getMe")
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode())
            if body.get("ok"):
                return body.get("result") or {}
    except Exception:
        return None
    return None


# --- CLI parsing ---

def parse_args(argv: list[str]) -> dict:
    args = {"reset": False, "print_config": False, "advanced": False, "command": "deploy"}
    for a in argv[1:]:
        if a in ("--reset", "-r"):
            args["reset"] = True
        elif a == "--print-config":
            args["print_config"] = True
        elif a in ("--advanced", "-a"):
            args["advanced"] = True
        elif a in ("uninstall", "destroy"):
            args["command"] = "uninstall"
        elif a in ("configure", "deploy", "reconfigure"):
            args["command"] = "deploy"
        elif a in ("-h", "--help"):
            print(__doc__)
            sys.exit(0)
        else:
            print(f"Unknown argument: {a}", file=sys.stderr)
            sys.exit(2)
    return args


# --- main entry ---

def main() -> None:
    args = parse_args(sys.argv)

    if args["print_config"]:
        cfg = load_saved_config()
        print("(no saved config)" if not cfg else json.dumps(cfg, indent=2))
        return

    if args["command"] == "uninstall":
        run_uninstall()
        return

    run_deploy(reset=args["reset"], advanced=args["advanced"])


# --- deploy flow ---

def _hr() -> None:
    print("─" * 72)


def _section(title: str) -> None:
    print()
    _hr()
    print(f"  {title}")
    _hr()


def _explain(*lines: str) -> None:
    for line in lines:
        print(f"  {line}")
    print()


def run_deploy(reset: bool, advanced: bool = False) -> None:
    saved = {} if reset else load_saved_config()

    print()
    print("  ┌─────────────────────────────────────────────────────────────────┐")
    print("  │  Living-AI agent installer                                      │")
    print("  └─────────────────────────────────────────────────────────────────┘")
    print()
    print("  This installer will set up an autonomous AI agent that lives in")
    print("  your Databricks workspace and chats with you on Telegram.")
    print()
    if saved:
        print(f"  Found saved config at {CONFIG_FILE}.")
        print("  Press ENTER at any prompt to keep the existing value.")
    else:
        print("  We'll ask three short rounds of questions:")
        print("    1. Databricks workspace  (where the agent runs)")
        print("    2. Telegram bot          (how you talk to it)")
        print("    3. Agent personality     (what to call it)")
        if not advanced:
            print()
            print("  Sensible defaults are used for everything else. To see and edit")
            print("  every setting, re-run with --advanced.")

    cli = check_prereqs()
    tf = shutil.which("terraform")
    print()
    print(f"  Databricks CLI:  {cli}")
    if tf:
        print(f"  Terraform:       {tf} (will be used for faster bundle deploys)")
    else:
        print(f"  Terraform:       not installed — Databricks CLI will fetch it on demand")

    # ===== Block 1: Databricks workspace =====
    _section("1. Databricks workspace")
    _explain(
        "The agent runs as a Databricks App. Free Edition works fine — no",
        "credit card needed.",
        "",
        "  Don't have a workspace yet? Sign up free in ~2 minutes:",
        "    https://www.databricks.com/learn/free-edition",
        "    (no credit card; OSS LLMs are included so the agent runs at no cost)",
        "",
        "  Where to find your workspace URL:",
        "    Sign in to Databricks. The browser address bar shows something like",
        "      https://dbc-xxxxxxxx-yyyy.cloud.databricks.com    (AWS Free Edition)",
        "      https://adb-xxxxxxxxx.x.azuredatabricks.net       (Azure)",
        "    Copy that URL (the part up to .com / .net).",
        "",
        "  How to create a Personal Access Token (~30 seconds):",
        "    1. In your workspace, click the avatar (top-right) → Settings",
        "    2. Pick Developer → Access tokens → 'Generate new token'",
        "    3. Pick a name (e.g. 'living-ai'), set lifetime, click Generate",
        "    4. Copy the token (starts with 'dapi...') — you only see it once",
    )

    profile_default = saved.get("profile", "living-ai")
    if advanced:
        profile = prompts.ask("CLI profile name to write to ~/.databrickscfg", default=profile_default)
    else:
        profile = profile_default

    existing_host = existing_profile(profile)
    reuse_creds = False
    if existing_host:
        print(f"  Found existing Databricks CLI profile '{profile}' pointing at:")
        print(f"    {existing_host}")
        reuse_creds = prompts.ask_yn(
            "  Use this existing profile? (no = enter a new workspace + token)",
            default=True,
        )

    if reuse_creds:
        host = existing_host
        pat = ""
        # Sanity-check: confirm the existing profile still works.
        print("  testing existing profile...")
        try:
            w_test = WorkspaceClient(profile=profile)
            me = w_test.current_user.me()
            user = getattr(me, "user_name", None) or getattr(me, "display_name", "?")
            print(f"  ✓ profile works; signed in as {user}")
        except Exception as exc:
            print(f"  ✗ existing profile failed: {exc}")
            print("    Falling back to entering a new workspace + token.")
            reuse_creds = False
            existing_host = None

    if not reuse_creds:
        host_default = existing_host if existing_host and not saved else None
        host = prompts.ask(
            "Workspace URL (https://...)",
            default=host_default,
            validate=prompts.validate_host,
        ).rstrip("/")
        while True:
            pat = prompts.ask(
                "Personal access token (input is hidden)",
                secret=True,
                validate=prompts.validate_pat,
            )
            print("  testing token against the workspace...")
            err = _validate_pat_live(host, pat)
            if err is None:
                break
            print(f"  ✗ {err}")
            if not prompts.ask_yn("  retry with a different token?", default=True):
                print("Aborted.", file=sys.stderr)
                sys.exit(1)

    # ===== Block 2: Telegram bot =====
    _section("2. Telegram bot")
    _explain(
        "You'll talk to the agent on Telegram. We need two things:",
        "",
        "  • A bot token from @BotFather (a Telegram-provided helper bot)",
        "  • Your own Telegram username  (so the agent only replies to you)",
        "",
        "  How to create a bot — takes ~30 seconds:",
        "    1. Open Telegram and DM @BotFather  https://t.me/BotFather",
        "    2. Send  /newbot",
        "    3. Pick a display name (anything, e.g. 'My Living-AI Agent')",
        "    4. Pick a username — must be unique and end with 'bot'",
        "       (e.g.  april_living_ai_bot)",
        "    5. BotFather replies with a token like  123456:AAH...xyz",
        "    6. Copy that whole token and paste it below",
        "",
        "  How to find your own Telegram username:",
        "    Telegram → tap the menu (≡) → tap your name at the top",
        "    Look for the line  'Username  @your_username'",
        "    Don't have one yet? Tap 'Username' and create one (letters/digits/underscores).",
    )

    # See if we already have a working bot in Databricks Secrets from a prior install.
    existing_bot_username = ""
    existing_bot_token = ""
    if saved and reuse_creds:
        try:
            tmp_w = WorkspaceClient(profile=profile)
            existing_bot_token = read_secret(tmp_w, saved.get("secrets_scope", "living_ai"),
                                             "telegram_bot_token") or ""
            if existing_bot_token:
                info = telegram_get_me(existing_bot_token)
                if info and info.get("username"):
                    existing_bot_username = info["username"]
        except Exception:
            existing_bot_token = ""

    bot_token = ""
    rotate_bot = True
    if existing_bot_username:
        print(f"  Currently connected to bot @{existing_bot_username}.")
        rotate_bot = prompts.ask_yn(
            "  Use a different bot? (no = keep this one)",
            default=False,
        )

    if rotate_bot:
        while True:
            bot_token = prompts.ask(
                "Bot token from @BotFather",
                secret=True,
                validate=prompts.validate_telegram_token,
            )
            bot_info = telegram_get_me(bot_token)
            if bot_info and bot_info.get("username"):
                print(f"  ✓ verified bot @{bot_info['username']} ({bot_info.get('first_name','')})")
                break
            print("  ✗ couldn't reach Telegram with that token; double-check and retry")
            if not prompts.ask_yn("  retry?", default=True):
                print("Aborted.", file=sys.stderr)
                sys.exit(1)

    # Telegram user handle: only show a default if we have one from the same workspace
    # AND the user is reusing those credentials (i.e. we believe the saved value is theirs).
    handle_default = saved.get("telegram_user_handle") if (saved and reuse_creds) else None
    user_handle = prompts.ask(
        "Your Telegram username (without the @)",
        default=handle_default,
    ).lstrip("@")

    # ===== Block 3: Agent personality =====
    _section("3. Agent personality")
    _explain(
        "What should we call your agent? It will introduce itself with this",
        "name. You can rename it later by re-running the installer.",
    )
    agent_name = prompts.ask("Agent name", default=saved.get("agent_name", "April"))

    # ===== Advanced (hidden by default) =====
    if advanced:
        _section("4. Advanced settings")
        _explain("Override Databricks resource names, LLM endpoint, heartbeat, token cap.")
        app_name = prompts.ask(
            "Databricks App name",
            default=saved.get("app_name", "living-ai"),
            validate=prompts.validate_app_name,
        )
        catalog = prompts.ask(
            "Unity Catalog catalog",
            default=saved.get("catalog", "workspace"),
            validate=prompts.validate_identifier,
        )
        schema = prompts.ask(
            "Schema",
            default=saved.get("schema", "living_ai"),
            validate=prompts.validate_identifier,
        )
        secrets_scope = prompts.ask(
            "Secrets scope name",
            default=saved.get("secrets_scope", "living_ai"),
            validate=prompts.validate_identifier,
        )
        lakebase_name = prompts.ask(
            "Lakebase instance name",
            default=saved.get("lakebase_instance", f"{_safe_app_name(agent_name)}-db"),
            validate=prompts.validate_app_name,
        )
        print()
        print("  LLM endpoint:")
        print(f"    Default: {DEFAULT_LLM_ENDPOINT}  (FMAPI OSS, works on Free Edition)")
        print("    Other FMAPI endpoints work too. For OpenAI / Anthropic / Bedrock,")
        print(f"    create an external model first: {EXTERNAL_MODELS_DOCS}")
        llm_endpoint = prompts.ask(
            "LLM serving endpoint name",
            default=saved.get("llm_endpoint", DEFAULT_LLM_ENDPOINT),
        )
        heartbeat = int(prompts.ask(
            "Heartbeat seconds (idle reflection cadence)",
            default=str(saved.get("heartbeat_seconds", 120)),
        ))
        token_cap = int(prompts.ask(
            "Daily LLM token cap",
            default=str(saved.get("daily_token_cap", 100000)),
        ))
    else:
        app_name = saved.get("app_name", "living-ai")
        catalog = saved.get("catalog", "workspace")
        schema = saved.get("schema", "living_ai")
        secrets_scope = saved.get("secrets_scope", "living_ai")
        lakebase_name = saved.get("lakebase_instance", f"{_safe_app_name(agent_name)}-db")
        llm_endpoint = saved.get("llm_endpoint", DEFAULT_LLM_ENDPOINT)
        heartbeat = int(saved.get("heartbeat_seconds", 120))
        token_cap = int(saved.get("daily_token_cap", 100000))

    snapshot = {
        "host": host,
        "profile": profile,
        "agent_name": agent_name,
        "app_name": app_name,
        "catalog": catalog,
        "schema": schema,
        "secrets_scope": secrets_scope,
        "lakebase_instance": lakebase_name,
        "llm_endpoint": llm_endpoint,
        "heartbeat_seconds": heartbeat,
        "daily_token_cap": token_cap,
        "telegram_user_handle": user_handle,
    }

    # ===== Plan summary =====
    _section("Summary — about to apply this configuration")
    print(f"  Workspace:        {host}")
    print(f"  Agent name:       {agent_name}")
    print(f"  Telegram user:    @{user_handle}")
    print(f"  LLM endpoint:     {llm_endpoint}")
    if advanced:
        print(f"  Databricks App:   {app_name}")
        print(f"  Catalog/schema:   {catalog}.{schema}")
        print(f"  Lakebase:         {lakebase_name}")
        print(f"  Heartbeat:        every {heartbeat}s   token cap: {token_cap:,}/day")
    else:
        print(f"  Resources:        app={app_name}, lakebase={lakebase_name}, schema={catalog}.{schema}")
        print(f"                    (re-run with --advanced to edit these)")
    print()

    if not prompts.ask_yn("Proceed with installation?", default=True):
        print("Aborted.")
        return

    # ===== Apply =====
    total = 6

    _section(f"[1/{total}] Configure Databricks CLI profile")
    if reuse_creds:
        print(f"  reusing existing profile '{profile}'")
    else:
        configure_profile(host, pat, profile)
        print(f"  wrote profile '{profile}' to ~/.databrickscfg")

    os.environ["DATABRICKS_CONFIG_PROFILE"] = profile
    w = WorkspaceClient(profile=profile)

    _section(f"[2/{total}] Store secrets in Databricks Secrets")
    secret_kvs: dict[str, str] = {"telegram_primary_user_handle": user_handle}
    if rotate_bot and bot_token:
        secret_kvs["telegram_bot_token"] = bot_token
    ensure_secrets(w, secrets_scope, secret_kvs)

    _section(f"[3/{total}] Prepare deployment bundle")
    bundle_dir = Path(tempfile.mkdtemp(prefix="living-ai-bundle-"))
    extract_bundle(bundle_dir)
    substitute_bundle_profile(bundle_dir, profile)
    substitute_app_yaml(bundle_dir, snapshot)
    print(f"  bundle ready")

    var_args = bundle_var_args(snapshot)

    _section(f"[4/{total}] Provision resources (Lakebase, App, schema, volumes)")
    print("  this can take ~3-5 minutes the first time (Lakebase comes up)")
    run_databricks(cli, ["bundle", "deploy", "-t", "free"] + var_args,
                   profile=profile, tf_exec_path=tf, cwd=bundle_dir)

    _section(f"[5/{total}] Start the app and deploy the code")
    run_databricks(cli, ["bundle", "run", "living_ai_app", "-t", "free"] + var_args,
                   profile=profile, tf_exec_path=tf, cwd=bundle_dir)

    _section(f"[6/{total}] Initialize the conversation memory tables")
    run_databricks(cli, ["bundle", "run", "setup_tables", "-t", "free"] + var_args,
                   profile=profile, tf_exec_path=tf, cwd=bundle_dir)

    save_config(snapshot)

    # Resolve the bot username for a clickable t.me link.
    bot_username = ""
    token_for_telegram = bot_token
    if not token_for_telegram:
        try:
            token_for_telegram = read_secret(w, secrets_scope, "telegram_bot_token") or ""
        except Exception:
            token_for_telegram = ""
    if token_for_telegram:
        info = telegram_get_me(token_for_telegram)
        if info:
            bot_username = info.get("username") or ""
        # Clear any registered webhook so long-polling can take over.
        delete_telegram_webhook(token_for_telegram)

    app = w.apps.get(app_name)
    app_url = app.url

    # ===== Final summary =====
    print()
    print("  ┌─────────────────────────────────────────────────────────────────┐")
    print("  │  Your agent is live.                                            │")
    print("  └─────────────────────────────────────────────────────────────────┘")
    print()
    print(f"   Agent:           {agent_name}")
    print(f"   App URL:         {app_url}")
    print(f"   LLM:             {llm_endpoint} (no extra LLM costs on Free Edition)")
    print(f"   Memory:          last 60 exchanges remembered, persisted in Lakebase ({lakebase_name})")
    print(f"   Heartbeat:       every {heartbeat}s; idle reflection at most every 30 min")
    print()
    print(f"   ▶ Start chatting on Telegram:")
    if bot_username:
        print(f"       https://t.me/{bot_username}")
        print(f"     Open that link, hit START, and DM the agent. {agent_name} replies in seconds.")
    else:
        print(f"     Open Telegram and DM your bot. {agent_name} replies in seconds.")
    print()
    print(f"   Manage:")
    print(f"     reconfigure   →  ./living-ai-deploy.pex configure")
    print(f"     advanced      →  ./living-ai-deploy.pex --advanced")
    print(f"     uninstall     →  ./living-ai-deploy.pex uninstall")
    print(f"     show config   →  ./living-ai-deploy.pex --print-config")
    print()
    print(f"   Saved to: {CONFIG_FILE} (no secrets — those are in Databricks)")
    print()


def _safe_app_name(name: str) -> str:
    """Coerce an arbitrary agent name into a valid Lakebase / app name."""
    out = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    return out or "living-ai"


def _validate_pat_live(host: str, pat: str) -> str | None:
    """Return None if the PAT works, or an error string."""
    try:
        w = WorkspaceClient(host=host, token=pat)
        me = w.current_user.me()
        user = getattr(me, "user_name", None) or getattr(me, "display_name", "?")
        print(f"  ✓ token valid; signed in as {user}")
        return None
    except Exception as exc:
        return f"token check failed: {exc}"


# --- uninstall flow ---

def run_uninstall() -> None:
    print("Living-AI agent uninstaller")
    print("===========================\n")

    saved = load_saved_config()
    if not saved:
        print(f"No saved config at {CONFIG_FILE} — nothing to uninstall.")
        print("If you deployed manually, run `databricks bundle destroy` from the bundle dir.")
        sys.exit(1)

    cli = check_prereqs()
    tf = shutil.which("terraform")

    profile = saved["profile"]
    app_name = saved["app_name"]
    secrets_scope = saved["secrets_scope"]
    catalog = saved["catalog"]
    schema = saved["schema"]
    lakebase = saved["lakebase_instance"]

    if not existing_profile(profile):
        print(f"WARNING: CLI profile '{profile}' not found in ~/.databrickscfg.")
        print("Make sure it exists, or remove ~/.living-ai/config.json and re-deploy.")
        sys.exit(1)

    print("Plan:")
    print(f"  Run `databricks bundle destroy` for app '{app_name}'")
    print(f"  Catalog/schema:  {catalog}.{schema} (volumes + tables removed by bundle destroy)")
    print(f"  Lakebase:        {lakebase} — choose below whether to delete the instance too")
    print(f"  Secrets scope:   {secrets_scope}")
    print(f"  Saved config:    {CONFIG_FILE}")
    print()

    confirm_phrase = f"uninstall {app_name}"
    print(f"  Type '{confirm_phrase}' to confirm:")
    try:
        typed = input("  > ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        sys.exit(1)
    if typed != confirm_phrase:
        print("  did not match; aborting.")
        sys.exit(1)

    delete_webhook = prompts.ask_yn("Delete the Telegram webhook?", default=True)
    delete_lakebase = prompts.ask_yn(
        f"Delete the Lakebase instance '{lakebase}'? "
        f"(this also deletes all conversation history)",
        default=True,
    )
    delete_scope = prompts.ask_yn(
        f"Delete the entire secrets scope '{secrets_scope}'?",
        default=False,
    )
    delete_keys_only = False
    if not delete_scope:
        delete_keys_only = prompts.ask_yn(
            "Delete just the keys this agent created (telegram_*) within the scope?",
            default=True,
        )
    drop_profile = prompts.ask_yn(
        f"Remove CLI profile '{profile}' from ~/.databrickscfg?",
        default=False,
    )

    os.environ["DATABRICKS_CONFIG_PROFILE"] = profile
    w = WorkspaceClient(profile=profile)

    if delete_webhook:
        print("\n[1] Deleting Telegram webhook")
        token = read_secret(w, secrets_scope, "telegram_bot_token")
        if token:
            delete_telegram_webhook(token)
        else:
            print("  could not read telegram_bot_token from secrets; skipping")

    print("\n[2] Bundle destroy")
    bundle_dir = Path(tempfile.mkdtemp(prefix="living-ai-bundle-"))
    extract_bundle(bundle_dir)
    substitute_bundle_profile(bundle_dir, profile)
    substitute_app_yaml(bundle_dir, saved)
    var_args = bundle_var_args(saved)
    try:
        run_databricks(
            cli, ["bundle", "destroy", "-t", "free", "--auto-approve"] + var_args,
            profile=profile, tf_exec_path=tf, cwd=bundle_dir,
        )
    except RuntimeError as exc:
        print(f"  bundle destroy reported a failure: {exc}")
        print("  (some resources may have already been removed; continuing)")

    print("\n[2b] Confirming app deletion")
    _wait_for_app_gone(w, app_name)

    if delete_lakebase:
        print(f"\n[3a] Deleting Lakebase instance '{lakebase}'")
        try:
            w.database.delete_database_instance(name=lakebase, purge=True)
            print(f"  deleted Lakebase instance '{lakebase}'")
        except Exception as exc:
            print(f"  could not delete Lakebase instance '{lakebase}': {exc}")

    if delete_scope:
        print("\n[3] Deleting secrets scope")
        delete_secret_scope(w, secrets_scope)
    elif delete_keys_only:
        print("\n[3] Deleting agent secrets (keeping scope)")
        delete_secrets(w, secrets_scope, [
            "telegram_bot_token",
            "telegram_primary_user_handle",
            "telegram_webhook_secret",
        ])
    else:
        print("\n[3] Skipping secrets cleanup (per user choice)")

    print("\n[4] Removing local saved config")
    if remove_saved_config():
        print(f"  removed {CONFIG_FILE}")
    else:
        print("  (no saved config file to remove)")

    if drop_profile:
        print("\n[5] Removing CLI profile")
        if remove_profile(profile):
            print(f"  removed [{profile}] from ~/.databrickscfg")
        else:
            print("  (profile not found)")

    print("\nUninstall complete.")
