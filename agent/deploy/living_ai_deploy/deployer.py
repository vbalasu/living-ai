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
import secrets as secrets_lib
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

def check_prereqs() -> tuple[str, str | None]:
    cli = shutil.which("databricks")
    if not cli:
        print("\nERROR: 'databricks' CLI not found on PATH.", file=sys.stderr)
        print("Install: https://docs.databricks.com/dev-tools/cli/install.html", file=sys.stderr)
        sys.exit(2)
    tf = shutil.which("terraform")
    return cli, tf


# --- saved config snapshot (no secrets) ---

CONFIG_DIR = Path.home() / ".living-ai"
CONFIG_FILE = CONFIG_DIR / "config.json"


def load_saved_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text())
    except Exception:
        return {}


def save_config(snapshot: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    redacted = {k: v for k, v in snapshot.items() if "token" not in k and "key" not in k}
    CONFIG_FILE.write_text(json.dumps(redacted, indent=2))
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


# --- Telegram webhook ---

def configure_telegram_webhook(token: str, app_url: str, secret: str) -> bool:
    data = urllib.parse.urlencode({
        "url": f"{app_url}/telegram/webhook",
        "secret_token": secret,
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/setWebhook", data=data,
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode()
            print(f"  telegram response: {body[:200]}")
            return '"ok":true' in body
    except Exception as exc:
        print(f"  telegram webhook setup failed: {exc}")
        return False


def delete_telegram_webhook(token: str) -> bool:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/deleteWebhook",
        data=urllib.parse.urlencode({"drop_pending_updates": "true"}).encode(),
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode()
            print(f"  telegram response: {body[:200]}")
            return '"ok":true' in body
    except Exception as exc:
        print(f"  telegram deleteWebhook failed: {exc}")
        return False


# --- CLI parsing ---

def parse_args(argv: list[str]) -> dict:
    args = {"reset": False, "print_config": False, "command": "deploy"}
    for a in argv[1:]:
        if a in ("--reset", "-r"):
            args["reset"] = True
        elif a == "--print-config":
            args["print_config"] = True
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

    run_deploy(reset=args["reset"])


# --- deploy flow ---

def run_deploy(reset: bool) -> None:
    saved = {} if reset else load_saved_config()

    print("Living-AI agent deployer")
    print("========================")
    if saved:
        print(f"\nFound saved config at {CONFIG_FILE}.")
        print("Press ENTER at each prompt to keep the current value.\n")
    else:
        print("\nProvisions the agent into a Databricks workspace using the existing")
        print("Databricks CLI + Asset Bundle packaged inside this binary.")
        print("Free Edition is supported (uses FMAPI OSS endpoints by default).\n")

    cli, tf = check_prereqs()
    print(f"  databricks CLI:  {cli}")
    print(f"  terraform:       {tf or '(none — bundle deploy may need it)'}")
    print()

    # --- Workspace ---
    profile = prompts.ask(
        "CLI profile name",
        default=saved.get("profile", "living-ai"),
    )
    existing_host = existing_profile(profile)
    reuse_creds = False
    if existing_host:
        print(f"  found existing CLI profile '{profile}' (host {existing_host})")
        reuse_creds = prompts.ask_yn("  reuse existing credentials?", default=True)

    if reuse_creds:
        host = existing_host
        pat = ""
    else:
        host = prompts.ask(
            "Workspace host (https://...)",
            default=saved.get("host", existing_host),
            validate=prompts.validate_host,
        )
        pat = prompts.ask(
            "Personal access token (PAT)",
            secret=True,
            validate=prompts.validate_pat,
        )

    # --- Agent identity ---
    agent_name = prompts.ask("Agent name", default=saved.get("agent_name", "April"))
    app_name = prompts.ask(
        "Databricks App name",
        default=saved.get("app_name", "living-ai"),
        validate=prompts.validate_app_name,
    )
    catalog = prompts.ask(
        "Catalog",
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
        default=saved.get("lakebase_instance", f"{app_name}-db"),
        validate=prompts.validate_app_name,
    )

    # --- LLM serving endpoint ---
    print()
    print("  ┌─ LLM serving endpoint ─────────────────────────────────────────────")
    print("  │ The agent calls a Databricks serving endpoint to think.")
    print("  │")
    print(f"  │ Default: {DEFAULT_LLM_ENDPOINT}")
    print("  │   (FMAPI OSS — works on Free Edition with no extra setup)")
    print("  │")
    print("  │ Other FMAPI endpoints in your workspace also work, e.g.")
    print("  │   databricks-meta-llama-3-1-8b-instruct, databricks-gpt-5-5, ...")
    print("  │")
    print("  │ To use OpenAI / Anthropic / Bedrock with your own provider key,")
    print("  │ create an *external model* serving endpoint first, then enter")
    print("  │ that endpoint's name here. Setup instructions:")
    print(f"  │   {EXTERNAL_MODELS_DOCS}")
    print("  └────────────────────────────────────────────────────────────────────")
    llm_endpoint = prompts.ask(
        "Serving endpoint name",
        default=saved.get("llm_endpoint", DEFAULT_LLM_ENDPOINT),
    )

    heartbeat = int(prompts.ask("Heartbeat seconds", default=str(saved.get("heartbeat_seconds", 120))))
    token_cap = int(prompts.ask("Daily LLM token cap", default=str(saved.get("daily_token_cap", 100000))))

    # --- Telegram channel ---
    print()
    print("  Telegram is the primary channel. Create a bot via @BotFather to get a token.")

    bot_token = ""
    skip_telegram = False
    if saved:
        skip_telegram = not prompts.ask_yn(
            "Update Telegram bot token / handle?", default=False
        )
    if not skip_telegram:
        bot_token = prompts.ask(
            "Telegram bot token (from @BotFather)",
            secret=True,
            validate=prompts.validate_telegram_token,
        )
    user_handle = prompts.ask(
        "Primary Telegram user handle (without @)",
        default=saved.get("telegram_user_handle"),
    ).lstrip("@")
    set_webhook = (not skip_telegram) and prompts.ask_yn(
        "Set the Telegram webhook now?", default=True
    )

    webhook_secret = secrets_lib.token_hex(16)

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

    print("\n--- Plan ---")
    print(f"  host:           {host}")
    print(f"  profile:        {profile}")
    print(f"  agent / app:    {agent_name} / {app_name}")
    print(f"  catalog/schema: {catalog}.{schema}")
    print(f"  secrets scope:  {secrets_scope}")
    print(f"  lakebase:       {lakebase_name}")
    print(f"  llm endpoint:   {llm_endpoint}")
    print(f"  heartbeat:      {heartbeat}s   token cap: {token_cap}/day")
    print(f"  primary user:   @{user_handle}")
    print(f"  set webhook:    {set_webhook}")

    if not prompts.ask_yn("\nProceed?", default=True):
        print("Aborted.")
        return

    total = 6

    print(f"\n[1/{total}] Configure CLI profile")
    if reuse_creds:
        print(f"  reusing existing profile '{profile}' from ~/.databrickscfg")
    else:
        configure_profile(host, pat, profile)
        print(f"  wrote profile '{profile}' to ~/.databrickscfg")

    os.environ["DATABRICKS_CONFIG_PROFILE"] = profile
    w = WorkspaceClient(profile=profile)

    print(f"\n[2/{total}] Set Databricks Secrets")
    secret_kvs: dict[str, str] = {}
    if not skip_telegram:
        secret_kvs.update({
            "telegram_bot_token": bot_token,
            "telegram_primary_user_handle": user_handle,
            "telegram_webhook_secret": webhook_secret,
        })
    if not secret_kvs:
        print("  no secrets to update")
    else:
        ensure_secrets(w, secrets_scope, secret_kvs)

    print(f"\n[3/{total}] Extract bundle and substitute config")
    bundle_dir = Path(tempfile.mkdtemp(prefix="living-ai-bundle-"))
    extract_bundle(bundle_dir)
    substitute_app_yaml(bundle_dir, snapshot)
    print(f"  bundle ready at {bundle_dir}")

    var_args = bundle_var_args(snapshot)

    print(f"\n[4/{total}] Bundle deploy")
    run_databricks(cli, ["bundle", "deploy", "-t", "free"] + var_args,
                   profile=profile, tf_exec_path=tf, cwd=bundle_dir)

    print(f"\n[5/{total}] Start app + deploy code")
    run_databricks(cli, ["bundle", "run", "living_ai_app", "-t", "free"] + var_args,
                   profile=profile, tf_exec_path=tf, cwd=bundle_dir)

    print(f"\n[6/{total}] Run Lakebase setup job")
    run_databricks(cli, ["bundle", "run", "setup_tables", "-t", "free"] + var_args,
                   profile=profile, tf_exec_path=tf, cwd=bundle_dir)

    save_config(snapshot)
    print(f"\n  saved config snapshot to {CONFIG_FILE}")

    app = w.apps.get(app_name)
    app_url = app.url
    print(f"\nApp URL: {app_url}")

    if set_webhook:
        print("\n[+] Setting Telegram webhook")
        if configure_telegram_webhook(bot_token, app_url, webhook_secret):
            print("  webhook set")

    print("\nDeployment complete.")
    print(f"   DM your bot to greet {agent_name}.")
    print(f"   Tail logs:    databricks --profile {profile} apps logs {app_name}")
    print(f"   Reconfigure:  ./living-ai-deploy.pex configure")
    print(f"   Uninstall:    ./living-ai-deploy.pex uninstall")
    print(f"   App page:     {app_url}")


# --- uninstall flow ---

def run_uninstall() -> None:
    print("Living-AI agent uninstaller")
    print("===========================\n")

    saved = load_saved_config()
    if not saved:
        print(f"No saved config at {CONFIG_FILE} — nothing to uninstall.")
        print("If you deployed manually, run `databricks bundle destroy` from the bundle dir.")
        sys.exit(1)

    cli, tf = check_prereqs()

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
    print(f"  Lakebase:        {lakebase} (removed by bundle destroy)")
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
