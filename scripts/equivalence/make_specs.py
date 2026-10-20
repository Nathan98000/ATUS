#!/usr/bin/env python3
"""Write a randomized spec file for the HTTP equivalence harness.

The specs are the ones ``atus compare-serving-db`` diffs at the engine level
(``atus_pipeline.serving.equivalence``): the fixed extreme cases (zero
variance, near-universal participation pooled over every year, the broad
F66 selectors, pandemic weights, an empty population, an unknown code) plus
``--random`` seeded random specifications over every population dimension,
weight scheme, variance mode and operation. Each becomes an HTTP request
body: the canonical spec the API echoes (``spec``), which the API accepts
as a request, with a comparison's base fields flattened next to its groups.

    python scripts/equivalence/make_specs.py --out DIR [--random 300] [--seed 20261020]

The loaded years and the lexicon are read (read-only) from the full
database: ``$ATUS_EQ_FULL_DATABASE_URL``, default the local development
database. Writes ``DIR/random-<seed>.jsonl``; random specs carry
``"expect": "any"`` (whether the engine refuses one is part of what is
compared, not an assertion), the fixed cases their known outcome.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from pathlib import Path

import psycopg

from atus_pipeline.analytics import ComparisonSpec
from atus_pipeline.analytics.spec import population_to_dict
from atus_pipeline.config import DEFAULT_DATABASE_URL
from atus_pipeline.database.dsn import describe
from atus_pipeline.serving.equivalence import random_specs, seed_specs

FULL_URL_ENV = "ATUS_EQ_FULL_DATABASE_URL"


def request_body(spec) -> dict:
    """The API request for an engine spec (its canonical, echoed form)."""
    if isinstance(spec, ComparisonSpec):
        body = spec.base.to_dict()
        body["group_a"] = population_to_dict(spec.group_a)
        body["group_b"] = population_to_dict(spec.group_b)
        if spec.label_a != "group_a":
            body["label_a"] = spec.label_a
        if spec.label_b != "group_b":
            body["label_b"] = spec.label_b
    else:
        body = spec.to_dict()
    return json.loads(json.dumps(body, default=str))  # dates as ISO strings


def _seed_name(index: int, operation: str, spec) -> tuple[str, str]:
    """(label, expect) of a fixed case: seed_specs() includes two refusals."""
    body = request_body(spec)
    population = body.get("population") or {}
    include = (body.get("activity") or {}).get("include") or []
    if population.get("state_fips") == "99":
        return "empty population", "error"
    if include == ["010198"]:
        return "unknown code", "error"
    return f"{operation} {index}", "ok"


def build(years: list[int], tier1: list[str], leaves: list[str], *, n: int, seed: int
          ) -> list[dict]:
    lines = []
    for i, (operation, spec) in enumerate(seed_specs(years, tier1), 1):
        label, expect = _seed_name(i, operation, spec)
        lines.append({"id": f"seed-{i:02d}", "endpoint": operation, "category": "seed-extreme",
                      "expect": expect, "note": label, "body": request_body(spec)})
    rng = random.Random(seed)
    for i, (operation, spec) in enumerate(
            random_specs(rng, years=years, tier1=tier1, leaves=leaves, n=n), 1):
        lines.append({"id": f"random-{seed}-{i:04d}", "endpoint": operation,
                      "category": f"random-{operation}", "expect": "any",
                      "body": request_body(spec)})
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True, help="Directory to write into.")
    parser.add_argument("--random", type=int, default=300, help="Random specs (default 300).")
    parser.add_argument("--seed", type=int, default=20261020)
    args = parser.parse_args(argv)
    url = os.environ.get(FULL_URL_ENV) or DEFAULT_DATABASE_URL
    try:
        with psycopg.connect(url, autocommit=True, connect_timeout=10,
                             options="-c default_transaction_read_only=on") as conn:
            years = [y for (y,) in conn.execute(
                "SELECT DISTINCT data_year FROM atus.respondents ORDER BY 1")]
            tier1 = [c for (c,) in conn.execute("SELECT code FROM atus.activity_tier1 ORDER BY 1")]
            leaves = [c for (c,) in conn.execute("SELECT code FROM atus.activity_codes ORDER BY 1")]
    except psycopg.Error as exc:
        print(f"make_specs: cannot read {describe(url)}: {type(exc).__name__}", file=sys.stderr)
        return 2
    lines = build(years, tier1, leaves, n=args.random, seed=args.seed)
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"random-{args.seed}.jsonl"
    path.write_text("".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines))
    print(f"{len(lines)} specs -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
