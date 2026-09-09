"""Logging configuration for the pipeline CLI."""

from __future__ import annotations

import logging
import sys


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    # curl_cffi / urllib noise is not useful at INFO
    logging.getLogger("curl_cffi").setLevel(logging.WARNING)
