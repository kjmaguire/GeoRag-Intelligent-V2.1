#!/usr/bin/env python
"""Fresh connection + autocommit + SET app.workspace_id; verify chr(0) RLS bypass."""
from __future__ import annotations
import os, psycopg2

def fresh_conn():
    c = psycopg2.connect(
        host=os.environ.get("POSTGRES_DIRECT_HOST", "postgresql"),
        port=int(os.environ.get("POSTGRES_DIRECT_PORT", 5432)),
        user=os.environ.get("POSTGRES_USER", "georag"),
        password=os.environ["POSTGRES_PASSWORD"],
        dbname=os.environ.get("POSTGRES_DB", "georag"),
    )
    c.autocommit = True
    return c

# Attempt 1: SET app.workspace_id then count
c = fresh_conn()
with c.cursor() as cur:
    cur.execute("SET app.workspace_id = 'a0000000-0000-0000-0000-000000000001'")
    try:
        cur.execute("SELECT count(*) FROM silver.workspaces")
        print("count w/ valid GUC:", cur.fetchone())
    except Exception as e:
        print("FAIL valid GUC:", type(e).__name__, str(e)[:200])
c.close()

# Attempt 2: superuser via the migrations role
c2 = psycopg2.connect(
    host=os.environ.get("POSTGRES_DIRECT_HOST", "postgresql"),
    port=int(os.environ.get("POSTGRES_DIRECT_PORT", 5432)),
    user="georag",  # owner role, bypasses RLS
    password=os.environ["POSTGRES_PASSWORD"],
    dbname=os.environ.get("POSTGRES_DB", "georag"),
)
c2.autocommit = True
with c2.cursor() as cur:
    try:
        cur.execute("SELECT count(*) FROM silver.workspaces")
        print("count as owner:", cur.fetchone())
    except Exception as e:
        print("FAIL as owner:", type(e).__name__, str(e)[:200])
c2.close()
