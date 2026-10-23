#!/usr/bin/env python3
"""Wait until a timed load-test run may start, and record the evidence.

The measurement precondition (ltcommon.precondition_snapshot): no process
matching the equivalence replay, pytest, Playwright, another k6 or wrangler;
no atus-* container besides the four protected databases (atus-postgres,
atus-wsb-drill, atus-wsb-build, atus-emu-pg) and this run's own atus-lt-*
containers; and those four databases idle (no active query but ours).

Polls every --poll-s seconds (default 60) for up to --max-wait-s (default
3 h), then writes every snapshot taken (the last one is the verdict) to --out
as JSON: matching processes (pid, age, a truncated command line), docker ps,
one docker stats sample of every container (other projects' idle services are
background noise, recorded, not disqualifying), pg_stat_activity of the
protected databases by state (no query text), and the host load average.

  python3 loadtests/precondition.py --out <scratch>/precondition.json
Exit status 0 when the precondition holds, 3 when it never did.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ltcommon as lt  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--poll-s", type=float, default=60.0)
    parser.add_argument("--max-wait-s", type=float, default=3 * 3600.0)
    parser.add_argument("--own", action="append", default=[],
                        help="a container name that belongs to this run")
    args = parser.parse_args(argv)
    deadline = time.time() + args.max_wait_s
    snapshots = []
    while True:
        snap = lt.precondition_snapshot(set(args.own))
        snapshots.append(snap)
        print(f"{snap['checked_at']} ok={snap['ok']} load={snap['load_average_1_5_15']} "
              f"{'; '.join(snap['reasons'])}", flush=True)
        if snap["ok"] or time.time() + args.poll_s > deadline:
            break
        time.sleep(args.poll_s)
    lt.write_json(args.out, {"ok": snapshots[-1]["ok"], "polls": len(snapshots),
                             "waited_s": round(time.time() - (deadline - args.max_wait_s), 1),
                             "snapshots": snapshots})
    return 0 if snapshots[-1]["ok"] else 3


if __name__ == "__main__":
    sys.exit(main())
