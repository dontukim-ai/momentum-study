# 미국 주식 모멘텀 가설 검증 엔진 구현 계획 (Claude 실행 최적화판)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (권장) 또는 superpowers:executing-plans 로 이 계획을 Task 단위로 실행한다. 모든 단계는 체크박스(`- [ ]`)로 추적한다.

**Goal:** H1~H4 네 개의 모멘텀 가설을, 미래정보 누수 없이 다음 정규장 시가로 체결하는 공통 엔진 위에서 실행하고, 비용 차감 후 통계적 우위 여부를 보류 표본에서 한 번만 판정하는 재현 가능한 배치 프로그램을 만든다.

**Architecture:** 단일 Python 패키지. `config/*.yaml` 의 선언적 명세 → `signals`(가설별 플러그인) → 공통 `targets/execution/ledger`(가설과 무관한 회계 엔진) → `evaluate`(부트스트랩·다중검정) → `report`. 가설별 복제 엔진을 만들지 않고, 가설은 "그날의 순위표"만 반환한다. 실데이터 확보 이전에는 결정론적 **합성 시장 생성기**를 1급 데이터 소스로 두어 Task 1~14 전부를 데이터 없이 TDD로 완성한다.

**Tech Stack:** Python 3.12, uv(잠금), polars + DuckDB + Parquet, pydantic v2(설정 검증), numpy/scipy, pytest + hypothesis, Jinja2(정적 HTML), ruff + mypy.

**Spec:** `docs/spec-v2.0.md` (미국 주식 모멘텀 가설 검증 프로그램 단계별 개발 계획 v2.0)

---

## 0. 이 계획이 원본 계획과 다른 점 — Claude로 개발하기 위한 재배치

원본 v2.0은 **연구 설계 문서**로서 완결적이다. 아래는 그 내용을 바꾸지 않고, Claude 세션에서 실행 가능하게 재배치한 것이다.

| 원본의 성질 | Claude 개발에서 생기는 문제 | 이 계획의 대응 |
|---|---|---|
| 1단계(데이터)가 2~4단계를 막는다 | 데이터 미확보 상태에서 세션이 진행 불가 | **합성 시장 생성기를 Task 2로 최우선 구현.** Task 3~14는 실데이터 0바이트로 완주 |
| "미래 정보를 쓰지 마라"가 산문 규칙 | LLM은 산문 규칙을 조용히 위반한다 | `available_at` 을 **타입 레벨로 강제**(Task 4). 목표수량 함수는 시가를 인자로 받지도 못하게 한다(Task 8) |
| 보류 표본(2021~2025) 열람 금지 | 세션 중 무심코 전체 기간 실행 가능 | `phase` 를 CLI 필수 인자로, holdout은 **동결 manifest 없으면 거부**(Task 13) + PreToolUse hook(Task 1) |
| 산식이 표 안의 문장 | 세션마다 재해석되어 미묘하게 달라짐 | 산식을 **손계산 골든 픽스처 CSV**로 고정(Task 9·10). 문장이 아니라 숫자가 명세 |
| 15~22일 단일 작업 흐름 | 컨텍스트 한계로 후반 세션이 앞 결정을 잊음 | Task = 1세션. `CLAUDE.md` 의 불변 규칙 + 각 Task의 `Interfaces` 블록으로 컨텍스트 재구성 |

**세션 운영 규칙(권장):** Task 1개 = Claude 세션 1개. 세션 시작 시 읽을 것은 `CLAUDE.md` + 이 계획의 해당 Task + `Interfaces`에 적힌 파일뿐. 세션 종료 시 커밋. 세션이 길어지면 이어붙이지 말고 다음 Task로 넘긴다.

## Global Constraints

명세에서 그대로 옮긴 프로젝트 전역 제약. **모든 Task의 요구사항에 암묵적으로 포함된다.**

- **미래 정보 금지:** 신호 시각(종가 이후) < 주문 수량 결정 시각(전일 종가 정보) < 체결 시각(다음 정규장 시가). 다음 시가를 미리 이용해 주문 수량을 결정하지 않는다.
- **선정 상한:** 최대 50종목. 목표 비중 `min(1/N, 5%)`. N<20이면 잔액은 현금.
- **유니버스 기본값(동결 대상):** 원시 종가 ≥ $5, 직전 60거래일 평균 거래대금 ≥ $20M, 유효 가격 이력 ≥ 252거래일, 평균 거래대금 상위 최대 1,000개.
- **비용:** 기준 편도 10bp, 스트레스 25bp, 낙관 5bp. 참여 한도 = 직전 20일 평균 거래량의 0.1%.
- **기준 계좌:** $30,000, 정수 주식. 민감도 $10,000 / $100,000. 현금 이자 0. 차입·음수 현금 금지.
- **기간:** 워밍업 1999, 개발 2000~2012, 시간순 검증 2013~2020, **보류 2021~2025(1회 개봉)**.
- **부트스트랩:** 날짜 블록, 평균 블록 63거래일, 10,000회, 고정 시드. 민감도 21/126일. 중심화 후 학생화 통계, 단측 p값, 4가설 Holm 보정.
- **금지:** 원시 가격 장부에 수정 총수익률 중복 적용, 결측 시가를 전일 종가로 대체, 발행주식 수 결측을 현재값으로 대체, 이상치 자동 삭제, 결과 확인 후 기준 완화.
- **결정론:** 동일 입력 + 동일 시드 → 바이트 단위 동일 출력. 이것은 테스트 대상이다(Task 9).
- **미정의 필드는 추정하지 않고 명시적 오류(`SpecUndefinedError`)를 반환한다.**

---

## File Structure

```
momentum/
  CLAUDE.md                      # 세션마다 자동 로드되는 불변 규칙 (Task 1)
  pyproject.toml / uv.lock
  .claude/settings.json          # holdout 차단 hook (Task 1)
  config/
    universe.yaml                # 유니버스 필터 임계값
    hypotheses.yaml              # H1~H4 산식 파라미터
    execution.yaml               # 비용·계좌·참여한도
    validation.yaml              # 기간·부트스트랩·검정
  docs/
    spec-v2.0.md                 # 원본 연구 명세 (권위 문서)
    decision_rules.md            # 판정 규칙 (Task 1)
    data_source_matrix.md        # 명세 1단계 산출물 (Task 14)
    null_calibration.md          # 합성 귀무 자료 1종 오류율 (Task 13)
  src/momentum/
    config.py                    # pydantic 설정 로드 + config_hash
    asof.py                      # available_at 강제 래퍼 (누수 방지의 핵심)
    calendar.py                  # 거래일 달력, 리밸런싱 관측일
    synth.py                     # 결정론적 합성 시장 생성기
    data/
      schema.py                  # 6개 표준 테이블 스키마
      ingest.py  normalize.py  quality.py     # Task 15 (실데이터 합류)
    universe.py                  # 월별 적격 종목 + exclusion_reason
    signals/
      base.py                    # Signal 프로토콜 + REGISTRY
      h1.py h2.py h3.py h4.py
    portfolio/targets.py         # 목표 비중 → 전일 종가 기반 목표 수량
    execution/
      costs.py                   # bp 비용 모형 + 참여 한도
      engine.py                  # 주문 → 체결/취소/축소
    ledger/ledger.py             # 현금·포지션·미수배당·순자산
    evaluate/
      benchmarks.py  metrics.py  bootstrap.py  multiple_testing.py  decide.py
    runs/registry.py             # run_id, 해시, accessed_holdout 기록
    report/html.py               # Jinja2 정적 보고서
    cli.py                       # validate-data / build-universe / run / evaluate / report / release-holdout
  tests/
    fixtures/                    # 손계산 골든 CSV (숫자가 명세다)
    unit/ golden/ property/
```

---

### Task 1: 프로젝트 스캐폴딩과 `CLAUDE.md` 불변 규칙

가장 먼저 만드는 것은 코드가 아니라 **이후 모든 세션이 읽을 규칙**이다.

**Files:**
- Create: `pyproject.toml`, `CLAUDE.md`, `docs/decision_rules.md`, `.claude/settings.json`, `.claude/guard_holdout.py`
- Create: `src/momentum/__init__.py`, `tests/unit/test_smoke.py`

**Interfaces:**
- Consumes: 없음
- Produces: `uv run pytest -q`, `uv run mypy src`, `uv run ruff check` 가 동작하는 환경

- [ ] **Step 1: 패키지 초기화**

```bash
cd momentum
uv init --package --name momentum --python 3.12
uv add polars duckdb pyarrow pydantic numpy scipy jinja2 pyyaml
uv add --dev pytest pytest-cov hypothesis mypy ruff
```

- [ ] **Step 2: `CLAUDE.md` 작성 — 세션마다 자동 로드되는 불변 규칙**

```markdown
# momentum — 개발 불변 규칙

이 프로젝트는 투자 판정용 검증 엔진이다. 성능보다 **정확성과 재현성**이 우선이다.

## 절대 금지 (위반 시 코드가 아니라 결론이 틀린다)
1. 다음 시가(open)를 주문 수량 계산에 사용하지 않는다. 수량은 전일 종가 정보로만 정한다.
2. `available_at` 이 관측 시점보다 미래인 행을 신호 계산에 넣지 않는다. `AsOfFrame` 을 우회하지 않는다.
3. 결측을 채우지 않는다. 결측 시가 → 매수 취소. 결측 주식 수 → H3 보류. 이상 수익률 → 격리, 삭제 금지.
4. 원시 가격 장부에 배당을 반영했으면 수정 총수익률을 다시 곱하지 않는다.
5. `phase=holdout` 은 동결 manifest 없이 실행 금지. 결과를 본 뒤 설정을 바꾸지 않는다.
6. 테스트가 실패하면 테스트를 고치지 않고 구현을 고친다. 임계값을 결과에 맞춰 완화하지 않는다.
7. 가설별로 체결/회계 엔진을 복제하지 않는다. 가설은 순위표만 반환한다.

## 작업 방식
- TDD: 실패하는 테스트 → 최소 구현 → 통과 → 커밋.
- 손계산 골든 픽스처(`tests/fixtures/`)가 산식의 최종 권위다. 산문 설명과 충돌하면 픽스처가 맞다.
- 미정의 동작은 추정하지 않고 `SpecUndefinedError` 를 던진다.
- 금액은 `Decimal`, 비율은 `float`. 순자산 대조는 센트 단위 정확 비교.

## 명령
- 테스트: `uv run pytest -q`
- 전체 검증: `uv run pytest -q && uv run mypy src && uv run ruff check`
```

- [ ] **Step 3: 보류 표본 보호 hook 등록**

`.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [{ "type": "command", "command": "python .claude/guard_holdout.py" }]
      }
    ]
  }
}
```

`.claude/guard_holdout.py`:

