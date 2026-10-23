"""Read-only HTTP API (Phase 3) around the Phase 2 analytical engine.

The API is an interface, not a second analytical engine: route handlers
translate validated requests into ``AnalysisSpec`` objects, call
``AnalysisEngine``, and serialize the engine's result objects. All statistical
semantics live in ``atus_pipeline.analytics``.
"""

API_VERSION = "v1"
