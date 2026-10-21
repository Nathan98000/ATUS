"""Read-only HTTP API (Phase 3) around the Phase 2 analytical engine.

The API is an interface, not a second analytical engine: route handlers
translate validated requests into ``AnalysisSpec`` objects, call
``AnalysisEngine``, and serialize the engine's result objects. All statistical
semantics live in ``atus_pipeline.analytics``.
"""

API_VERSION = "v1"

RESPONSE_FORMAT = "1"
"""Version of the analysis response *bytes* (sent as ``X-Response-Format``).

Edge caches key stored responses on it together with the data and analytics
versions, and their entries survive code-only deploys (ADR-015); bump it
whenever the bytes of an analysis response change (fields, their order or
formatting, warning or label text) without a change in
``ANALYTICS_VERSION``, together with the Worker's ``RESPONSE_FORMAT`` var.
``tests/unit/api/test_response_format.py`` pins the response schemas and
bytes to this value.
"""