```python
import json, sys

payload = json.load(sys.stdin)
cmd = payload.get("tool_input", {}).get("command", "")
if "--phase holdout" in cmd and "release-holdout" not in cmd:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason":
                "holdout 구간은 release-holdout 명령과 동결 manifest로만 실행한다.",
        }
    }))
sys.exit(0)
```

- [ ] **Step 4: 스모크 테스트를 작성하고 통과 확인**

```python
# tests/unit/test_smoke.py
import momentum


def test_package_imports():
    assert momentum.__name__ == "momentum"
```

Run: `uv run pytest -q`
Expected: PASS (1 passed)

- [ ] **Step 5: 커밋**

```bash
git init && git add -A
git commit -m "chore: scaffold momentum package with invariant rules and holdout guard"
```

---

### Task 2: 결정론적 합성 시장 생성기

**왜 먼저인가:** 실데이터 확보는 미확정이다. 합성 시장이 있으면 Task 3~14(엔진 전체)를 데이터 없이 완주할 수 있고, 명세 6단계가 요구하는 **통계 검정의 합성 귀무 자료 점검**에도 그대로 쓰인다.

**Files:**
- Create: `src/momentum/synth.py`
- Test: `tests/unit/test_synth.py`

**Interfaces:**
- Consumes: 없음
- Produces: `generate_market(*, seed: int, n_securities: int, start: date, end: date, with_events: bool = True) -> SynthMarket`. `SynthMarket` 은 frozen dataclass로 `security_master, daily_prices, corporate_actions, shares_history, calendar` 5개 `polars.DataFrame` 속성을 갖는다. 컬럼명은 명세 §13 최소 데이터 표와 동일.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/unit/test_synth.py
from datetime import date

from momentum.synth import generate_market

KW = dict(seed=7, n_securities=20, start=date(2000, 1, 3), end=date(2000, 6, 30))


def test_same_seed_gives_identical_frames():
    assert generate_market(**KW).daily_prices.equals(generate_market(**KW).daily_prices)


def test_market_includes_required_lifecycle_events():
    m = generate_market(**KW)
    assert {"split", "cash_dividend"} <= set(m.corporate_actions["action_type"].to_list())
    assert m.security_master.filter(m.security_master["effective_to"].is_not_null()).height >= 1


def test_no_price_rows_after_delisting():
    m = generate_market(**KW)
    sm = m.security_master.filter(m.security_master["effective_to"].is_not_null())
    for sid, last in zip(sm["security_id"], sm["effective_to"]):
        rows = m.daily_prices.filter(
            (m.daily_prices["security_id"] == sid) & (m.daily_prices["session_date"] > last)
        )
        assert rows.height == 0


def test_corporate_actions_are_available_before_their_ex_date():
    m = generate_market(**KW)
    ca = m.corporate_actions
    assert (ca["available_at"] <= ca["ex_date"]).all()
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/unit/test_synth.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'momentum.synth'`

- [ ] **Step 3: 최소 구현**

```python
# src/momentum/synth.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import polars as pl


@dataclass(frozen=True)
class SynthMarket:
    security_master: pl.DataFrame
    daily_prices: pl.DataFrame
    corporate_actions: pl.DataFrame
    shares_history: pl.DataFrame
    calendar: pl.DataFrame


def _sessions(start: date, end: date) -> list[date]:
    d, out = start, []
    while d <= end:
        if d.weekday() < 5:  # 휴일은 Task 5 달력에서 정교화
            out.append(d)
        d += timedelta(days=1)
    return out


