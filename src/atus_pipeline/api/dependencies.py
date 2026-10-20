"""FastAPI dependencies: settings, database, cache, engine factory, runner.

All dependencies are ``async`` and do no I/O: they hand out per-process
objects built in the application lifespan. Database work happens inside the
route, in exactly one worker-thread hop per transaction, through
:meth:`Database.transaction` — so a request waiting for a connection or a
lane holds no thread, and DB-free routes never touch the threadpool (audit
F02). The Phase 2 ``AnalysisEngine`` is instantiated per computation around
that transaction's connection; it is cheap (a few small lexicon lookups) and
guarantees no analysis state is shared between concurrent requests.

Tests replace ``get_database`` / ``get_engine_factory`` / ``get_cache`` via
``app.dependency_overrides``.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Request

from ..analytics import AnalysisEngine
from ..config import Settings
from .analysis_runner import AnalysisRunner, EngineFactory
from .cache import AnalysisCache
from .database import Database


async def get_settings(request: Request) -> Settings:
    return request.app.state.settings


async def get_cache(request: Request) -> AnalysisCache:
    return request.app.state.cache


async def get_database(request: Request) -> Database:
    return request.app.state.database


async def get_engine_factory() -> EngineFactory:
    return AnalysisEngine


async def get_runner(request: Request) -> AnalysisRunner:
    return request.app.state.runner


@dataclass(frozen=True)
class AnalysisRuntime:
    runner: AnalysisRunner
    cache: AnalysisCache
    db: Database
    engine_factory: EngineFactory


async def get_analysis_runtime(
    runner: AnalysisRunner = Depends(get_runner),
    cache: AnalysisCache = Depends(get_cache),
    db: Database = Depends(get_database),
    engine_factory: EngineFactory = Depends(get_engine_factory),
) -> AnalysisRuntime:
    return AnalysisRuntime(runner=runner, cache=cache, db=db, engine_factory=engine_factory)
