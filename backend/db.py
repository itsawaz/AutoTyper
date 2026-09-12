"""Turso / libSQL data-access layer.

All persistent data lives in Turso. This module is the ONLY place that holds
the Turso auth token, and it only ever runs on the backend (never shipped in
the desktop app).

Schema
------
users
    id            TEXT PRIMARY KEY         -- uuid4
    email         TEXT UNIQUE NOT NULL
    password_hash TEXT NOT NULL
    balance_secs  REAL NOT NULL DEFAULT 0  -- remaining paid time, in seconds
    created_at    TEXT NOT NULL

sessions
    id            TEXT PRIMARY KEY         -- uuid4
    user_id       TEXT NOT NULL
    start_time    TEXT NOT NULL
    end_time      TEXT
    duration_secs REAL                     -- active seconds consumed
    status        TEXT NOT NULL            -- 'active' | 'closed'

orders
    id            TEXT PRIMARY KEY         -- our order id (uuid4)
    user_id       TEXT NOT NULL
    hours         REAL NOT NULL            -- hours purchased
    amount_paise  INTEGER NOT NULL
    currency      TEXT NOT NULL
    status        TEXT NOT NULL            -- 'created' | 'paid' | 'expired'
    provider      TEXT NOT NULL            -- 'upi_gmail' | 'mock'
    provider_ref  TEXT                     -- provider order ref
    created_at    TEXT NOT NULL
    paid_at       TEXT
"""
from __future__ import annotations

import libsql_client

from config import get_settings

_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS users (
        id            TEXT PRIMARY KEY,
        email         TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        balance_secs  REAL NOT NULL DEFAULT 0,
        created_at    TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id            TEXT PRIMARY KEY,
        user_id       TEXT NOT NULL,
        start_time    TEXT NOT NULL,
        end_time      TEXT,
        duration_secs REAL,
        status        TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS orders (
        id            TEXT PRIMARY KEY,
        user_id       TEXT NOT NULL,
        hours         REAL NOT NULL,
        amount_paise  INTEGER NOT NULL,
        currency      TEXT NOT NULL,
        status        TEXT NOT NULL,
        provider      TEXT NOT NULL,
        provider_ref  TEXT,
        created_at    TEXT NOT NULL,
        paid_at       TEXT,
        expires_at    TEXT,          -- UPI: when a pending order stops matching
        matched_ref   TEXT           -- UPI: bank ref/UTR that paid this order
    )
    """,
    # Idempotency guard: a bank alert (unique amount) can match only one order.
    "CREATE INDEX IF NOT EXISTS idx_orders_amount_status ON orders(amount_paise, status)",
    """
    CREATE TABLE IF NOT EXISTS payment_events (
        id            TEXT PRIMARY KEY,      -- uuid4
        created_at    TEXT NOT NULL,
        source        TEXT NOT NULL,         -- 'gmail_poll' | 'gmail_check' | 'upi_gmail' | 'mock'
        outcome       TEXT NOT NULL,         -- 'matched' | 'unmatched' | 'duplicate' | 'error'
        amount_paise  INTEGER,               -- amount seen in the alert
        bank_ref      TEXT,                  -- UPI reference / UTR
        order_id      TEXT,                  -- order it matched (if any)
        user_id       TEXT,                  -- owner of the matched order (if any)
        subject       TEXT,                  -- email subject (audit)
        snippet       TEXT,                  -- email snippet (audit)
        detail        TEXT                   -- freeform note / error text
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_events_ref ON payment_events(bank_ref)",
    "CREATE INDEX IF NOT EXISTS idx_events_created ON payment_events(created_at)",
]


def _client() -> libsql_client.Client:
    """Create a libSQL client.

    Turso URLs use the libsql:// scheme; the sync HTTP client needs https://.
    """
    settings = get_settings()
    url = settings.turso_database_url
    if url.startswith("libsql://"):
        url = "https://" + url[len("libsql://"):]
    return libsql_client.create_client_sync(
        url=url,
        auth_token=settings.turso_auth_token,
    )


# Columns added after the first release; applied idempotently on init.
_MIGRATIONS = [
    ("orders", "expires_at", "ALTER TABLE orders ADD COLUMN expires_at TEXT"),
    ("orders", "matched_ref", "ALTER TABLE orders ADD COLUMN matched_ref TEXT"),
]


def init_db() -> None:
    """Create tables if they don't exist and apply column migrations. Safe to
    call on every cold start."""
    with _client() as client:
        for stmt in _SCHEMA:
            client.execute(stmt)
        # Add columns that may be missing on an already-created DB.
        for table, column, alter_sql in _MIGRATIONS:
            if not _column_exists(client, table, column):
                client.execute(alter_sql)


def _column_exists(client, table: str, column: str) -> bool:
    rs = client.execute(f"PRAGMA table_info({table})")
    # PRAGMA table_info columns: cid, name, type, notnull, dflt_value, pk
    names = {row[1] for row in rs.rows}
    return column in names


def execute(sql: str, params: tuple | list | None = None):
    """Run a single statement and return the ResultSet."""
    with _client() as client:
        return client.execute(sql, params or [])


def query_one(sql: str, params: tuple | list | None = None) -> dict | None:
    """Return the first row as a dict, or None."""
    rs = execute(sql, params)
    if not rs.rows:
        return None
    return _row_to_dict(rs.columns, rs.rows[0])


def query_all(sql: str, params: tuple | list | None = None) -> list[dict]:
    """Return all rows as a list of dicts."""
    rs = execute(sql, params)
    return [_row_to_dict(rs.columns, row) for row in rs.rows]


def _row_to_dict(columns, row) -> dict:
    return {col: row[i] for i, col in enumerate(columns)}