def generate_market(
    *, seed: int, n_securities: int, start: date, end: date, with_events: bool = True
) -> SynthMarket:
    rng = np.random.default_rng(seed)
    sessions = _sessions(start, end)
    ids = [f"SEC{i:04d}" for i in range(n_securities)]
    delisted = set(ids[: max(1, n_securities // 10)]) if with_events else set()
    delist_on = {sid: sessions[len(sessions) // 2] for sid in delisted}

    px_rows, ca_rows, sh_rows, sm_rows = [], [], [], []
    for k, sid in enumerate(ids):
        level = 20.0 + 80.0 * rng.random()
        shares = float(rng.integers(20_000_000, 400_000_000))
        end_s = delist_on.get(sid)
        for d in sessions:
            if end_s is not None and d > end_s:
                break
            level = max(1.0, level * (1.0 + float(rng.normal(0.0004, 0.02))))
            px_rows.append({
                "security_id": sid, "session_date": d,
                "open_raw": level * (1 + float(rng.normal(0, 0.003))),
                "high_raw": level * 1.01, "low_raw": level * 0.99,
                "close_raw": level, "volume": float(rng.integers(500_000, 8_000_000)),
                "currency": "USD", "available_at": d, "source_id": "synth",
            })
            sh_rows.append({
                "security_id": sid, "effective_date": d, "shares_outstanding": shares,
                "unit": "share", "available_at": d,
            })
        if with_events and k % 5 == 0:
            ex = sessions[len(sessions) // 3]
            ca_rows.append({
                "security_id": sid, "action_type": "split", "ex_date": ex, "pay_date": ex,
                "split_ratio": 2.0, "cash_amount": None,
                "available_at": ex - timedelta(days=10),
            })
        if with_events and k % 3 == 0:
            ex = sessions[len(sessions) // 4]
            ca_rows.append({
                "security_id": sid, "action_type": "cash_dividend", "ex_date": ex,
                "pay_date": ex + timedelta(days=14), "split_ratio": None, "cash_amount": 0.25,
                "available_at": ex - timedelta(days=10),
            })
        sm_rows.append({
            "security_id": sid, "ticker": sid, "exchange": "XNYS", "share_type": "common",
            "effective_from": sessions[0], "effective_to": end_s, "available_at": sessions[0],
        })

    cal = pl.DataFrame({
        "session_date": sessions,
        "open_utc": [None] * len(sessions),
        "close_utc": [None] * len(sessions),
        "exchange": ["XNYS"] * len(sessions),
    })
    return SynthMarket(
        pl.DataFrame(sm_rows), pl.DataFrame(px_rows),
        pl.DataFrame(ca_rows), pl.DataFrame(sh_rows), cal,
    )
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/unit/test_synth.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: 커밋**

```bash
git add src/momentum/synth.py tests/unit/test_synth.py
git commit -m "feat(synth): deterministic synthetic market with splits, dividends, delistings"
```

---

### Task 3: 설정 로더와 config_hash

**Files:**
- Create: `src/momentum/config.py`, `config/universe.yaml`, `config/execution.yaml`, `config/hypotheses.yaml`, `config/validation.yaml`
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Consumes: 없음
- Produces: `load_config(config_dir: Path) -> Config`. `Config` 는 frozen pydantic 모델로 `.universe`, `.execution`, `.validation`, `.hypotheses`, `.hash`(sha256 hex) 속성을 갖는다. `Phase = Literal["development", "walkforward", "holdout"]`. `SpecUndefinedError`.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/unit/test_config.py
from pathlib import Path

import pytest

from momentum.config import SpecUndefinedError, load_config

CFG = Path("config")


def test_defaults_match_spec():
    c = load_config(CFG)
    assert c.execution.base_cost_bps == 10.0
    assert c.execution.account_equity_usd == 30000
    assert c.execution.max_positions == 50
    assert c.execution.max_weight == 0.05
    assert c.universe.min_close_usd == 5.0
    assert c.universe.min_adv_usd == 20_000_000
    assert c.validation.bootstrap_block_days == 63
    assert c.validation.bootstrap_iterations == 10_000


def test_phase_windows_are_disjoint_and_ordered():
    w = load_config(CFG).validation.windows
    assert w["development"].end < w["walkforward"].start
    assert w["walkforward"].end < w["holdout"].start


def test_hash_is_stable_and_is_a_sha256():
    assert load_config(CFG).hash == load_config(CFG).hash
    assert len(load_config(CFG).hash) == 64


def test_unknown_key_is_an_error_not_a_silent_default(tmp_path):
    (tmp_path / "execution.yaml").write_text("base_cost_bps: 10\nmystery_knob: 3\n")
    with pytest.raises(SpecUndefinedError):
        load_config(tmp_path)
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/unit/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'momentum.config'`

- [ ] **Step 3: 최소 구현**

```python
# src/momentum/config.py
from __future__ import annotations

import json
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

Phase = Literal["development", "walkforward", "holdout"]


class SpecUndefinedError(RuntimeError):
    """명세에 없는 값을 추정하지 않고 즉시 중단한다."""


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class UniverseCfg(Strict):
    min_close_usd: float = 5.0
    min_adv_usd: float = 20_000_000
    adv_lookback_days: int = 60
    min_history_days: int = 252
    max_names: int = 1000


class ExecutionCfg(Strict):
    base_cost_bps: float = 10.0
    stress_cost_bps: float = 25.0
    optimistic_cost_bps: float = 5.0
    participation_limit: float = 0.001
    participation_lookback_days: int = 20
    account_equity_usd: int = 30000
    max_positions: int = 50
    max_weight: float = 0.05
    min_positions_for_full_investment: int = 20
    cash_buffer: float = 0.005
    cash_interest_rate: float = 0.0


class Window(Strict):
    start: date
    end: date


class ValidationCfg(Strict):
    windows: dict[str, Window]
    bootstrap_block_days: int = 63
    bootstrap_block_sensitivity: list[int] = [21, 126]
    bootstrap_iterations: int = 10_000
    seed: int = 20260916
    alpha: float = 0.05


class HypothesisCfg(Strict):
    id: str
    top_pct: float
    require_positive: bool
    rebalance: Literal["daily", "weekly", "monthly"]
    lookback: str
    skip: str | None = None
    secondary_filter: str | None = None


class HypothesesCfg(Strict):
    hypotheses: list[HypothesisCfg]


class Config(Strict):
    universe: UniverseCfg
    execution: ExecutionCfg
    validation: ValidationCfg
    hypotheses: HypothesesCfg
    hash: str


def load_config(config_dir: Path) -> Config:
    raw: dict[str, dict] = {}
    for name in ("universe", "execution", "validation", "hypotheses"):
        p = Path(config_dir) / f"{name}.yaml"
        raw[name] = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
    digest = sha256(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()
    try:
        return Config(
            universe=UniverseCfg(**raw["universe"]),
            execution=ExecutionCfg(**raw["execution"]),
            validation=ValidationCfg(**raw["validation"]),
            hypotheses=HypothesesCfg(**raw["hypotheses"]),
            hash=digest,
        )
    except ValidationError as e:
        raise SpecUndefinedError(str(e)) from e
```

`config/validation.yaml`:

```yaml
windows:
  development: {start: 2000-01-03, end: 2012-12-31}
  walkforward: {start: 2013-01-02, end: 2020-12-31}
  holdout:     {start: 2021-01-04, end: 2025-12-31}
bootstrap_block_days: 63
bootstrap_block_sensitivity: [21, 126]
bootstrap_iterations: 10000
seed: 20260916
alpha: 0.05
```

`config/hypotheses.yaml`:

```yaml
hypotheses:
  - {id: H1, top_pct: 0.10, require_positive: true,  rebalance: daily,   lookback: 1d}
  - {id: H2, top_pct: 0.10, require_positive: true,  rebalance: weekly,  lookback: 1w}
  - {id: H3, top_pct: 0.10, require_positive: true,  rebalance: monthly, lookback: 1mo,
     secondary_filter: turnover_top_30pct}
  - {id: H4, top_pct: 0.10, require_positive: false, rebalance: monthly, lookback: 12mo, skip: 1mo}
```

`config/universe.yaml` 과 `config/execution.yaml` 은 위 pydantic 기본값과 **동일한 값을 명시적으로 적는다**. 기본값에 의존하지 않고 동결 대상임을 문서화하기 위해서다.

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/unit/test_config.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: 커밋**

```bash
git add config src/momentum/config.py tests/unit/test_config.py
git commit -m "feat(config): frozen strict config with content hash and phase windows"
```

---

### Task 4: 표준 스키마와 `AsOfFrame` — 미래정보 누수의 타입 레벨 차단

이 프로젝트에서 **가장 중요한 Task**다. 규칙을 산문으로 두면 사람도 LLM도 어긴다. 조인 시점을 타입이 강제하게 만든다.

**Files:**
- Create: `src/momentum/asof.py`, `src/momentum/data/schema.py`
- Test: `tests/unit/test_asof.py`, `tests/unit/test_schema.py`

**Interfaces:**
- Consumes: `momentum.synth.generate_market`
- Produces:
  - `SCHEMAS: dict[str, dict[str, pl.DataType]]` — `security_master, daily_prices, corporate_actions, shares_history, calendar, runs` 6개 (명세 §13).
  - `class AsOfFrame`: 생성자 `AsOfFrame(df: pl.DataFrame, asof_col: str = "available_at")`, 메서드 `visible_at(t: date) -> pl.DataFrame`, `raw() -> pl.DataFrame`(명시적 탈출구, 호출 횟수를 `raw_access_count` 로 기록), 속성 `columns`.
  - `class LookaheadError(RuntimeError)`.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/unit/test_asof.py
from datetime import date

import polars as pl
import pytest

from momentum.asof import AsOfFrame, LookaheadError


def _frame() -> pl.DataFrame:
    return pl.DataFrame({
        "security_id": ["A", "A", "B"],
        "session_date": [date(2000, 1, 3), date(2000, 1, 4), date(2000, 1, 4)],
        "close_raw": [10.0, 11.0, 5.0],
        "available_at": [date(2000, 1, 3), date(2000, 1, 4), date(2000, 1, 6)],
    })


def test_visible_at_hides_rows_not_yet_available():
    out = AsOfFrame(_frame()).visible_at(date(2000, 1, 4))
    assert out.height == 2
    assert "B" not in out["security_id"].to_list()


def test_row_dated_earlier_but_published_later_is_still_hidden():
    f = _frame().with_columns(pl.lit(date(2000, 1, 10)).alias("available_at"))
    assert AsOfFrame(f).visible_at(date(2000, 1, 4)).height == 0


def test_missing_available_at_column_is_rejected_at_construction():
    with pytest.raises(LookaheadError):
        AsOfFrame(_frame().drop("available_at"))


def test_raw_escape_hatch_is_counted_for_audit():
    a = AsOfFrame(_frame())
    a.raw()
    assert a.raw_access_count == 1
```

```python
# tests/unit/test_schema.py
from datetime import date

from momentum.data.schema import SCHEMAS, validate
from momentum.synth import generate_market


def test_all_six_spec_tables_are_defined():
    assert set(SCHEMAS) == {
        "security_master", "daily_prices", "corporate_actions",
        "shares_history", "calendar", "runs",
    }


def test_synthetic_market_conforms_to_the_schema():
    m = generate_market(seed=7, n_securities=5, start=date(2000, 1, 3), end=date(2000, 3, 31))
    validate("daily_prices", m.daily_prices)
    validate("security_master", m.security_master)
    validate("shares_history", m.shares_history)
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/unit/test_asof.py tests/unit/test_schema.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'momentum.asof'`

- [ ] **Step 3: 최소 구현**

```python
# src/momentum/asof.py
from __future__ import annotations

import logging
from datetime import date

import polars as pl

log = logging.getLogger("momentum.leakage")


class LookaheadError(RuntimeError):
    """미래 정보 접근 시도."""


class AsOfFrame:
    """관측 시점 t 에 실제로 이용 가능했던 행만 노출하는 래퍼."""

    def __init__(self, df: pl.DataFrame, asof_col: str = "available_at") -> None:
        if asof_col not in df.columns:
            raise LookaheadError(f"'{asof_col}' 컬럼이 없는 표는 신호 계산에 쓸 수 없다.")
        self._df = df
        self._col = asof_col
        self.raw_access_count = 0

    @property
    def columns(self) -> list[str]:
        return self._df.columns

    def visible_at(self, t: date) -> pl.DataFrame:
        return self._df.filter(pl.col(self._col) <= t)

    def raw(self) -> pl.DataFrame:
        self.raw_access_count += 1
        log.warning("AsOfFrame.raw() 호출 — 누수 감사 대상")
        return self._df
```

`src/momentum/data/schema.py` 는 명세 §13의 6개 표를 `dict[str, pl.DataType]` 로 선언하고, `validate(name, df)` 가 누락 컬럼·타입 불일치를 `SpecUndefinedError` 로 던지게 한다.

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/unit/test_asof.py tests/unit/test_schema.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: 커밋**

```bash
git add src/momentum/asof.py src/momentum/data/schema.py tests/unit/test_asof.py tests/unit/test_schema.py
git commit -m "feat(asof): enforce point-in-time visibility at the type level"
```

---

### Task 5: 거래일 달력과 리밸런싱 일정

**Files:**
- Create: `src/momentum/calendar.py`
- Test: `tests/unit/test_calendar.py`

**Interfaces:**
- Consumes: `calendar` 표의 `session_date`
- Produces: `class TradingCalendar` — 생성자 `TradingCalendar(sessions: list[date])`, 클래스메서드 `from_dates(dates)`, 메서드 `sessions(start, end) -> list[date]`, `next_session(d) -> date | None`, `is_month_end(d) -> bool`, `is_week_end(d) -> bool`, `rebalance_dates(freq: Literal["daily","weekly","monthly"], start, end) -> list[date]`. **반환값은 신호 관측일이며, 체결일은 각 관측일의 `next_session`이다.**

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/unit/test_calendar.py
from datetime import date

from momentum.calendar import TradingCalendar

CAL = TradingCalendar.from_dates([
    date(2000, 6, 28), date(2000, 6, 29), date(2000, 6, 30),  # 금 = 월말 = 주말
    date(2000, 7, 3), date(2000, 7, 5),                        # 7/4 휴장
])


def test_next_session_skips_holidays():
    assert CAL.next_session(date(2000, 7, 3)) == date(2000, 7, 5)


def test_next_session_at_the_end_of_data_is_none():
    assert CAL.next_session(date(2000, 7, 5)) is None


def test_month_end_uses_last_session_not_calendar_last_day():
    assert CAL.is_month_end(date(2000, 6, 30)) is True
    assert CAL.is_month_end(date(2000, 6, 29)) is False


def test_week_end_is_the_last_session_of_the_week():
    assert CAL.is_week_end(date(2000, 6, 30)) is True
    assert CAL.is_week_end(date(2000, 6, 29)) is False


def test_rebalance_dates_are_observation_dates_and_fill_is_the_next_session():
    obs = CAL.rebalance_dates("monthly", date(2000, 6, 1), date(2000, 7, 31))
    assert obs == [date(2000, 6, 30)]
    assert CAL.next_session(obs[0]) == date(2000, 7, 3)
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/unit/test_calendar.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'momentum.calendar'`

- [ ] **Step 3: 최소 구현**

```python
# src/momentum/calendar.py
from __future__ import annotations

from bisect import bisect_right
from datetime import date
from typing import Callable, Literal, Self


class TradingCalendar:
    def __init__(self, sessions: list[date]) -> None:
        self._s = sorted(sessions)

    @classmethod
    def from_dates(cls, dates: list[date]) -> Self:
        return cls(dates)

    def sessions(self, start: date, end: date) -> list[date]:
        return [d for d in self._s if start <= d <= end]

    def next_session(self, d: date) -> date | None:
        i = bisect_right(self._s, d)
        return self._s[i] if i < len(self._s) else None

    def _is_last_of(self, d: date, key: Callable[[date], object]) -> bool:
        if d not in self._s:
            return False
        nxt = self.next_session(d)
        return nxt is None or key(nxt) != key(d)

    def is_month_end(self, d: date) -> bool:
        return self._is_last_of(d, lambda x: (x.year, x.month))

    def is_week_end(self, d: date) -> bool:
        return self._is_last_of(d, lambda x: x.isocalendar()[:2])

    def rebalance_dates(
        self, freq: Literal["daily", "weekly", "monthly"], start: date, end: date
    ) -> list[date]:
        pred = {
            "daily": lambda d: True,
            "weekly": self.is_week_end,
            "monthly": self.is_month_end,
        }[freq]
        return [d for d in self.sessions(start, end) if pred(d)]
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/unit/test_calendar.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: 커밋**

```bash
git add src/momentum/calendar.py tests/unit/test_calendar.py
git commit -m "feat(calendar): session-aware month/week boundaries and rebalance dates"
```

---

### Task 6: 유니버스 구성과 제외 사유

**Files:**
- Create: `src/momentum/universe.py`
- Test: `tests/unit/test_universe.py`, `tests/golden/test_universe_golden.py`
- Create: `tests/fixtures/universe_2000-06-30.csv`

**Interfaces:**
- Consumes: `AsOfFrame`, `TradingCalendar`, `UniverseCfg`
- Produces: `build_universe(prices: AsOfFrame, master: AsOfFrame, cal: TradingCalendar, cfg: UniverseCfg, effective_date: date) -> pl.DataFrame` — 컬럼 `effective_date, security_id, adv_usd, rank, included(bool), exclusion_reason(str | None)`. **제외 종목도 행으로 남긴다**(사유 추적 가능해야 함).

**제외 사유 우선순위(고정, 앞 사유에 걸리면 뒤 조건은 평가하지 않는다):**
`not_listed` → `share_type` → `min_history_days` → `min_close_usd` → `min_adv_usd` → `max_names`

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/unit/test_universe.py
from datetime import date, timedelta

import polars as pl

from momentum.asof import AsOfFrame
from momentum.calendar import TradingCalendar
from momentum.config import UniverseCfg
from momentum.universe import build_universe

CFG = UniverseCfg(min_close_usd=5.0, min_adv_usd=1_000_000, adv_lookback_days=3,
                  min_history_days=3, max_names=2)
DAYS = [date(2000, 6, 26) + timedelta(days=i) for i in range(5)]  # 월~금
EFF = DAYS[-1]
CAL = TradingCalendar.from_dates(DAYS)


def _px(spec: dict[str, tuple[float, float, int]]) -> AsOfFrame:
    """spec: security_id -> (close, volume, n_days_of_history)"""
    rows = []
    for sid, (close, vol, n) in spec.items():
        for d in DAYS[-n:]:
            rows.append({"security_id": sid, "session_date": d, "close_raw": close,
                         "open_raw": close, "volume": vol, "available_at": d})
    return AsOfFrame(pl.DataFrame(rows))


def _master(ids, delisted=()) -> AsOfFrame:
    return AsOfFrame(pl.DataFrame([
        {"security_id": s, "share_type": "common", "effective_from": DAYS[0],
         "effective_to": DAYS[0] if s in delisted else None, "available_at": DAYS[0]}
        for s in ids
    ]))


def _reason(u: pl.DataFrame, sid: str) -> str | None:
    return u.filter(pl.col("security_id") == sid).row(0, named=True)["exclusion_reason"]


def test_penny_stock_is_excluded_with_its_reason():
    spec = {"PENNY": (4.99, 1e6, 5), "OK1": (50.0, 1e6, 5), "OK2": (60.0, 1e6, 5)}
    u = build_universe(_px(spec), _master(spec), CAL, CFG, EFF)
    assert _reason(u, "PENNY") == "min_close_usd"


def test_short_history_is_rejected_before_liquidity_is_even_evaluated():
    spec = {"NEW": (50.0, 1e9, 2), "OK1": (50.0, 1e6, 5), "OK2": (60.0, 1e6, 5)}
    u = build_universe(_px(spec), _master(spec), CAL, CFG, EFF)
    assert _reason(u, "NEW") == "min_history_days"


def test_illiquid_name_is_excluded_on_dollar_volume():
    spec = {"THIN": (50.0, 100, 5), "OK1": (50.0, 1e6, 5), "OK2": (60.0, 1e6, 5)}
    u = build_universe(_px(spec), _master(spec), CAL, CFG, EFF)
    assert _reason(u, "THIN") == "min_adv_usd"


def test_max_names_keeps_top_adv_and_marks_the_rest():
    spec = {f"S{i}": (50.0, 1e6 * (i + 1), 5) for i in range(4)}
    u = build_universe(_px(spec), _master(spec), CAL, CFG, EFF)
    assert u.filter(pl.col("included")).height == 2
    assert _reason(u, "S0") == "max_names"


def test_delisted_security_is_excluded_but_still_present_in_the_output():
    spec = {"DEAD": (50.0, 1e6, 5), "OK1": (50.0, 1e6, 5), "OK2": (60.0, 1e6, 5)}
    u = build_universe(_px(spec), _master(spec, delisted={"DEAD"}), CAL, CFG, EFF)
    assert _reason(u, "DEAD") == "not_listed"
    assert u.filter(pl.col("security_id") == "DEAD").height == 1


def test_universe_never_sees_rows_published_after_the_effective_date():
    spec = {"OK1": (50.0, 1e6, 5), "OK2": (60.0, 1e6, 5)}
    px = _px(spec)
    late = px.visible_at(EFF).with_columns([
        pl.lit("LATE").alias("security_id"), pl.lit(EFF + timedelta(days=3)).alias("available_at"),
    ])
    combined = AsOfFrame(pl.concat([px.visible_at(EFF), late]))
    u = build_universe(combined, _master(list(spec) + ["LATE"]), CAL, CFG, EFF)
    assert "LATE" not in u.filter(pl.col("included"))["security_id"].to_list()
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/unit/test_universe.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'momentum.universe'`

- [ ] **Step 3: 최소 구현**

우선순위 순서대로 `exclusion_reason` 을 확정한다. ADV는 `effective_date` 이전 `adv_lookback_days` 세션의 `close_raw * volume` 평균. `max_names` 는 ADV 내림차순 상위 `max_names` 개만 `included=True`, 나머지는 `max_names` 사유.

```python
# src/momentum/universe.py (핵심 골격)
def build_universe(prices, master, cal, cfg, effective_date):
    px = prices.visible_at(effective_date)
    ms = master.visible_at(effective_date)
    hist = px.group_by("security_id").agg([
        pl.len().alias("n_days"),
        (pl.col("close_raw") * pl.col("volume"))
            .tail(cfg.adv_lookback_days).mean().alias("adv_usd"),
        pl.col("close_raw").last().alias("last_close"),
    ])
    df = ms.join(hist, on="security_id", how="left")
    reason = (
        pl.when(pl.col("effective_to").is_not_null()
                & (pl.col("effective_to") < effective_date)).then(pl.lit("not_listed"))
        .when(pl.col("share_type") != "common").then(pl.lit("share_type"))
        .when(pl.col("n_days").fill_null(0) < cfg.min_history_days).then(pl.lit("min_history_days"))
        .when(pl.col("last_close") < cfg.min_close_usd).then(pl.lit("min_close_usd"))
        .when(pl.col("adv_usd") < cfg.min_adv_usd).then(pl.lit("min_adv_usd"))
        .otherwise(None).alias("exclusion_reason")
    )
    df = df.with_columns(reason).sort("adv_usd", descending=True, nulls_last=True)
    surviving = df.filter(pl.col("exclusion_reason").is_null())
    keep = set(surviving["security_id"].to_list()[: cfg.max_names])
    return df.with_columns([
        pl.when(pl.col("exclusion_reason").is_not_null()).then(pl.col("exclusion_reason"))
          .when(~pl.col("security_id").is_in(list(keep))).then(pl.lit("max_names"))
          .otherwise(None).alias("exclusion_reason"),
    ]).with_columns([
        pl.col("exclusion_reason").is_null().alias("included"),
        pl.lit(effective_date).alias("effective_date"),
        pl.int_range(pl.len()).alias("rank"),
    ]).select(["effective_date", "security_id", "adv_usd", "rank",
               "included", "exclusion_reason"])
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/unit/test_universe.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: 골든 회귀 픽스처 고정**

합성 시장 seed=7 의 2000-06-30 유니버스를 `tests/fixtures/universe_2000-06-30.csv` 로 저장하고 회귀 비교를 추가한다.

```python
# tests/golden/test_universe_golden.py
import polars as pl
from datetime import date
from momentum.universe import build_universe

FIX = "tests/fixtures/universe_2000-06-30.csv"


def test_universe_for_seed7_is_unchanged():
    got = build_universe(*_synth_inputs(seed=7), effective_date=date(2000, 6, 30))
    assert got.equals(pl.read_csv(FIX, try_parse_dates=True))
```

Run: `uv run pytest tests/golden/test_universe_golden.py -v`
Expected: PASS

- [ ] **Step 6: 커밋**

```bash
git add src/momentum/universe.py tests/unit/test_universe.py tests/golden tests/fixtures
git commit -m "feat(universe): point-in-time eligible universe with ordered exclusion reasons"
```

---

### Task 7: 비용 모형과 참여 한도

**Files:**
- Create: `src/momentum/execution/costs.py`
- Test: `tests/unit/test_costs.py`

**Interfaces:**
- Consumes: `ExecutionCfg`
- Produces:
  - `one_way_cost(notional: Decimal, bps: float) -> Decimal` — 센트 단위 `ROUND_HALF_UP`
  - `participation_cap(adv_shares: float, cfg: ExecutionCfg) -> int` — 내림
  - `apply_caps(desired_shares: int, adv_shares: float, cfg: ExecutionCfg) -> tuple[int, str | None]` — 축소된 수량과 사유

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/unit/test_costs.py
from decimal import Decimal

from momentum.config import ExecutionCfg
from momentum.execution.costs import apply_caps, one_way_cost, participation_cap

CFG = ExecutionCfg()


def test_base_cost_is_ten_bps_of_notional():
    assert one_way_cost(Decimal("10000.00"), 10.0) == Decimal("10.00")


def test_cost_is_rounded_to_the_cent():
    assert one_way_cost(Decimal("1234.56"), 10.0) == Decimal("1.23")


def test_stress_cost_exceeds_base_which_exceeds_optimistic():
    n = Decimal("10000.00")
    assert one_way_cost(n, 25.0) > one_way_cost(n, 10.0) > one_way_cost(n, 5.0)


def test_participation_cap_is_one_tenth_percent_of_adv_rounded_down():
    assert participation_cap(1_234_567, CFG) == 1234


def test_order_above_cap_is_reduced_with_a_reason():
    assert apply_caps(5000, adv_shares=1_000_000, cfg=CFG) == (1000, "participation_limit")


def test_order_below_cap_is_untouched():
    assert apply_caps(500, adv_shares=1_000_000, cfg=CFG) == (500, None)


def test_zero_adv_yields_zero_shares_not_a_division_error():
    assert apply_caps(500, adv_shares=0.0, cfg=CFG) == (0, "participation_limit")
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/unit/test_costs.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'momentum.execution.costs'`

- [ ] **Step 3: 최소 구현**

```python
# src/momentum/execution/costs.py
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from math import floor

from momentum.config import ExecutionCfg

CENT = Decimal("0.01")


def one_way_cost(notional: Decimal, bps: float) -> Decimal:
    return (notional * Decimal(str(bps)) / Decimal("10000")).quantize(CENT, ROUND_HALF_UP)


def participation_cap(adv_shares: float, cfg: ExecutionCfg) -> int:
    return max(0, floor(adv_shares * cfg.participation_limit))


def apply_caps(desired_shares: int, adv_shares: float, cfg: ExecutionCfg) -> tuple[int, str | None]:
    cap = participation_cap(adv_shares, cfg)
    if desired_shares <= cap:
        return desired_shares, None
    return cap, "participation_limit"
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/unit/test_costs.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: 커밋**

```bash
git add src/momentum/execution/costs.py tests/unit/test_costs.py
git commit -m "feat(costs): bp cost model and ADV participation cap"
```

---

### Task 8: 목표 수량 산정 — 전일 종가만 사용

**Files:**
- Create: `src/momentum/portfolio/targets.py`
- Test: `tests/unit/test_targets.py`

**Interfaces:**
- Consumes: `ExecutionCfg`
- Produces: `compute_targets(ranked: list[str], nav: Decimal, prev_close: dict[str, Decimal], holdings: dict[str, int], cfg: ExecutionCfg) -> list[TargetOrder]`. `class TargetOrder(NamedTuple): security_id: str; side: Literal["buy","sell"]; shares: int; rank: int` (매도는 `rank = -1`). **매도가 먼저, 매수는 순위 오름차순.** 함수 시그니처가 시가를 받지 않으므로 누수가 구조적으로 불가능하다.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/unit/test_targets.py
import inspect
from decimal import Decimal

from momentum.config import ExecutionCfg
from momentum.portfolio import targets as T

CFG = ExecutionCfg(cash_buffer=0.0)


def test_signature_cannot_see_the_execution_price_at_all():
    params = set(inspect.signature(T.compute_targets).parameters)
    assert not {"open_px", "open_price", "fill_price", "opens"} & params


def test_weight_is_capped_at_five_percent():
    ranked = [f"S{i}" for i in range(10)]
    px = {s: Decimal("100") for s in ranked}
    out = T.compute_targets(ranked, Decimal("30000"), px, {}, CFG)
    # 1/N = 10% 이지만 상한 5% → 종목당 $1500 → 15주
    assert all(o.shares == 15 for o in out if o.side == "buy")


def test_fewer_than_twenty_names_leaves_residual_cash():
    ranked = [f"S{i}" for i in range(5)]
    px = {s: Decimal("100") for s in ranked}
    out = T.compute_targets(ranked, Decimal("30000"), px, {}, CFG)
    assert sum(o.shares * 100 for o in out) <= 30000 * 0.05 * 5


def test_held_name_is_net_traded_not_round_tripped():
    out = T.compute_targets(["A", "B"], Decimal("30000"),
                            {"A": Decimal("100"), "B": Decimal("100")}, {"A": 10}, CFG)
    a = [o for o in out if o.security_id == "A"]
    assert len(a) == 1 and a[0].side == "buy" and a[0].shares == 5  # 목표 15, 보유 10


def test_dropped_name_is_fully_sold_and_ordered_before_any_buy():
    out = T.compute_targets(["A"], Decimal("30000"),
                            {"A": Decimal("100"), "Z": Decimal("50")}, {"Z": 20}, CFG)
    assert out[0] == T.TargetOrder("Z", "sell", 20, -1)
    assert all(o.side == "buy" for o in out[1:])


def test_buys_are_emitted_in_signal_rank_order():
    ranked = ["A", "B", "C"]
    px = {s: Decimal("100") for s in ranked}
    out = [o for o in T.compute_targets(ranked, Decimal("30000"), px, {}, CFG) if o.side == "buy"]
    assert [o.security_id for o in out] == ranked


def test_zero_selected_names_returns_no_orders_rather_than_erroring():
    assert T.compute_targets([], Decimal("30000"), {}, {}, CFG) == []


def test_stock_too_expensive_for_one_share_is_omitted():
    out = T.compute_targets(["EXP"], Decimal("30000"), {"EXP": Decimal("9999")}, {}, CFG)
    assert out == []  # 5% = $1500 < 1주 가격


def test_more_than_fifty_names_are_truncated_to_the_cap():
    ranked = [f"S{i}" for i in range(80)]
    px = {s: Decimal("10") for s in ranked}
    out = T.compute_targets(ranked, Decimal("30000"), px, {}, CFG)
    assert len({o.security_id for o in out}) == 50
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/unit/test_targets.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'momentum.portfolio.targets'`

- [ ] **Step 3: 최소 구현**

```python
# src/momentum/portfolio/targets.py
from __future__ import annotations

from decimal import Decimal
from typing import Literal, NamedTuple

from momentum.config import ExecutionCfg


class TargetOrder(NamedTuple):
    security_id: str
    side: Literal["buy", "sell"]
    shares: int
    rank: int


def compute_targets(
    ranked: list[str],
    nav: Decimal,
    prev_close: dict[str, Decimal],
    holdings: dict[str, int],
    cfg: ExecutionCfg,
) -> list[TargetOrder]:
    selected = ranked[: cfg.max_positions]
    orders: list[TargetOrder] = []

    # 1) 선정에서 빠진 보유 종목은 전량 매도. 매도가 항상 먼저다.
    for sid, qty in holdings.items():
        if sid not in selected and qty > 0:
            orders.append(TargetOrder(sid, "sell", qty, -1))

    if not selected:
        return orders

    weight = min(Decimal(1) / Decimal(len(selected)), Decimal(str(cfg.max_weight)))
    investable = nav * (Decimal(1) - Decimal(str(cfg.cash_buffer)))

    for rank, sid in enumerate(selected):
        px = prev_close.get(sid)
        if px is None or px <= 0:
            continue
        target = int((investable * weight) // px)
        delta = target - holdings.get(sid, 0)
        if delta > 0:
            orders.append(TargetOrder(sid, "buy", delta, rank))
        elif delta < 0:
            orders.insert(0, TargetOrder(sid, "sell", -delta, -1))
    return orders
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/unit/test_targets.py -v`
Expected: PASS (9 passed)

- [ ] **Step 5: 커밋**

```bash
git add src/momentum/portfolio/targets.py tests/unit/test_targets.py
git commit -m "feat(targets): prev-close-only sizing with weight cap, net trading, sell-first ordering"
```

---

### Task 9: 체결 엔진과 원장 — 센트 단위 회계

**Files:**
- Create: `src/momentum/execution/engine.py`, `src/momentum/ledger/ledger.py`
- Test: `tests/unit/test_engine.py`, `tests/golden/test_ledger_handcalc.py`
- Create: `tests/fixtures/handcalc_ledger.csv`

**Interfaces:**
- Consumes: `TargetOrder`, `costs.apply_caps`, `costs.one_way_cost`, `corporate_actions`
- Produces:
  - `execute(orders: list[TargetOrder], opens: dict[str, Decimal | None], adv: dict[str, float], cash: Decimal, cfg: ExecutionCfg) -> list[Fill]`
  - `class Fill(NamedTuple): security_id: str; side: str; shares: int; price: Decimal; cost: Decimal; status: str` — `status ∈ {"filled", "cancelled_no_open", "reduced_participation", "reduced_cash"}`
  - `class Ledger`: `Ledger(starting_cash: Decimal)`, 메서드 `apply_corporate_actions(cas, d)`, `apply_fills(fills, d)`, `mark_to_market(closes, d)`, 속성 `cash`, `positions: dict[str, int]`, `positions_value`, `dividend_receivable`, `nav`
- 불변식: `cash + positions_value + dividend_receivable == nav` (센트 정확), `cash >= 0`

**처리 순서(고정):** 기업행위 → 매도 체결 → 가용 현금 재계산 → 매수 체결(순위순, 현금 부족 시 수량 축소) → 종가 평가.

- [ ] **Step 1: 손계산 골든 픽스처 작성**

`tests/fixtures/handcalc_ledger.csv` — 한 달치 시나리오를 **손으로 계산해** 날짜별 기대 `cash, positions_value, dividend_receivable, nav` 를 적는다. 반드시 포함할 사건:

| 사건 | 확인하는 것 |
|---|---|
| (a) 정상 매수 | 비용 차감, 현금 감소 |
| (b) 배당락일 | 미수배당 계상, 순자산 불변 |
| (c) 배당 지급일 | 미수배당 → 현금 전환 |
| (d) 2:1 분할 | 수량 2배, 평단 1/2, 순자산 불변 |
| (e) 시가 결측 | 매수 취소(`cancelled_no_open`), 전일 종가로 체결하지 않음 |
| (f) 참여 한도 초과 | 수량 축소(`reduced_participation`) |
| (g) 상장폐지 | 청산가 부재 시 포지션 유지, 성공 체결 생성 금지 |
| (h) 시가 갭으로 현금 부족 | 순위 낮은 종목부터 수량 축소, 음수 현금 없음 |

- [ ] **Step 2: 골든 테스트를 작성하고 실패 확인**

```python
# tests/golden/test_ledger_handcalc.py
import csv
import pathlib
from decimal import Decimal

from momentum.ledger.ledger import Ledger

FIX = pathlib.Path("tests/fixtures/handcalc_ledger.csv")


def _rows():
    return list(csv.DictReader(FIX.open(encoding="utf-8")))


def test_ledger_matches_hand_calculation_to_the_cent():
    led = Ledger(starting_cash=Decimal("30000.00"))
    for row in _rows():
        led.step(row)
        assert led.cash == Decimal(row["cash"]), row["session_date"]
        assert led.dividend_receivable == Decimal(row["dividend_receivable"])
        assert led.nav == Decimal(row["nav"])


def test_accounting_identity_holds_every_single_day():
    led = Ledger(starting_cash=Decimal("30000.00"))
    for row in _rows():
        led.step(row)
        assert led.cash + led.positions_value + led.dividend_receivable == led.nav


def test_cash_never_goes_negative():
    led = Ledger(starting_cash=Decimal("30000.00"))
    for row in _rows():
        led.step(row)
        assert led.cash >= 0
```

```python
# tests/unit/test_engine.py
from decimal import Decimal

from momentum.config import ExecutionCfg
from momentum.execution.engine import execute
from momentum.portfolio.targets import TargetOrder

CFG = ExecutionCfg()


def test_missing_open_cancels_the_buy_instead_of_using_prior_close():
    fills = execute([TargetOrder("A", "buy", 10, 0)], {"A": None}, {"A": 1e9},
                    Decimal("30000"), CFG)
    assert fills[0].status == "cancelled_no_open" and fills[0].shares == 0


def test_insufficient_cash_reduces_the_lowest_ranked_buy_first():
    orders = [TargetOrder("A", "buy", 10, 0), TargetOrder("B", "buy", 10, 1)]
    opens = {"A": Decimal("100"), "B": Decimal("100")}
    fills = execute(orders, opens, {"A": 1e9, "B": 1e9}, Decimal("1500"), CFG)
    by = {f.security_id: f for f in fills}
    assert by["A"].shares == 10
    assert by["B"].shares < 10 and by["B"].status == "reduced_cash"


def test_execution_never_produces_negative_cash():
    orders = [TargetOrder("A", "buy", 1000, 0)]
    fills = execute(orders, {"A": Decimal("100")}, {"A": 1e9}, Decimal("500"), CFG)
    spent = sum(f.shares * f.price + f.cost for f in fills)
    assert spent <= Decimal("500")


def test_sells_are_executed_before_buys_so_their_proceeds_are_usable():
    orders = [TargetOrder("Z", "sell", 10, -1), TargetOrder("A", "buy", 10, 0)]
    opens = {"Z": Decimal("100"), "A": Decimal("100")}
    fills = execute(orders, opens, {"Z": 1e9, "A": 1e9}, Decimal("0"), CFG)
    assert [f.security_id for f in fills][0] == "Z"
    assert next(f for f in fills if f.security_id == "A").shares > 0
```

Run: `uv run pytest tests/golden tests/unit/test_engine.py -v`
Expected: FAIL

- [ ] **Step 3: 구현**

`execute` 는 위 처리 순서를 따르고, `Ledger` 는 `Decimal` 로만 금액을 다룬다. 배당은 `ex_date` 에 `dividend_receivable += shares * cash_amount`, `pay_date` 에 현금 전환. 분할은 `shares *= ratio`, `cost_basis /= ratio` 로 순자산 불변.

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/golden tests/unit/test_engine.py -v`
Expected: PASS

- [ ] **Step 5: 결정론 테스트 추가**

```python
# tests/unit/test_determinism.py
def test_identical_inputs_produce_byte_identical_output(tmp_path):
    from momentum.cli import run_backtest
    run_backtest(seed=7, phase="development", out=tmp_path / "a")
    run_backtest(seed=7, phase="development", out=tmp_path / "b")
    for name in ("orders.csv", "fills.csv", "positions.csv", "cash.csv", "equity.csv"):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()
```

(이 테스트는 Task 10에서 `cli.run_backtest` 가 생긴 뒤 통과한다. 지금은 `pytest.mark.xfail(strict=True)` 로 등록하고 Task 10 Step 5에서 마크를 제거한다.)

- [ ] **Step 6: 커밋**

```bash
git add src/momentum/execution/engine.py src/momentum/ledger tests/unit/test_engine.py tests/golden tests/fixtures
git commit -m "feat(ledger): cent-exact accounting with dividends, splits, and fill failures"
```

---

### Task 10: H4 신호와 첫 엔드투엔드 실행

H4를 먼저 하는 이유는 원본 계획과 같다 — 월간 리밸런싱이라 사건이 가장 적고, 문헌 근거가 가장 단단하다.

**Files:**
- Create: `src/momentum/signals/base.py`, `src/momentum/signals/h4.py`, `src/momentum/cli.py`, `src/momentum/runs/registry.py`
- Test: `tests/unit/test_h4.py`, `tests/golden/test_h4_signal_handcalc.py`, `tests/unit/test_cli.py`
- Create: `tests/fixtures/h4_signal_case.csv`

**Interfaces:**
- Consumes: `AsOfFrame`, `TradingCalendar`, `build_universe`, `compute_targets`, `execute`, `Ledger`
- Produces:
  - `class RankTable(NamedTuple): frame: pl.DataFrame; unavailable: bool = False; reason: str | None = None` — `frame` 컬럼 `hypothesis_id, security_id, asof, score, rank`
  - `class Signal(Protocol): def rank(self, asof: date, universe: pl.DataFrame, prices: AsOfFrame) -> RankTable`
  - `REGISTRY: dict[str, Signal]`
  - `run_backtest(*, hypothesis: str, phase: Phase, data: str, out: Path, seed: int) -> Path`
  - `class HoldoutLockedError(RuntimeError)`
  - CLI: `momentum run --hypothesis H4 --phase development --data synth:seed=7 --out runs/x`

- [ ] **Step 1: 산식 골든 픽스처로 "11개월 누적"을 숫자로 못박는다**

`tests/fixtures/h4_signal_case.csv`: 한 종목의 월말 TR 지수 13개 값과 손계산 기대 score.

```python
# tests/golden/test_h4_signal_handcalc.py
from datetime import date

from momentum.signals.h4 import h4_score


def test_h4_score_is_tr_from_twelve_months_ago_to_one_month_ago():
    # TR(1999-12-31)=100, TR(2000-11-30)=110 → score = 0.10
    # asof = 2000-12-29 (직전 한 달 제외 → 실제 누적 구간은 11개월)
    assert round(h4_score(tr_by_month_end, asof=date(2000, 12, 29)), 10) == round(110 / 100 - 1, 10)


def test_the_most_recent_month_is_excluded_so_a_december_spike_cannot_move_the_score():
    spiked = dict(tr_by_month_end, **{date(2000, 12, 29): 165.0})
    assert h4_score(spiked, asof=date(2000, 12, 29)) == h4_score(tr_by_month_end, asof=date(2000, 12, 29))


def test_insufficient_history_yields_no_score_rather_than_a_partial_window():
    assert h4_score(short_history, asof=date(2000, 3, 31)) is None
```

```python
# tests/unit/test_h4.py
def test_h4_selects_top_ten_percent_without_a_positive_return_filter():
    # 전 종목이 음수 수익이어도 상위 10%는 선정되어야 한다 (H4는 양의수익 필터 없음)
    rt = REGISTRY["H4"].rank(asof, universe_of_100_all_negative, prices)
    assert rt.frame.height == 10


def test_ties_are_broken_by_security_id_ascending():
    assert rt.frame.filter(pl.col("score") == tied_score)["security_id"].to_list() == sorted(...)


def test_h4_only_reads_data_visible_at_the_observation_date():
    prices = AsOfFrame(frame_with_late_published_rows)
    REGISTRY["H4"].rank(asof, universe, prices)
    assert prices.raw_access_count == 0
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/unit/test_h4.py tests/golden/test_h4_signal_handcalc.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'momentum.signals.h4'`

- [ ] **Step 3: 구현**

`h4.py` 는 `prices.visible_at(asof)` 로만 데이터를 읽는다. 상위 `top_pct` 컷, 양의수익 필터 없음, 동률은 `security_id` 오름차순. 이력 부족 종목은 `score=None` 으로 제외한다.

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/unit/test_h4.py tests/golden/test_h4_signal_handcalc.py -v`
Expected: PASS

- [ ] **Step 5: 엔드투엔드 CLI 테스트를 통과시키고 Task 9의 xfail 마크를 제거**

```python
# tests/unit/test_cli.py
import pytest

from momentum.cli import HoldoutLockedError, main


def test_h4_runs_end_to_end_on_the_synthetic_market(tmp_path):
    main(["run", "--hypothesis", "H4", "--phase", "development",
          "--data", "synth:seed=7", "--out", str(tmp_path)])
    for f in ("orders.csv", "fills.csv", "positions.csv", "cash.csv", "equity.csv", "run.json"):
        assert (tmp_path / f).exists()


def test_every_fill_is_traceable_back_to_its_signal_and_universe_row(tmp_path):
    main(["run", "--hypothesis", "H4", "--phase", "development",
          "--data", "synth:seed=7", "--out", str(tmp_path)])
    fills = pl.read_csv(tmp_path / "fills.csv")
    signals = pl.read_csv(tmp_path / "signals.csv")
    assert fills.join(signals, on=["security_id", "asof"], how="anti").height == 0


def test_holdout_phase_is_refused_without_a_frozen_manifest(tmp_path):
    with pytest.raises(HoldoutLockedError):
        main(["run", "--hypothesis", "H4", "--phase", "holdout", "--out", str(tmp_path)])


def test_run_json_records_hashes_and_holdout_access_flag(tmp_path):
    main(["run", "--hypothesis", "H4", "--phase", "development",
          "--data", "synth:seed=7", "--out", str(tmp_path)])
    meta = json.loads((tmp_path / "run.json").read_text())
    assert set(meta) >= {"run_id", "phase", "config_hash", "data_hash",
                         "code_commit", "seed", "started_at", "accessed_holdout"}
    assert meta["accessed_holdout"] is False
```

Run: `uv run pytest tests/unit/test_cli.py tests/unit/test_determinism.py -v`
Expected: PASS

- [ ] **Step 6: 커밋**

```bash
git add src/momentum/signals src/momentum/cli.py src/momentum/runs tests
git commit -m "feat(h4): 12-1 momentum signal and first end-to-end backtest run"
```

---

### Task 11: H1 · H2 · H3 확장

**Files:**
- Create: `src/momentum/signals/h1.py`, `h2.py`, `h3.py`
- Test: `tests/unit/test_h1.py`, `tests/unit/test_h2.py`, `tests/unit/test_h3.py`, `tests/property/test_engine_invariance.py`

**Interfaces:**
- Consumes: Task 10의 `Signal` 프로토콜과 `RankTable` (**변경 없음**)
- Produces: `REGISTRY` 에 `H1`, `H2`, `H3` 등록. H3는 시점별 발행주식 수가 없으면 `RankTable(frame=empty, unavailable=True, reason="shares_outstanding_not_point_in_time")` 을 반환한다 — 예외가 아니라 **1급 결과 객체**여야 보고서가 "0% 수익"이 아닌 "평가 보류"로 렌더한다.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/unit/test_h3.py
def test_h3_returns_unavailable_when_shares_history_has_no_available_at():
    rt = REGISTRY["H3"].rank(asof, universe, prices_without_pit_shares)
    assert rt.unavailable is True
    assert rt.reason == "shares_outstanding_not_point_in_time"


def test_h3_never_substitutes_current_shares_for_historical_ones():
    rt = REGISTRY["H3"].rank(asof, universe, prices_with_only_current_shares)
    assert rt.unavailable is True
    assert rt.frame.height == 0


def test_h3_turnover_is_the_sum_of_daily_volume_over_point_in_time_shares():
    # 거래대금도, 거래량 급증률도 아니다
    expected = sum(v / s for v, s in zip(vols, shares_pit))
    assert round(score_for("SEC0001"), 8) == round(expected, 8)


def test_h3_selects_the_intersection_of_return_top_10pct_and_turnover_top_30pct():
    assert set(rt.frame["security_id"]) == set(ret_top_10) & set(turnover_top_30)


def test_h3_applies_the_positive_return_filter():
    assert (rt.frame["score"] > 0).all()
```

```python
# tests/unit/test_h1.py
def test_h1_signal_is_the_one_day_total_return_at_the_close_of_t():
    assert round(score, 10) == round(tr[t] / tr[t_minus_1] - 1, 10)


def test_h1_enters_at_the_next_open_and_exits_at_the_one_after():
    assert entry.session_date == t_plus_1 and entry.price_source == "open_raw"
    assert exit_.session_date == t_plus_2 and exit_.price_source == "open_raw"


def test_h1_never_buys_at_the_signal_day_close():
    assert all(f.session_date > f.asof for f in fills)


def test_h1_keeps_a_reselected_position_instead_of_round_tripping():
    assert [f for f in fills if f.security_id == reselected and f.side == "sell"] == []
```

```python
# tests/unit/test_h2.py
def test_h2_signal_spans_last_session_of_week_over_last_session_of_prior_week():
    assert obs_date == date(2000, 6, 30) and base_date == date(2000, 6, 23)


def test_h2_enters_on_the_first_session_of_the_next_week():
    assert entry.session_date == date(2000, 7, 3)


def test_h2_holding_period_is_one_week_not_the_best_of_one_two_four_twelve():
    assert exit_.session_date == date(2000, 7, 10)
```

```python
# tests/property/test_engine_invariance.py
def test_identical_rank_tables_produce_identical_ledgers_across_hypotheses():
    """가설이 달라도 순위표가 같으면 체결·회계 결과는 같아야 한다.
    가설별 복제 엔진이 생기면 이 테스트가 깨진다."""
    ranks = fixed_rank_table()
    assert run_with("H1", ranks).equity.equals(run_with("H4", ranks).equity)
```

추가로 경계 사례를 각 가설에 공통 적용한다: 선정 0개, 20개 미만, 고가 주식으로 정수 수량 0주, 거래정지, 동률.

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/unit/test_h1.py tests/unit/test_h2.py tests/unit/test_h3.py tests/property -v`
Expected: FAIL

- [ ] **Step 3: 구현** — 세 파일 모두 `Signal` 프로토콜만 구현한다. 체결·회계 코드는 한 줄도 추가하지 않는다.

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/unit tests/property -v`
Expected: PASS

- [ ] **Step 5: 리밸런싱 횟수 대사**

```python
def test_scheduled_and_actual_rebalance_counts_differ_only_for_logged_reasons():
    scheduled = len(cal.rebalance_dates("monthly", start, end))
    actual = equity["rebalanced"].sum()
    assert scheduled - actual == len(exception_log)   # 모든 차이가 설명되어야 한다
```

- [ ] **Step 6: 커밋**

```bash
git add src/momentum/signals tests
git commit -m "feat(signals): add H1, H2, H3 with a first-class unavailable result for H3"
```

---

### Task 12: 비교군과 성과 지표

**Files:**
- Create: `src/momentum/evaluate/benchmarks.py`, `src/momentum/evaluate/metrics.py`
- Test: `tests/unit/test_metrics.py`, `tests/unit/test_benchmarks.py`

**Interfaces:**
- Consumes: `equity.csv`(일별 `nav`), 유니버스, 비용 설정
- Produces:
  - `equal_weight_research_benchmark(universe_by_date, prices, cfg) -> pl.DataFrame` — 1차 비교군. **전략과 동일한 리밸런싱 일정·현금 비중·비용**을 적용하되 분할주 가정(정수 주식 제약 없음).
  - `buy_and_hold(symbol: str, prices) -> pl.DataFrame` — SPY(2차), QQQ(보조)
  - `random_portfolios(n_names: int, iters: int, seed: int, ...) -> pl.DataFrame` — 진단용
  - `summary(equity, benchmark, cfg) -> Metrics` — `cagr_gross, cagr_net, sharpe, max_drawdown, recovery_days, monthly_win_rate, active_return_mean, information_ratio, active_ci, turnover, total_cost, beta, breakeven_cost_bps, top_contributors`

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/unit/test_metrics.py
from pytest import approx

from momentum.evaluate.metrics import cagr, max_drawdown_and_recovery, summary_from_series


def test_cagr_of_doubling_over_exactly_two_years():
    assert cagr(start=1.0, end=2.0, years=2.0) == approx(2 ** 0.5 - 1)


def test_max_drawdown_and_recovery_are_measured_in_sessions():
    mdd, recovery = max_drawdown_and_recovery([100, 120, 60, 90, 120, 130])
    assert mdd == approx(-0.5)
    assert recovery == 2  # 저점(index 2) → 전고점 회복(index 4)


def test_an_unrecovered_drawdown_reports_none_not_zero():
    _, recovery = max_drawdown_and_recovery([100, 120, 60, 70])
    assert recovery is None


def test_active_return_is_strategy_minus_benchmark_after_costs():
    m = summary_from_series(strategy=[0.01, 0.02], benchmark=[0.005, 0.005])
    assert m.active_return_mean == approx(0.01)


def test_breakeven_cost_is_the_bps_at_which_net_cagr_reaches_zero():
    # 왕복 회전율과 총수익이 주어지면 손익분기 편도 bp가 유일하게 결정된다
    assert summary_from_series(...).breakeven_cost_bps == approx(expected_bps, rel=1e-6)
```

```python
# tests/unit/test_benchmarks.py
def test_research_benchmark_uses_the_same_rebalance_schedule_as_the_strategy():
    assert bench.rebalance_dates == strategy.rebalance_dates


def test_research_benchmark_pays_transaction_costs_too():
    assert bench.total_cost > 0


def test_research_benchmark_is_equal_weighted_over_the_eligible_universe():
    assert bench.weights_on(date(2000, 6, 30)).std() == approx(0.0, abs=1e-12)


def test_random_portfolios_are_reproducible_and_hold_the_same_number_of_names():
    a = random_portfolios(n_names=50, iters=100, seed=3, ...)
    b = random_portfolios(n_names=50, iters=100, seed=3, ...)
    assert a.equals(b)


def test_benchmark_weights_are_never_set_using_future_volatility():
    import inspect
    assert "realized_vol" not in inspect.getsource(equal_weight_research_benchmark)
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/unit/test_metrics.py tests/unit/test_benchmarks.py -v`
Expected: FAIL

- [ ] **Step 3: 구현**

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/unit/test_metrics.py tests/unit/test_benchmarks.py -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add src/momentum/evaluate tests/unit/test_metrics.py tests/unit/test_benchmarks.py
git commit -m "feat(evaluate): research/SPY/random benchmarks and performance metrics"
```

---

### Task 13: 부트스트랩 · 다중검정 · 판정 · holdout 동결

**Files:**
- Create: `src/momentum/evaluate/bootstrap.py`, `multiple_testing.py`, `decide.py`; 확장: `src/momentum/runs/registry.py`
- Test: `tests/unit/test_bootstrap.py`, `test_multiple_testing.py`, `test_decide.py`, `test_holdout_lock.py`
- Create: `docs/null_calibration.md`

**Interfaces:**
- Consumes: 일별 활성수익 시계열. **모든 전략에 공통 날짜 블록**을 재표집한다.
- Produces:
  - `block_bootstrap_pvalue(active: np.ndarray, block: int, iters: int, seed: int) -> BootResult` — `BootResult(pvalue: float | None, ci_low, ci_high, se, blocks_used: int, insufficient: bool)`
  - `holm(pvalues: dict[str, float | None]) -> dict[str, float]` — `None`(미검정)은 **p=1 로 보수적 배정**하되 가족 크기는 4로 유지
  - `decide(...) -> Verdict(data_status, research_status, economic_status, stability_status, risk_fit, final)`
  - `freeze_manifest(cfg, data_hash, code_commit) -> Path`, `release_holdout(manifest, cfg) -> RunResult`, `class HoldoutLockedError`

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/unit/test_bootstrap.py
import numpy as np

from momentum.evaluate.bootstrap import block_bootstrap_pvalue


def test_pure_noise_is_not_significant():
    x = np.random.default_rng(1).normal(0, 0.01, 2520)
    assert block_bootstrap_pvalue(x, block=63, iters=2000, seed=1).pvalue > 0.05


def test_strong_positive_drift_is_significant():
    x = np.random.default_rng(1).normal(0.001, 0.005, 2520)
    assert block_bootstrap_pvalue(x, block=63, iters=2000, seed=1).pvalue < 0.05


def test_the_test_is_one_sided_so_strong_negative_drift_is_not_significant():
    x = np.random.default_rng(1).normal(-0.001, 0.005, 2520)
    assert block_bootstrap_pvalue(x, block=63, iters=2000, seed=1).pvalue > 0.5


def test_result_is_reproducible_with_the_same_seed():
    x = np.random.default_rng(2).normal(0, 0.01, 1000)
    assert (block_bootstrap_pvalue(x, 63, 2000, seed=5).pvalue
            == block_bootstrap_pvalue(x, 63, 2000, seed=5).pvalue)


def test_zero_standard_error_reports_insufficient_rather_than_a_pvalue():
    r = block_bootstrap_pvalue(np.zeros(500), 63, 100, seed=1)
    assert r.insufficient is True and r.pvalue is None


def test_series_shorter_than_two_blocks_is_insufficient():
    assert block_bootstrap_pvalue(np.ones(80) * 0.001, 63, 100, seed=1).insufficient is True


def test_pvalue_is_never_exactly_zero_because_of_the_plus_one_correction():
    x = np.random.default_rng(3).normal(0.01, 0.001, 2520)
    assert block_bootstrap_pvalue(x, 63, 1000, seed=1).pvalue >= 1 / 1001
```

```python
# tests/unit/test_multiple_testing.py
from momentum.evaluate.multiple_testing import holm


def test_holm_is_never_less_conservative_than_the_raw_pvalue():
    raw = {"H1": 0.01, "H2": 0.02, "H3": 0.03, "H4": 0.04}
    adj = holm(raw)
    assert all(adj[k] >= v for k, v in raw.items())


def test_smallest_pvalue_is_multiplied_by_the_family_size():
    assert holm({"H1": 0.001, "H2": 0.4, "H3": 0.5, "H4": 0.02})["H1"] == 0.004


def test_untested_hypothesis_gets_one_but_the_family_size_stays_four():
    adj = holm({"H1": 0.001, "H2": 0.4, "H3": None, "H4": 0.02})
    assert adj["H3"] == 1.0
    assert adj["H1"] == 0.004  # 3으로 줄이지 않는다


def test_adjusted_pvalues_are_monotone_in_the_raw_ordering():
    adj = holm({"A": 0.01, "B": 0.02, "C": 0.03, "D": 0.04})
    assert adj["A"] <= adj["B"] <= adj["C"] <= adj["D"]
```

```python
# tests/unit/test_decide.py
def test_missing_data_yields_pending_evaluation_not_no_edge():
    v = decide(data_ok=False, ...)
    assert v.final == "평가 보류"


def test_insufficient_sample_yields_no_edge_confirmed_distinct_from_pending():
    v = decide(data_ok=True, bootstrap_insufficient=True, ...)
    assert v.final == "우위 미확인"


def test_positive_active_return_and_holm_p_below_alpha_is_a_statistical_edge():
    v = decide(data_ok=True, active_mean=0.0003, holm_p=0.01, ...)
    assert v.research_status == "통계적 우위 확인"


def test_risk_fit_is_unevaluated_by_default_because_capital_is_undecided():
    assert decide(...).risk_fit == "미평가"


def test_paper_candidate_requires_positive_active_return_in_both_windows():
    assert decide(wf_active=0.001, holdout_active=-0.001, ...).final != "모의운용 후보"
```

```python
# tests/unit/test_holdout_lock.py
import pytest

from momentum.evaluate.decide import HoldoutLockedError
from momentum.runs.registry import freeze_manifest, last_run, release_holdout


def test_holdout_without_a_manifest_is_refused():
    with pytest.raises(HoldoutLockedError):
        release_holdout(manifest=None, cfg=cfg)


def test_holdout_run_is_recorded_and_a_second_release_is_refused(tmp_path):
    m = freeze_manifest(cfg, data_hash="d", code_commit="c")
    release_holdout(m, cfg)
    assert last_run().accessed_holdout is True
    with pytest.raises(HoldoutLockedError):
        release_holdout(m, cfg)


def test_a_config_change_after_freezing_invalidates_the_manifest(tmp_path):
    m = freeze_manifest(cfg, "d", "c")
    with pytest.raises(HoldoutLockedError):
        release_holdout(m, cfg.model_copy(update={"hash": "different"}))
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/unit/test_bootstrap.py tests/unit/test_multiple_testing.py tests/unit/test_decide.py tests/unit/test_holdout_lock.py -v`
Expected: FAIL

- [ ] **Step 3: 구현**

부트스트랩: 중심화(`x - mean(x)`) 후 블록 재표집, 학생화 통계 `t* = mean(x*) / se(x*)`, 관측 통계 `t_obs = mean(x) / se(x)`, 단측 p = `(1 + #{t* >= t_obs}) / (iters + 1)`. **원시 부트스트랩의 양수 비율을 p값으로 쓰지 않는다.**

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/unit -v`
Expected: PASS

- [ ] **Step 5: 합성 귀무 자료로 1종 오류율 점검 — 최종 개봉 전 필수**

Task 2의 `generate_market(with_events=False)` 은 드리프트가 거의 0인 시장이다. 여기서 서로 다른 seed 200개로 4가설을 돌려 **명목 5%에서 실제 기각률이 대략 5%인지** 확인하고 `docs/null_calibration.md` 에 기록한다.

```python
# tests/property/test_null_calibration.py (느린 테스트, -m slow)
def test_type_one_error_rate_is_near_nominal_on_a_null_market():
    rejects = sum(run_null(seed=s).holm_p < 0.05 for s in range(200))
    assert 0.01 <= rejects / 200 <= 0.12   # 보수적 허용 범위, 결과는 문서에 기록
```

- [ ] **Step 6: 커밋**

```bash
git add src/momentum/evaluate src/momentum/runs docs/null_calibration.md tests
git commit -m "feat(validation): studentized block bootstrap, Holm correction, verdict, holdout lock"
```

---

### Task 14: 보고서, 재현 패키지, 데이터 소스 매트릭스

**Files:**
- Create: `src/momentum/report/html.py`, `src/momentum/report/templates/report.html.j2`, `docs/data_source_matrix.md`, `README.md`
- Test: `tests/unit/test_report.py`

**Interfaces:**
- Consumes: `Verdict`, `Metrics`, `equity.csv`, `run.json`
- Produces: `render(run_dir: Path) -> Path` → `report.html`, `metrics.csv`

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/unit/test_report.py
import json

from momentum.report.html import render


def test_report_numbers_match_the_ledger_exactly(run_dir):
    html = render(run_dir).read_text(encoding="utf-8")
    final_nav = read_equity(run_dir)[-1]["nav"]
    assert f"{final_nav:,.2f}" in html


def test_an_unavailable_hypothesis_renders_as_pending_not_as_zero_return(run_dir_h3_unavailable):
    card = extract_card(render(run_dir_h3_unavailable).read_text(encoding="utf-8"), "H3")
    assert "평가 보류" in card
    assert "0.00%" not in card
    assert "shares_outstanding_not_point_in_time" in card


def test_report_shows_all_five_verdict_axes_and_the_breakeven_cost(run_dir):
    html = render(run_dir).read_text(encoding="utf-8")
    for token in ("데이터 상태", "연구 상태", "경제성 상태", "안정성 상태",
                  "위험 적합성", "손익분기 편도 비용"):
        assert token in html


def test_report_embeds_the_run_manifest_hashes(run_dir):
    html = render(run_dir).read_text(encoding="utf-8")
    meta = json.loads((run_dir / "run.json").read_text())
    assert meta["config_hash"] in html and meta["code_commit"] in html


def test_report_separates_research_edge_from_personal_risk_suitability(run_dir):
    html = render(run_dir).read_text(encoding="utf-8")
    assert "위험 적합성: 미평가" in html
    assert "참고 표" in html  # 낙폭 20/30/40% 는 참고용으로만


def test_a_run_where_all_four_hypotheses_fail_still_renders_a_complete_report(run_dir_all_fail):
    html = render(run_dir_all_fail).read_text(encoding="utf-8")
    assert "우위 미확인" in html and "오류" not in html
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/unit/test_report.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'momentum.report.html'`

- [ ] **Step 3: 구현** — Jinja2 단일 템플릿. 첫 화면에 4가설 카드(상태·순 CAGR·활성수익·낙폭·비용 민감도·판정 이유), 이어서 순수익 곡선, 낙폭, 연도별 수익, 비용 스트레스, 신뢰구간, 데이터 품질 요약.

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/unit/test_report.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: `docs/data_source_matrix.md` 작성 — 명세 1단계 산출물**

후보(CRSP / Nasdaq Data Link / Polygon / Sharadar / 사용자 보유 파일)마다 다음을 표로 정리한다: 실제 접근 가능 여부, 역사 구간, **원시 시가**, **상장폐지 이력**, **영구 종목 ID**, 배당·분할, **당시 발행주식 수와 available_at**, 정정 이력, 이용 조건.

- **확인된 공식 자료만 기록한다. 요금은 추정하지 않는다.**
- 가설별 충족표를 만들어 H3 진행 가능 여부를 여기서 결정한다.
- 유료 결제나 계정 계약은 이 문서만으로 실행하지 않는다.

- [ ] **Step 6: 재현성 확인**

```bash
uv run momentum run --hypothesis H4 --phase development --data synth:seed=7 --out runs/a
uv run momentum run --hypothesis H4 --phase development --data synth:seed=7 --out runs/b
diff -r runs/a runs/b   # run.json 의 started_at, run_id 외 차이 0
```

- [ ] **Step 7: 커밋**

```bash
git add src/momentum/report docs README.md tests/unit/test_report.py
git commit -m "feat(report): static HTML verdict report and reproducibility package"
```

---

### Task 15 (조건부): 실데이터 어댑터

**선행 조건:** Task 14 Step 5의 `data_source_matrix.md` 에서 실제 접근 가능한 공급자가 확정되었을 때만 착수한다.

**Files:**
- Create: `src/momentum/data/ingest.py`, `normalize.py`, `quality.py`
- Test: `tests/unit/test_normalize.py`, `tests/unit/test_quality.py`

**Interfaces:**
- Produces: `load_market(source: str) -> SynthMarket` 과 **동일한 5개 프레임**. 엔진은 데이터 출처를 모른다 — 그래서 Task 3~14 코드는 한 줄도 바뀌지 않는다.

- [ ] **Step 1: 실패하는 테스트**

```python
def test_ticker_change_is_linked_by_permanent_id_not_by_ticker():
    assert normalized.filter(pl.col("security_id") == "PERM1").height == full_history_len


def test_volume_and_shares_use_the_same_split_basis():
    # 분할 전후로 turnover 가 불연속 점프하지 않아야 한다
    assert abs(turnover_after - turnover_before) / turnover_before < 0.1


def test_duplicate_security_date_keys_are_zero():
    assert normalized.group_by(["security_id", "session_date"]).len().filter(
        pl.col("len") > 1).height == 0


def test_future_information_joins_are_zero():
    assert normalized.filter(pl.col("available_at") < pl.col("session_date")).height == 0


def test_unexplained_missing_rows_are_quarantined_not_dropped():
    r = quality_report(normalized)
    assert r.quarantined_rows > 0 and r.dropped_rows == 0


def test_extreme_returns_are_flagged_with_a_cause_not_silently_removed():
    assert all(i.cause is not None for i in r.issues if i.kind == "extreme_return")
```

- [ ] **Step 2~4:** 실패 확인 → 구현 → 통과
- [ ] **Step 5: 전체 회귀** — `uv run pytest -q` 전부 통과. 합성 데이터 테스트가 하나도 깨지지 않아야 한다(어댑터는 엔진을 건드리지 않는다는 증거).
- [ ] **Step 6: 커밋** — `git commit -m "feat(data): real provider adapter conforming to the synthetic market contract"`

---

## 실행 순서와 실데이터 합류 지점

```
Task 1 ─ 2 ─ 3 ─ 4 ─ 5 ─ 6 ─ 7 ─ 8 ─ 9 ─ 10 ─ 11 ─ 12 ─ 13 ─ 14        [Task 15]
        └──────────── 전부 합성 데이터로 완주 가능 ────────────┘            ▲
                                                                   실데이터 확보 시 여기만 추가
```

실데이터 어댑터가 `SynthMarket` 과 같은 5개 프레임을 반환하기만 하면 엔진은 출처를 모른다. 이것이 "데이터 미확보로 개발이 막히지 않게" 하는 구조적 장치이며, 원본 계획의 "단계별 중단·우회 규칙" 표를 코드 구조로 옮긴 것이다.

## 명세 대비 커버리지 (Self-Review)

| 명세 절 | 대응 Task | 비고 |
|---|---|---|
| §2 네 가설 산식·사전 고정 규칙 | 10, 11 | 골든 픽스처로 산식 고정 |
| §3 유니버스·포트폴리오 | 6, 8 | 50종목 / 5% / N<20 현금 |
| §4 데이터 품질·available_at | 4, 15 | `AsOfFrame` + data_source_matrix |
| §5 거래비용·실행 모형 | 7, 9 | 참여 한도·결측 시가 취소·손익분기 bp |
| §6 비교 기준·성과 지표 | 12 | 연구용 동일가중 + SPY + 무작위 |
| §7 검증 기간·과최적화 방지 | 3, 13 | phase 강제 + 동결 manifest + hook |
| §8 판정 규칙 | 13, 14 | 5축 상태 분리, H3 미검정 p=1 |
| §9 프로그램 구조 | File Structure | 모듈 1:1 대응 |
| §10 0~7단계 완료 기준 | 각 Task 완료 조건 | 코드 작성이 아니라 테스트 통과가 기준 |
| §13 모듈 계약·최소 데이터 표 | 4 | `schema.py` 6개 표 |

**의도적으로 남긴 것:** §8 위험 적합성은 사용자 실투자금·허용 낙폭이 미정이므로 `risk_fit = "미평가"` 고정값으로만 구현한다(Task 13). §10 8단계 전향 모의 관찰은 조건부 후속이므로 이 계획 밖이다. 두 항목 모두 원본 명세가 미확정으로 표시한 것이며, 이 계획이 임의로 값을 정하지 않는다.

## 리스크와 대응

| 리스크 | 관찰되는 신호 | 대응 |
|---|---|---|
| 세션이 길어져 앞 결정을 잊음 | 이미 정한 산식을 다시 묻거나 다르게 구현 | Task 경계에서 세션 종료. 골든 픽스처가 산식의 권위 |
| 편의상 결측을 채움 | 테스트는 통과하는데 수익률이 개선됨 | 결측 대체 금지 테스트를 Task 6·9·15에 명시 배치 |
| 비용 단조성 오판 | "비용 올렸는데 수익 올라감 → 버그" 결론 | 명세대로, 전략 전체에서는 비용이 주문 수량도 바꾸므로 **단일 주문 수준에서만** 단조성 검사 |
| holdout 조기 열람 | — | hook(Task 1) + manifest(Task 13) + `runs.accessed_holdout` 3중 |
| H3 데이터 부재를 실패로 표시 | 보고서에 H3 0% | `unavailable` 1급 객체(Task 11) + 렌더 테스트(Task 14) |
| 결과를 본 뒤 기준 완화 | config 변경 커밋이 결과 커밋 뒤에 옴 | `config_hash` 를 `run.json` 에 기록, manifest 불일치 시 거부 |
