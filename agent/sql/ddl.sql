-- Idempotent DDL for the living AI agent's Delta tables.
-- Executed by the bundle's setup Job after schema + volumes exist.

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.events (
  id        STRING NOT NULL,
  ts        TIMESTAMP NOT NULL,
  ts_date   DATE GENERATED ALWAYS AS (CAST(ts AS DATE)),
  kind      STRING NOT NULL,
  channel   STRING,
  thread_id STRING,
  payload   STRING
)
USING DELTA
PARTITIONED BY (ts_date)
COMMENT 'Append-only event log: stimuli, ticks, tool calls, responses, errors, wallet ops.';

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.wallet_ledger (
  signature    STRING,
  ts           TIMESTAMP NOT NULL,
  direction    STRING NOT NULL,
  counterparty STRING,
  usdc_amount  DECIMAL(18,6),
  sol_amount   DECIMAL(18,9),
  memo         STRING,
  status       STRING NOT NULL,
  network      STRING NOT NULL
)
USING DELTA
COMMENT 'Solana/USDC wallet operations. Daily-cap check is a SUM aggregate over this table.';

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.semantic_facts (
  id         STRING NOT NULL,
  fact       STRING NOT NULL,
  source_ts  TIMESTAMP,
  last_seen  TIMESTAMP NOT NULL,
  confidence DOUBLE
)
USING DELTA
COMMENT 'Distilled semantic memory. Populated by nightly consolidation. Optional Vector Search source.';
