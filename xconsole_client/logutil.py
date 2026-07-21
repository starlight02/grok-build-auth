# -*- coding: utf-8 -*-
"""Library logging helpers.

CLI entrypoints may keep ``print`` for human progress bars. Library modules
should use :func:`get_logger` so debug can be redirected or leveled without
rewriting call sites.
"""

from __future__ import annotations

import logging
import os
from typing import Optional


_CONFIGURED = False


def _ensure_configured() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    level_name = (os.environ.get("XCONSOLE_LOG_LEVEL") or "").strip().upper()
    if level_name:
        level = getattr(logging, level_name, logging.INFO)
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
            datefmt="%H:%M:%S",
        )
    _CONFIGURED = True


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """Return a module logger under the ``xconsole`` namespace."""
    _ensure_configured()
    if not name:
        return logging.getLogger("xconsole")
    if name.startswith("xconsole"):
        return logging.getLogger(name)
    return logging.getLogger(f"xconsole.{name}")
