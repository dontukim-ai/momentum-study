"""Preregistered suppression over released memory; never expands the grid."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from trading_agent.logic import LogicProposer
from trading_agent.selection import RankedStrategy

from .contracts import AgentPolicy


class MemoryAwareProposer:
    def __init__(
        self,
        proposer: LogicProposer,
        memories: list[dict[str, Any]],
        ordinal: int,
        study: str,
        policy: AgentPolicy,
        resolve: Callable[[str], tuple[str | None, int | None]],
    ) -> None:
        self.proposer = proposer
        self.metadata = dict(proposer.metadata)
        self.suppressed: dict[str, list[str]] = {}
        if policy.memory_policy == "none":
            return
        failures = [
            m
            for m in memories
            if m.get("eligible_for_proposer") is True
            and m.get("market", policy.market) == policy.market
            and m.get("family_lineage_id", "ranked-momentum") == "ranked-momentum"
            and m["source_phase"] == "final"
            and m["failure_code"] in {"FINAL_UNDERPERFORM", "INSUFFICIENT_EVIDENCE"}
        ]
        # Ordinals are local to a study. For foreign studies, take the latest
        # N distinct failed cycles across all foreign studies by release time.
        # Multiple codes for one cycle consume one slot; ties use input order
        # (the repository's append order). A cycle's latest release orders it.
        foreign_cycles = list(
            dict.fromkeys(
                (m["study"], m["source_cycle_id"])
                for _, m in sorted(
                    enumerate(failures),
                    key=lambda item: (item[1]["released_at"], item[0]),
                    reverse=True,
                )
                if m["study"] != study
            )
        )[: policy.memory_cooldown_cycles]
        unresolved = []
        for m in failures:
            fallback, source_ordinal = resolve(m["source_cycle_id"])
            version = m["payload"].get("selected_version") or fallback
            if not version or (m["study"] == study and source_ordinal is None):
                unresolved.append(m["snapshot_id"])
                continue
            if m["study"] == study:
                active = (
                    source_ordinal is not None
                    and 0 < ordinal - source_ordinal <= policy.memory_cooldown_cycles
                )
            else:
                active = (m["study"], m["source_cycle_id"]) in foreign_cycles
            if active:
                self.suppressed.setdefault(version, []).append(m["snapshot_id"])
        self.metadata.update(
            memory_policy=policy.memory_policy,
            suppressed=self.suppressed,
            unresolved=unresolved,
        )

    def propose(self, champion: RankedStrategy) -> list[RankedStrategy]:
        candidates = self.proposer.propose(champion)
        return [s for s in candidates if s.version not in self.suppressed]
