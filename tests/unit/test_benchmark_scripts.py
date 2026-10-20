"""The performance scripts take their years from the data (audit F55), so a
new release (2003-2026) changes their cases without an edit."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
YEARS_0326 = tuple(range(2003, 2027))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_engine_benchmark_cases_follow_the_loaded_years():
    cases = _load("benchmark_analytics").cases(YEARS_0326)
    by_name = {name: spec for name, _, spec in cases}
    assert by_name["one activity, one year, point only"].years == (2026,)
    assert by_name["pooled 23 years (all except 2020), with SE"].years == tuple(
        y for y in YEARS_0326 if y != 2020
    )
    assert by_name["24-year trend with per-year SEs"].years == YEARS_0326


def test_api_benchmark_cases_follow_meta_years():
    cases = _load("benchmark_api").cases(list(YEARS_0326))
    bodies = {name: body for name, _, _, body in cases}
    assert bodies["estimate: sleep 2026, variance=none"]["years"] == [2026]
    assert 2020 not in bodies["estimate: pooled 23 years, replicate SE"]["years"]
    assert bodies["trend: sleep 2003-2026, per-year SEs"]["years"] == list(YEARS_0326)


@pytest.mark.parametrize("name", ["benchmark_analytics", "benchmark_api"])
def test_no_hard_coded_survey_years_beyond_2020(name):
    tree = ast.parse((SCRIPTS / f"{name}.py").read_text())
    numbers = {
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, int)
    }
    # 2020 is a fixed fact (no multi-year weight); no other year may be coded
    assert not {n for n in numbers if 2003 <= n <= 2100} - {2020}
