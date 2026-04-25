# Databricks notebook source
# Bundle setup notebook: idempotent DDL for the living AI agent's Delta tables.
# Run automatically after `databricks bundle deploy` via the setup Job.

dbutils.widgets.text("catalog", "workspace")
dbutils.widgets.text("schema", "living_ai")

catalog = dbutils.widgets.get("catalog")
schema = dbutils.widgets.get("schema")

spark.sql(f"USE CATALOG {catalog}")
spark.sql(f"USE SCHEMA {schema}")

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {catalog}.{schema}.events (
  id STRING NOT NULL,
  ts TIMESTAMP NOT NULL,
  ts_date DATE GENERATED ALWAYS AS (CAST(ts AS DATE)),
  kind STRING NOT NULL,
  channel STRING,
  thread_id STRING,
  payload STRING
)
USING DELTA
PARTITIONED BY (ts_date)
COMMENT 'Append-only event log: stimuli, ticks, tool calls, responses, errors, wallet ops.'
""")

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {catalog}.{schema}.wallet_ledger (
  signature STRING,
  ts TIMESTAMP NOT NULL,
  direction STRING NOT NULL,
  counterparty STRING,
  usdc_amount DECIMAL(18,6),
  sol_amount DECIMAL(18,9),
  memo STRING,
  status STRING NOT NULL,
  network STRING NOT NULL
)
USING DELTA
COMMENT 'Solana/USDC wallet operations. Daily-cap check is a SUM aggregate over this table.'
""")

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {catalog}.{schema}.semantic_facts (
  id STRING NOT NULL,
  fact STRING NOT NULL,
  source_ts TIMESTAMP,
  last_seen TIMESTAMP NOT NULL,
  confidence DOUBLE
)
USING DELTA
COMMENT 'Distilled semantic memory. Populated by nightly consolidation.'
""")

print(f"Tables created in {catalog}.{schema}: events, wallet_ledger, semantic_facts")
