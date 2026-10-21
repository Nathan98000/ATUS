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
``ANALYTICS_VERSION``. A bump edits three files: this one,
``edge/wrangler.jsonc`` (the Worker's ``RESPONSE_FORMAT`` default, used by
``wrangler dev`` and the local stack) and ``edge/test/node/config.test.ts``.
Everything else follows this constant: deploy.yml publishes the format the
snapshot recorded, ``scripts/build_snapshot.py`` takes the one the API
reports, and ``scripts/smoke_stack.sh`` reads it here for the stack probe.
``tests/unit/api/test_response_format.py`` pins the response schemas, the
bytes of hand-built results and the real engine's responses to a fixed set
of requests (``ENGINE_REQUESTS``: every warning, label and description form
the engine produces today) to this value and ``ANALYTICS_VERSION``, and the
copies to it. New engine text needs a request there to be covered.
"""
