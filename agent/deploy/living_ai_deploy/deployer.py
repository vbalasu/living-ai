"""Living-AI agent deployer.

Self-contained Python entry point that deploys the agent into a target
Databricks workspace. The bundle YAML + source files are packaged inside
this binary; we extract them to a temporary directory and shell out to the
`databricks` CLI for the bundle commands. Secrets are set directly via the
SDK so they never hit a config file or shell history.
"""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
import secrets as secrets_lib
import sys
import tempfile
from pathlib import Path

from databricks.sdk import WorkspaceClient

try:
    from importlib import resources as importlib_resources
except ImportError:  # pragma: no cover
    import importlib_resources  # type: ignore

from . import prompts


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
    """Verify databricks CLI and (optionally) terraform are available."""
    cli = shutil.which("databricks")
    if not cli:
        print("\nERROR: 'databricks' CLI not found on PATH.", file=sys.stderr)
        print("Install: https://docs.databricks.com/dev-tools/cli/install.html", file=sys.stderr)
        sys.exit(2)
    tf = shutil.which("terraform")
    return cli, tf


# --- profile + secrets ---

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
    import json
    try:
        return json.loads(res.stdout)["terraform_version"]
    except Exception:
        for line in res.stdout.splitlines():
            if line.startswith("Terraform v"):
                return line[len("Terraform v"):].strip()
    return "1.5.7"


# --- substitution + telegram webhook ---

def substitute_app_yaml(bundle_dir: Path, agent_name: str, catalog: str,
                        schema: str, fmapi_endpoint: str, secrets_scope: str,
                        lakebase_name: str, heartbeat: int, token_cap: int) -> None:
    """Inject runtime config values into src/app.yaml so the App env reflects user input."""
    app_yaml = bundle_dir / "src" / "app.yaml"
    text = app_yaml.read_text()
    replacements = {
        "April": agent_name,
        "/Volumes/workspace/living_ai/config": f"/Volumes/{catalog}/{schema}/config",
        "/Volumes/workspace/living_ai/workspace_dir": f"/Volumes/{catalog}/{schema}/workspace_dir",
        '"workspace"': f'"{catalog}"',
        '"living_ai"': f'"{schema}"',
        '"databricks-gpt-5-5"': f'"{fmapi_endpoint}"',
        '"april-db"': f'"{lakebase_name}"',
        '"120"': f'"{heartbeat}"',
        '"100000"': f'"{token_cap}"',
    }
    if secrets_scope != "living_ai":
        # only swap the SECRETS_SCOPE value, not the schema name occurrences
        text = text.replace('SECRETS_SCOPE\n    value: "living_ai"',
                            f'SECRETS_SCOPE\n    value: "{secrets_scope}"')
    for old, new in replacements.items():
        text = text.replace(old, new)
    app_yaml.write_text(text)


