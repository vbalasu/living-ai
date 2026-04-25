"""Runtime configuration loaded from env vars set by the Databricks App."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    agent_name: str
    catalog: str
    schema: str
    config_volume_path: str
    workspace_volume_path: str
    fmapi_endpoint: str
    heartbeat_seconds: int
    daily_token_cap: int
    secrets_scope: str
    lakebase_instance: str | None
    telegram_token_secret_key: str = "telegram_bot_token"
    telegram_user_handle_secret_key: str = "telegram_primary_user_handle"


def load() -> Config:
    return Config(
        agent_name=os.environ.get("AGENT_NAME", "April"),
        catalog=os.environ["CATALOG"],
        schema=os.environ["SCHEMA"],
        config_volume_path=os.environ["CONFIG_VOLUME_PATH"],
        workspace_volume_path=os.environ["WORKSPACE_VOLUME_PATH"],
        fmapi_endpoint=os.environ.get("FMAPI_ENDPOINT", "databricks-gpt-5-5"),
        heartbeat_seconds=int(os.environ.get("HEARTBEAT_SECONDS", "120")),
        daily_token_cap=int(os.environ.get("DAILY_TOKEN_CAP", "100000")),
        secrets_scope=os.environ.get("SECRETS_SCOPE", "living_ai"),
        lakebase_instance=os.environ.get("LAKEBASE_INSTANCE") or None,
    )
