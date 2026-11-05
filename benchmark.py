#!/usr/bin/env python3
"""Performance measurements behind the report.

  python benchmark.py db   --url "$DB_URL"            query plans and timings (EXPLAIN ANALYZE)
  python benchmark.py http --base https://...          end-to-end latency of a running server

``db`` compares the location lookup with the n-D GiST index used here, with the
template's 2-D GiST index (built inside a transaction that is rolled back, so the
database is left unchanged), and with no index at all.

``http`` times new queries (random coordinates or term pairs, so no cache can
answer them) and the repeated DMN examples; it reports the client-side total and
the server's own time (Server-Timing header).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import statistics
import time

import httpx
import psycopg

# ---------------------------------------------------------------- database (EXPLAIN ANALYZE)
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


def run_db(args) -> None:

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


# ---------------------------------------------------------------- HTTP latency
TERMS = ["memory", "attention", "reward", "pain", "emotion", "language", "motor", "visual",
         "fear", "working memory", "decision making", "social", "face", "auditory", "default mode"]


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def _timed(client: httpx.Client, paths: list[str]) -> dict:
    total, server = [], []
    for path in paths:
        t0 = time.perf_counter()
        r = client.get(path)
        total.append((time.perf_counter() - t0) * 1000)
        r.raise_for_status()
        m = re.search(r"dur=([\d.]+)", r.headers.get("server-timing", ""))
        if m:
            server.append(float(m.group(1)))
    row = {"n": len(paths), "total_p50_ms": round(statistics.median(total)), "total_p95_ms": round(pct(total, 95))}
    if server:
        row |= {"server_p50_ms": round(statistics.median(server), 1), "server_p95_ms": round(pct(server, 95), 1)}
    return row


def run_http(args) -> None:
    rnd = random.Random(args.seed)

    def xyz() -> str:
        return "_".join(str(rnd.randrange(-60, 61, 2)) for _ in range(3))

    with httpx.Client(base_url=args.base, timeout=120) as c:
        t0 = time.perf_counter()
        c.get("/health").raise_for_status()
        first = round((time.perf_counter() - t0) * 1000)
        pairs = [rnd.sample(TERMS, 2) for _ in range(args.n)]
        dmn_loc = "/dissociate/locations/0_-52_26/-2_50_-6?both=true"
        dmn_term = "/dissociate/terms/posterior_cingulate/ventromedial_prefrontal?both=true"
        out = {
            "first /health (includes wake-up if asleep)": first,
            "locations, uncached": _timed(c, [f"/dissociate/locations/{xyz()}/{xyz()}?both=true" for _ in range(args.n)]),
            "terms, uncached": _timed(c, [f"/dissociate/terms/{a}/{b}?both=true" for a, b in pairs]),
            "locations DMN, cached": _timed(c, [dmn_loc] * args.n),
            "terms DMN, cached": _timed(c, [dmn_term] * args.n),
            "terms DMN + analyze": _timed(c, [dmn_term + f"&analyze=true&threshold=0.00{i % 9 + 1}" for i in range(5)]),
        }
    print(json.dumps(out, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("db", help="EXPLAIN ANALYZE timings")
    d.add_argument("--url", default=os.getenv("DB_URL"))
    d.add_argument("--repeat", type=int, default=5)
    h = sub.add_parser("http", help="end-to-end latency")
    h.add_argument("--base", required=True)
    h.add_argument("--n", type=int, default=20)
    h.add_argument("--seed", type=int, default=8)
    args = ap.parse_args()
    (run_db if args.cmd == "db" else run_http)(args)


if __name__ == "__main__":
    main()
