"""Neon compute settings for the emulation profile, from Neon's published formulas.

Neon derives a compute's Postgres settings from its autoscaling range, not from
its current size (https://neon.com/docs/reference/compatibility, read
2026-10-20; also cited in docs/evidence/phase5/01-audit-timed.md):

    compute_size         = min(max_compute_size, 8 * min_compute_size)
    max_connections      = max(100, min(4000, floor(compute_size * 419.66)))
    max_worker_processes = 13 + floor(2 * max_compute_size)
    backends             = 1 + max_connections + max_worker_processes
    shared_buffers_mb    = max(128, (1023 + backends * 256) / 1024)
    max_parallel_workers = 8

(Large fixed computes of 18 CU and more use 75% of RAM instead; Free stops at
2 CU.) One CU is 1 vCPU and 4 GB of RAM (https://neon.com/autoscaling-report),
which gives the container limits for the size being emulated.

    python deploy/emulation/neon_settings.py                 # 0.25-2 CU, at 0.25 CU
    python deploy/emulation/neon_settings.py --at-cu 2       # the same compute at 2 CU
    python deploy/emulation/neon_settings.py --min-cu 0.25 --max-cu 0.25

The output is env-file lines for docker-compose.prod.yml (database settings)
and docker-compose.emulation.yml (CPU/RAM limits). The compose defaults are the
0.25-2 CU compute ADR-011 chose, measured at its 0.25 CU floor;
tests/deploy/test_compose_config.py checks they match this formula.
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass

GB_PER_CU = 4
MAX_PARALLEL_WORKERS = 8


@dataclass(frozen=True)
class NeonSettings:
    max_connections: int
    max_worker_processes: int
    shared_buffers_mb: int
    max_parallel_workers: int = MAX_PARALLEL_WORKERS


def neon_settings(min_cu: float, max_cu: float) -> NeonSettings:
    """Postgres settings Neon applies to a compute with this autoscaling range."""
    if not 0 < min_cu <= max_cu:
        raise ValueError(f"need 0 < min_cu <= max_cu, got {min_cu}..{max_cu}")
    compute_size = min(max_cu, 8 * min_cu)
    max_connections = max(100, min(4000, math.floor(compute_size * 419.66)))
    max_worker_processes = 13 + math.floor(2 * max_cu)
    backends = 1 + max_connections + max_worker_processes
    shared_buffers_mb = max(128, (1023 + backends * 256) // 1024)
    return NeonSettings(max_connections, max_worker_processes, shared_buffers_mb)


def container_limits(at_cu: float) -> tuple[str, str]:
    """(cpus, memory) docker limits for a compute running at ``at_cu``."""
    memory_mb = round(at_cu * GB_PER_CU * 1024)
    memory = f"{memory_mb // 1024}g" if memory_mb % 1024 == 0 else f"{memory_mb}m"
    return f"{at_cu:g}", memory


def env_lines(min_cu: float, max_cu: float, at_cu: float) -> list[str]:
    if not min_cu <= at_cu <= max_cu:
        raise ValueError(f"at_cu {at_cu} is outside {min_cu}..{max_cu}")
    settings = neon_settings(min_cu, max_cu)
    cpus, memory = container_limits(at_cu)
    return [
        f"ATUS_STACK_DB_SHARED_BUFFERS={settings.shared_buffers_mb}MB",
        f"ATUS_STACK_DB_MAX_CONNECTIONS={settings.max_connections}",
        f"ATUS_STACK_DB_MAX_WORKER_PROCESSES={settings.max_worker_processes}",
        f"ATUS_EMU_DB_CPUS={cpus}",
        f"ATUS_EMU_DB_MEMORY={memory}",
        f"PGBOUNCER_DEFAULT_POOL_SIZE={math.floor(0.9 * settings.max_connections)}",
    ]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--min-cu", type=float, default=0.25)
    parser.add_argument("--max-cu", type=float, default=2.0)
    parser.add_argument("--at-cu", type=float, default=None,
                        help="compute size to emulate (default: --min-cu)")
    args = parser.parse_args(argv)
    at_cu = args.min_cu if args.at_cu is None else args.at_cu
    print("\n".join(env_lines(args.min_cu, args.max_cu, at_cu)))


if __name__ == "__main__":
    main()
