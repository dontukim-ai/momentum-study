"""Holdout-lock guard.

This module will eventually hold the full holdout decision machinery (Task
13: the frozen manifest, the single go/no-go check against it). Task 10 only
needs the exception it raises -- a ``phase=holdout`` run has no frozen
manifest to check against yet, so it must refuse outright rather than run
and let someone peek at 2021-2025 results before the rules are locked
(CLAUDE.md rule 5). Nothing else belongs in this file until Task 13.
"""

from __future__ import annotations


class HoldoutLockedError(RuntimeError):
    """Raised whenever ``phase=holdout`` is requested without a frozen manifest."""
