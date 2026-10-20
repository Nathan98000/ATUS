"""Loader errors (kept separate so the switch machinery can raise them too)."""

from __future__ import annotations


class LoadError(RuntimeError):
    """A load could not complete. The live data is unchanged."""
