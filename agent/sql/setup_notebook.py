# Databricks notebook source
# Bundle setup notebook: Lakebase Postgres DDL for the living AI agent.
# Idempotent — re-run safe. Triggered by `databricks bundle run setup_tables`.

# COMMAND ----------

# MAGIC %pip install psycopg[binary]==3.2.3
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

import uuid
import psycopg
from databricks.sdk import WorkspaceClient

dbutils.widgets.text("instance_name", "april-db")
dbutils.widgets.text("app_sp_client_id", "")

instance_name = dbutils.widgets.get("instance_name")
app_sp_client_id = dbutils.widgets.get("app_sp_client_id")

w = WorkspaceClient()

instance = w.database.get_database_instance(instance_name)
host = instance.read_write_dns
print(f"Lakebase host: {host}")

current_user = spark.sql("SELECT current_user()").collect()[0][0]
print(f"Connecting as: {current_user}")

cred = w.database.generate_database_credential(
    request_id=str(uuid.uuid4()),
    instance_names=[instance_name],
)

conn = psycopg.connect(
    host=host,
    port=5432,
    dbname="databricks_postgres",
    user=current_user,
    password=cred.token,
    sslmode="require",
)
conn.autocommit = True

DDL = [
    "CREATE EXTENSION IF NOT EXISTS databricks_auth",
    """
    CREATE TABLE IF NOT EXISTS events (
        id          UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
        ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
        kind        TEXT NOT NULL,
        channel     TEXT,
        thread_id   TEXT,
        payload     JSONB
    )
    """,
    "CREATE INDEX IF NOT EXISTS events_ts_idx ON events (ts DESC)",
    "CREATE INDEX IF NOT EXISTS events_thread_idx ON events (thread_id, ts DESC)",
    """
    CREATE TABLE IF NOT EXISTS wallet_ledger (
        id            UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
        signature     TEXT,
        ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
        direction     TEXT NOT NULL CHECK (direction IN ('in', 'out')),
        counterparty  TEXT,
        usdc_amount   NUMERIC(18, 6),
        sol_amount    NUMERIC(18, 9),
        memo          TEXT,
        status        TEXT NOT NULL,
        network       TEXT NOT NULL CHECK (network IN ('devnet', 'mainnet'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS wallet_ts_idx ON wallet_ledger (ts DESC)",
    """
    CREATE TABLE IF NOT EXISTS semantic_facts (
        id          UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
        fact        TEXT NOT NULL,
        source_ts   TIMESTAMPTZ,
        last_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
        confidence  DOUBLE PRECISION
    )
    """,
]

with conn.cursor() as cur:
    for stmt in DDL:
        cur.execute(stmt)
    print("Tables created.")

if app_sp_client_id:
    with conn.cursor() as cur:
        try:
            cur.execute(
                "SELECT databricks_create_role(%s, 'SERVICE_PRINCIPAL')",
                (app_sp_client_id,),
            )
            print(f"Created Postgres role for SP {app_sp_client_id}")
        except Exception as exc:
            if "already exists" in str(exc).lower():
                print(f"Role {app_sp_client_id} already exists.")
            else:
                print(f"Role create skipped/failed: {exc}")

        grant_stmts = [
            f'GRANT CONNECT ON DATABASE databricks_postgres TO "{app_sp_client_id}"',
            f'GRANT USAGE ON SCHEMA public TO "{app_sp_client_id}"',
            f'GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO "{app_sp_client_id}"',
            f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "{app_sp_client_id}"',
            f'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE ON TABLES TO "{app_sp_client_id}"',
        ]
        for stmt in grant_stmts:
            try:
                cur.execute(stmt)
            except Exception as exc:
                print(f"Grant failed: {stmt} -> {exc}")
        print(f"Granted privileges to {app_sp_client_id}.")

conn.close()
print("Setup complete.")
