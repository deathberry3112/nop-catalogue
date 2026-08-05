#!/usr/bin/env python3
"""Run SQL against the Supabase database from the command line.

    python sql.py "select count(*) from runs"      one statement
    python sql.py app/store/schema.sql             a file
    python sql.py < some.sql                       stdin

This exists because PostgREST — what the application talks to — cannot run
DDL. `CREATE TABLE` has no verb in its HTTP vocabulary, so anything structural
has to go over the Postgres wire protocol instead, which needs the database
password rather than the API key.

Reads SUPABASE_DB_URL from .env (gitignored). Get it from
Supabase → Project Settings → Database → Connection string → URI:

    postgresql://postgres:<password>@db.<ref>.supabase.co:5432/postgres

EVERYTHING RUNS IN ONE TRANSACTION and commits only if the whole input
succeeded. A schema file that fails on statement nine leaves nothing behind
rather than eight tables and a mess — Postgres makes DDL transactional, so
this costs nothing and is worth having on a database somebody's catalogue
ownership records live in.
"""

from __future__ import annotations

import sys
from pathlib import Path

from app.config import ROOT, settings   # loads .env as a side effect


def _connection_string() -> str:
    url = settings().supabase_db_url
    if not url:
        sys.exit(
            "SUPABASE_DB_URL is not set.\n"
            "Add it to .env — Supabase → Project Settings → Database →\n"
            "Connection string → URI, which looks like:\n"
            "  SUPABASE_DB_URL=postgresql://postgres:<password>"
            "@db.<ref>.supabase.co:5432/postgres")
    return url


def _read_sql(argv: list[str]) -> str:
    if not argv:
        if sys.stdin.isatty():
            sys.exit(__doc__.strip().split("\n\n")[1])   # the usage block
        return sys.stdin.read()

    candidate = Path(argv[0])
    if not candidate.is_absolute():
        candidate = ROOT / argv[0]
    # A path wins over a literal query only if it actually exists — otherwise a
    # typo'd filename would be sent to the server as SQL and fail obscurely.
    return candidate.read_text(encoding="utf-8") if candidate.is_file() else argv[0]


def _print_table(columns: list[str], rows: list[tuple]) -> None:
    if not rows:
        print("(0 rows)")
        return
    text = [[("" if v is None else str(v)) for v in row] for row in rows]
    widths = [max(len(c), *(len(r[i]) for r in text))
              for i, c in enumerate(columns)]
    line = "  ".join(c.ljust(w) for c, w in zip(columns, widths))
    print(line)
    print("  ".join("-" * w for w in widths))
    for row in text:
        print("  ".join(v.ljust(w) for v, w in zip(row, widths)))
    print(f"({len(rows)} row{'s' if len(rows) != 1 else ''})")


def main() -> None:
    try:
        import psycopg2
    except ImportError:
        sys.exit("psycopg2 is not installed — run:\n"
                 "  python -m pip install psycopg2-binary")

    sql = _read_sql(sys.argv[1:])
    if not sql.strip():
        sys.exit("nothing to run")

    conn = psycopg2.connect(_connection_string(), connect_timeout=15)
    try:
        with conn:                       # commits on success, rolls back on error
            with conn.cursor() as cur:
                cur.execute(sql)
                if cur.description:      # a SELECT, or anything RETURNING
                    _print_table([d.name for d in cur.description], cur.fetchall())
                else:
                    print(f"OK ({cur.rowcount} row(s) affected)"
                          if cur.rowcount and cur.rowcount > 0 else "OK")
    except Exception as exc:
        sys.exit(f"SQL failed (nothing was committed): {exc}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