def configure_telegram_webhook(token: str, app_url: str, secret: str) -> bool:
    import urllib.parse
    import urllib.request
    data = urllib.parse.urlencode({
        "url": f"{app_url}/telegram/webhook",
        "secret_token": secret,
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/setWebhook",
        data=data,
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode()
            print(f"  telegram response: {body[:200]}")
            return '"ok":true' in body
    except Exception as exc:
        print(f"  telegram webhook setup failed: {exc}")
        return False


# --- main ---

def main() -> None:
    print("Living-AI agent deployer\n========================\n")
    print("Provisions the agent into a Databricks workspace using the existing")
    print("Databricks CLI + Asset Bundle packaged inside this binary.")
    print("Free Edition is supported.\n")

    cli, tf = check_prereqs()
    print(f"  databricks CLI:  {cli}")
    print(f"  terraform:       {tf or '(none — bundle deploy may need it)'}")

    print()
    host = prompts.ask("Workspace host (https://...)", validate=prompts.validate_host)
    pat = prompts.ask("Personal access token (PAT)", secret=True, validate=prompts.validate_pat)
    profile = prompts.ask("CLI profile name to write", default="living-ai")
    agent_name = prompts.ask("Agent name", default="April")
    app_name = prompts.ask("Databricks App name", default="living-ai", validate=prompts.validate_app_name)
    catalog = prompts.ask("Catalog", default="workspace", validate=prompts.validate_identifier)
    schema = prompts.ask("Schema", default="living_ai", validate=prompts.validate_identifier)
    secrets_scope = prompts.ask("Secrets scope name", default="living_ai", validate=prompts.validate_identifier)
    lakebase_name = prompts.ask(
        "Lakebase instance name", default=f"{app_name}-db", validate=prompts.validate_app_name
    )
    fmapi_endpoint = prompts.ask("Foundation Model API endpoint", default="databricks-gpt-5-5")
    heartbeat = int(prompts.ask("Heartbeat seconds", default="120"))
    token_cap = int(prompts.ask("Daily LLM token cap", default="100000"))
    bot_token = prompts.ask(
        "Telegram bot token (from @BotFather)", secret=True, validate=prompts.validate_telegram_token
    )
    user_handle = prompts.ask("Primary Telegram user handle (without @)").lstrip("@")
    set_webhook = prompts.ask_yn("Set the Telegram webhook now?", default=True)

    webhook_secret = secrets_lib.token_hex(16)

    print("\n--- Plan ---")
    print(f"  host:           {host}")
    print(f"  profile:        {profile}")
    print(f"  agent / app:    {agent_name} / {app_name}")
    print(f"  catalog/schema: {catalog}.{schema}")
    print(f"  secrets scope:  {secrets_scope}")
    print(f"  lakebase:       {lakebase_name}")
    print(f"  fmapi:          {fmapi_endpoint}")
    print(f"  heartbeat:      {heartbeat}s   token cap: {token_cap}/day")
    print(f"  primary user:   @{user_handle}")
    print(f"  set webhook:    {set_webhook}")

    if not prompts.ask_yn("\nProceed?", default=True):
        print("Aborted.")
        return

    total = 5

    print(f"\n[1/{total}] Configure CLI profile")
    configure_profile(host, pat, profile)
    print(f"  wrote profile '{profile}' to ~/.databrickscfg")

    os.environ["DATABRICKS_CONFIG_PROFILE"] = profile
    w = WorkspaceClient(profile=profile)

    print(f"\n[2/{total}] Set Databricks Secrets")
    ensure_secrets(w, secrets_scope, {
        "telegram_bot_token": bot_token,
        "telegram_primary_user_handle": user_handle,
        "telegram_webhook_secret": webhook_secret,
    })

    print(f"\n[3/{total}] Extract bundle and substitute config")
    bundle_dir = Path(tempfile.mkdtemp(prefix="living-ai-bundle-"))
    extract_bundle(bundle_dir)
    substitute_app_yaml(bundle_dir, agent_name, catalog, schema,
                        fmapi_endpoint, secrets_scope, lakebase_name,
                        heartbeat, token_cap)
    print(f"  bundle ready at {bundle_dir}")

    var_args = [
        "--var", f"agent_name={agent_name}",
        "--var", f"catalog={catalog}",
        "--var", f"schema={schema}",
        "--var", f"app_name={app_name}",
        "--var", f"fmapi_endpoint={fmapi_endpoint}",
        "--var", f"secrets_scope={secrets_scope}",
        "--var", f"lakebase_instance={lakebase_name}",
        "--var", f"heartbeat_seconds={heartbeat}",
        "--var", f"daily_token_cap={token_cap}",
    ]

    print(f"\n[4/{total}] Bundle deploy")
    run_databricks(cli, ["bundle", "deploy", "-t", "free"] + var_args,
                   profile=profile, tf_exec_path=tf, cwd=bundle_dir)

    print(f"\n[5/{total}] Run Lakebase setup job")
    run_databricks(cli, ["bundle", "run", "setup_tables", "-t", "free"] + var_args,
                   profile=profile, tf_exec_path=tf, cwd=bundle_dir)

    app = w.apps.get(app_name)
    app_url = app.url
    print(f"\nApp URL: {app_url}")

    if set_webhook:
        print("\n[+] Setting Telegram webhook")
        ok = configure_telegram_webhook(bot_token, app_url, webhook_secret)
        if ok:
            print("  webhook set ✓")

    print("\n✅ Deployment complete.")
    print(f"   DM your bot to greet {agent_name}.")
    print(f"   Tail logs:   databricks --profile {profile} apps logs {app_name}")
    print(f"   App page:    {app_url}")
