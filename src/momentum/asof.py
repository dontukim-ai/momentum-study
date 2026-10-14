"""Point-in-time visibility, enforced at the type level.

Every downstream signal computation touches data through ``AsOfFrame``
instead of a raw ``polars.DataFrame``. The wrapper's only trick is
``visible_at(t)``: it filters on ``available_at <= t`` and nothing else. In
particular it does NOT filter on any "as-of" business date column (e.g.
``session_date``) — a row dated last week but published tomorrow must stay
hidden today (that is the entire point of CLAUDE.md rule 2), and a row dated
in the future but already published is a data-generation problem, not this
class's problem to solve.

``raw()`` is a deliberate escape hatch, not an oversight: some legitimate
operations (e.g. building the frozen holdout manifest) need the whole frame.
It stays auditable — every call increments ``raw_access_count`` and logs a
warning — so a leakage audit can find where it was used.
"""

from __future__ import annotations

import logging
from datetime import date

import polars as pl

log = logging.getLogger("momentum.leakage")


class LookaheadError(RuntimeError):
    """미래 정보 접근 시도, 또는 시점 정보 없이 AsOfFrame 을 만들려는 시도."""


class AsOfFrame:
    """관측 시점 t 에 실제로 이용 가능했던 행만 노출하는 래퍼."""

    def __init__(self, df: pl.DataFrame, asof_col: str = "available_at") -> None:
        if asof_col not in df.columns:
            raise LookaheadError(
                f"'{asof_col}' 컬럼이 없는 표는 언제 알려졌는지 답할 수 없으므로 "
                "AsOfFrame 이 될 수 없다."
            )
        self._df = df.clone()
        self._col = asof_col
        self.raw_access_count = 0

    @property
    def columns(self) -> list[str]:
        return self._df.columns

    def visible_at(self, t: date) -> pl.DataFrame:
        """t 시점에 이미 공개된(``available_at <= t``) 행만 반환한다.

        ``session_date`` 등 다른 날짜 컬럼은 보지 않는다: 지난 주 날짜의 행이라도
        발표가 내일이면 오늘은 보이면 안 되고, 반대로 미래 날짜의 행이 어쩌다
        이미 발표되어 있어도 그건 이 클래스가 판단할 문제가 아니다. 원본
        프레임은 변경하지 않으며, 호출마다 독립적인 결과를 돌려준다.
        """
        return self._df.filter(pl.col(self._col) <= t)

    def raw(self) -> pl.DataFrame:
        """탈출구: 전체 프레임에 접근한다. 호출은 감사를 위해 기록된다."""
        self.raw_access_count += 1
        log.warning("AsOfFrame.raw() 호출 — 누수 감사 대상 (count=%d)", self.raw_access_count)
        return self._df
