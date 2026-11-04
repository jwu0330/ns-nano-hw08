#!/usr/bin/env python3
"""Query plans and timings for the two core lookups (EXPLAIN ANALYZE).

Compares the location lookup with the n-D GiST index used here, with the
teacher's 2-D GiST index (built inside a transaction that is rolled back, so the
database is left unchanged), and with no index at all.

Usage: python scripts/explain.py --url "$DB_URL" [--repeat 5]
"""

from __future__ import annotations

import argparse
import json
import os
import statistics

import psycopg

LOC = (
    "SELECT DISTINCT study_id FROM ns.coordinates "
    "WHERE ST_3DDWithin(geom, ST_MakePoint(0, -52, 26), 6)"
)
TERM = (
    "SELECT study_id FROM ns.annotations_terms "
    "WHERE term = 'posterior cingulate' AND weight > 0.001"
)


def conninfo(url: str) -> str:
    for scheme in ("postgresql+psycopg://", "postgresql+psycopg2://", "postgres://"):
        if url.startswith(scheme):
            return "postgresql://" + url[len(scheme):]
    return url


def measure(cur, sql: str, repeat: int) -> dict:
    times, plan = [], None
    for _ in range(repeat):
        cur.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + sql)
        plan = cur.fetchone()[0][0]
        times.append(plan["Execution Time"])
    nodes, stack = [], [plan["Plan"]]
    while stack:
        n = stack.pop()
        nodes.append(n["Node Type"] + (f" on {n['Index Name']}" if "Index Name" in n else ""))
        stack.extend(n.get("Plans", []))
    return {"median_ms": round(statistics.median(times), 2), "min_ms": round(min(times), 2), "plan": nodes}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.getenv("DB_URL"))
    ap.add_argument("--repeat", type=int, default=5)
    args = ap.parse_args()

    out = {}
    with psycopg.connect(conninfo(args.url)) as conn, conn.cursor() as cur:
        out["term lookup (btree PK)"] = measure(cur, TERM, args.repeat)
        out["location lookup, n-D GiST (this repo)"] = measure(cur, LOC, args.repeat)

        cur.execute("SET LOCAL enable_indexscan = off; SET LOCAL enable_bitmapscan = off;")
        out["location lookup, no index"] = measure(cur, LOC, args.repeat)
        conn.rollback()

        # Teacher's index type: default 2-D GiST. Built and dropped inside one transaction.
        cur.execute("DROP INDEX ns.coordinates_geom_nd")
        cur.execute("CREATE INDEX tmp_geom_2d ON ns.coordinates USING GIST (geom)")
        cur.execute("ANALYZE ns.coordinates")
        out["location lookup, 2-D GiST (template)"] = measure(cur, LOC, args.repeat)
        conn.rollback()

        cur.execute("SELECT count(*) FROM pg_indexes WHERE indexname = 'coordinates_geom_nd'")
        assert cur.fetchone()[0] == 1, "rollback failed"

    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
