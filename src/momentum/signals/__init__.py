"""Hypothesis signals: each returns a rank table, never touches accounting.

CLAUDE.md rule 7 forbids duplicating the execution/accounting engines per
hypothesis -- a ``Signal`` computes scores and a rank cut only. Importing
this package registers every known signal (currently just H4) into
``REGISTRY`` so callers can do ``REGISTRY[hypothesis_id].rank(...)`` without
knowing which module defines it.
"""

from __future__ import annotations

from momentum.signals import h1 as _h1  # noqa: F401  (import registers H1)
from momentum.signals import h2 as _h2  # noqa: F401  (import registers H2)
from momentum.signals import h3 as _h3  # noqa: F401  (import registers H3)
from momentum.signals import h4 as _h4  # noqa: F401  (import registers H4)
from momentum.signals.base import REGISTRY, RankTable, Signal

__all__ = ["REGISTRY", "RankTable", "Signal"]
