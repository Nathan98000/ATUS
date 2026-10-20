"""Neon's published compute-setting formulas (deploy/emulation/neon_settings.py).

The fixed-size values are the ones Neon's compute table lists (0.25 CU: 104
connections; 2 CU: 839), which pins the formula to the documentation.
"""

from __future__ import annotations

import importlib.util
import sys

import pytest

from .conftest import REPO_ROOT


@pytest.fixture(scope="module")
def neon():
    spec = importlib.util.spec_from_file_location(
        "neon_settings", REPO_ROOT / "deploy" / "emulation" / "neon_settings.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("min_cu", "max_cu", "connections", "workers", "shared_buffers_mb"),
    [
        (0.25, 0.25, 104, 13, 128),  # fixed 0.25 CU: the formula's 128 MB floor
        (1.0, 1.0, 419, 15, 128),
        (2.0, 2.0, 839, 17, 215),
        (0.25, 2.0, 839, 17, 215),   # ADR-011's autoscaling compute: sized by its maximum
        (0.25, 4.0, 839, 21, 216),   # compute_size is capped at 8 x min
    ],
)
def test_formula(neon, min_cu, max_cu, connections, workers, shared_buffers_mb):
    settings = neon.neon_settings(min_cu, max_cu)
    assert settings.max_connections == connections
    assert settings.max_worker_processes == workers
    assert settings.shared_buffers_mb == shared_buffers_mb
    assert settings.max_parallel_workers == 8


def test_rejects_an_empty_range(neon):
    with pytest.raises(ValueError):
        neon.neon_settings(2.0, 0.25)
    with pytest.raises(ValueError):
        neon.neon_settings(0, 2.0)


@pytest.mark.parametrize(
    ("cu", "limits"), [(0.25, ("0.25", "1g")), (2.0, ("2", "8g")), (0.5, ("0.5", "2g"))]
)
def test_container_limits(neon, cu, limits):
    assert neon.container_limits(cu) == limits


def test_env_lines_for_the_default_profile(neon):
    assert neon.env_lines(0.25, 2.0, 0.25) == [
        "ATUS_STACK_DB_SHARED_BUFFERS=215MB",
        "ATUS_STACK_DB_MAX_CONNECTIONS=839",
        "ATUS_STACK_DB_MAX_WORKER_PROCESSES=17",
        "ATUS_EMU_DB_CPUS=0.25",
        "ATUS_EMU_DB_MEMORY=1g",
        "PGBOUNCER_DEFAULT_POOL_SIZE=755",
    ]


def test_env_lines_reject_a_size_outside_the_range(neon):
    with pytest.raises(ValueError):
        neon.env_lines(0.25, 2.0, 4.0)


def test_cli_prints_env_lines(neon, capsys):
    neon.main(["--at-cu", "2"])
    lines = capsys.readouterr().out.splitlines()
    assert "ATUS_EMU_DB_CPUS=2" in lines and "ATUS_EMU_DB_MEMORY=8g" in lines
