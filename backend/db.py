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
    status        TEXT NOT NULL            -- 'created' | 'paid' | 'failed'
    provider      TEXT NOT NULL            -- 'mock' | 'juspay'
    provider_ref  TEXT                     -- gateway order/txn id
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
        paid_at       TEXT
    )
    """,
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


def init_db() -> None:
    """Create tables if they don't exist. Safe to call on every cold start."""
    with _client() as client:
        for stmt in _SCHEMA:
            client.execute(stmt)


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
