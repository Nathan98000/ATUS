"""The serving database: a reduced, read-only copy of the full database that
keeps every table, column and constraint the engine, the API and
``atus validate-analytics`` read (docs/adr/ADR-013-data-releases-and-serving-db.md).

This package holds what the rest of the pipeline needs to know about it: the
explicit serving marker and the guard that keeps full-database commands
(``atus migrate``, ``atus load``, ``atus validate-db``) away from it.
"""

from .guard import SERVING_MARKER, ServingDatabaseError, refuse_if_serving, serving_marker

__all__ = ["SERVING_MARKER", "ServingDatabaseError", "refuse_if_serving", "serving_marker"]
