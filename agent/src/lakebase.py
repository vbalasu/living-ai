"""Lakebase Postgres connection helper for the App's service principal.

Uses the Databricks SDK to mint a short-lived OAuth credential and reconnects
when the token expires.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager

import psycopg
from databricks.sdk import WorkspaceClient

log = logging.getLogger(__name__)

CREDENTIAL_TTL_SECONDS = 50 * 60  # rotate before the 1-hour Lakebase token expiry


class Lakebase:
    """Pooled Postgres access for the agent's events / wallet ledger / facts."""

    def __init__(self, instance_name: str, sp_client_id: str | None = None):
        self.instance_name = instance_name
        self.sp_client_id = sp_client_id or os.environ.get("DATABRICKS_CLIENT_ID")
        self._w = WorkspaceClient()
        self._lock = threading.Lock()
        self._instance = None
        self._cred_token: str | None = None
        self._cred_minted_at: float = 0.0
        self._conn: psycopg.Connection | None = None

    def _resolve_instance(self):
        if self._instance is None:
            self._instance = self._w.database.get_database_instance(self.instance_name)
        return self._instance

    def _mint_credential(self) -> str:
        if self._cred_token and (time.time() - self._cred_minted_at) < CREDENTIAL_TTL_SECONDS:
            return self._cred_token
        cred = self._w.database.generate_database_credential(
            request_id=str(uuid.uuid4()),
            instance_names=[self.instance_name],
        )
        self._cred_token = cred.token
        self._cred_minted_at = time.time()
        return self._cred_token

    def _new_connection(self) -> psycopg.Connection:
        instance = self._resolve_instance()
        token = self._mint_credential()
        if not self.sp_client_id:
            raise RuntimeError(
                "Lakebase needs a service-principal client id. "
                "Set DATABRICKS_CLIENT_ID or pass sp_client_id."
            )
        conn = psycopg.connect(
            host=instance.read_write_dns,
            port=5432,
            dbname="databricks_postgres",
            user=self.sp_client_id,
            password=token,
            sslmode="require",
            autocommit=True,
        )
        return conn

    @contextmanager
    def cursor(self):
        with self._lock:
            if self._conn is None or self._conn.closed:
                self._conn = self._new_connection()
            try:
                with self._conn.cursor() as cur:
                    yield cur
            except psycopg.OperationalError:
                log.warning("postgres connection dropped; reconnecting")
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = self._new_connection()
                with self._conn.cursor() as cur:
                    yield cur
