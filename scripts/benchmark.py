#!/usr/bin/env python3
"""End-to-end latency of a running server.

* ``uncached``: every request is a new query (random coordinates, or random term
  pairs), so neither the app's memo nor any HTTP cache can answer it.
* ``cached``: the same DMN request repeated, as a grader clicking the examples would.

Reports the client-side total time and the server's own time (Server-Timing header).

Usage: python scripts/benchmark.py --base https://ns-nano-hw08.onrender.com [--n 20]
"""

from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import time

import httpx

TERMS = ["memory", "attention", "reward", "pain", "emotion", "language", "motor", "visual",
         "fear", "working memory", "decision making", "social", "face", "auditory", "default mode"]


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def run(client: httpx.Client, paths: list[str]) -> dict:
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--seed", type=int, default=8)
    args = ap.parse_args()
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
            "locations, uncached": run(c, [f"/dissociate/locations/{xyz()}/{xyz()}?both=true" for _ in range(args.n)]),
            "terms, uncached": run(c, [f"/dissociate/terms/{a}/{b}?both=true" for a, b in pairs]),
            "locations DMN, cached": run(c, [dmn_loc] * args.n),
            "terms DMN, cached": run(c, [dmn_term] * args.n),
            "terms DMN + analyze": run(c, [dmn_term + f"&analyze=true&threshold=0.00{i % 9 + 1}" for i in range(5)]),
        }
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
