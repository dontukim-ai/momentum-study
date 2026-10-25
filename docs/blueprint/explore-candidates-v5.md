# 탐색 후보 공간 v5: 신호 확장 (2026-10-25 사용자 결정)

작성 2026-10-25. 근거는 v4 판정(explore-candidates-v4 §5.1: 개선 아님, 종목 수 조정 중단)과 전략 대안 조사(strategy-alternatives §2, §5.4)다. 결과를 본 뒤 후보 공간을 바꾸는 제안이라 새 연구로만 적용한다. 결과를 본 뒤 아래 값을 바꾸지 않는다.

## 1. 왜 바꾸는가

- v2~v4의 후보는 모두 20~60일 가격 모멘텀 변형이었다. 종목 수를 5→10으로 늘려도 2022년 낙폭이 거의 같았다(48.6% 대 53.4%). 손실이 개별 종목이 아니라 신호 전체에서 난다.
- 한국 개별 종목은 모멘텀보다 반전이 우세하다는 장기 연구가 있고, 저변동성·MAX 효과도 한국에서 보고됐다(strategy-alternatives §2, §5.1).
- 이번 질문: 같은 연구 절차에서 모멘텀과 다른 신호가 독립 검증을 통과하는가.

## 2. 새 순위 (RES-05b, kis-agent `selection.py`)

| 순위 | 점수 | 적격 조건(기존 선별 외) |
| --- | --- | --- |
| `short_reversal` | −(lookback 수익률). 가장 많이 내린 종목 우선 | lookback 손실이 `min_momentum`(0)보다 커야 함. 아니면 `REVERSAL_BELOW_THRESHOLD` |
| `low_volatility` | −(lookback 일간 수익률 표준편차) | 모멘텀 부호 조건 없음(위험 정렬) |
| `momentum_ex_max` | lookback 수익률 | 모멘텀 > 0, 그리고 lookback 안 최대 일간 수익률 ≤ 25%. 넘으면 `RECENT_MAX_RETURN_ABOVE_LIMIT` |

- 25%는 결과를 보기 전에 정한 상수(`MAX_FILTER_DAILY_RETURN`)다. KRX 일간 제한폭 30% 근처의 급등일이 있는 종목을 뺀다.
- 기본 탐색 공간(`DEFAULT_RANKINGS`)은 기존 3종 그대로다. 새 순위는 정책의 `rankings`에 적어야만 쓰인다. 기존 3종의 점수·특징 출력은 바뀌지 않았다(회귀 테스트).
- kis-agent 소스 해시가 바뀌므로 모든 전략 버전 ID가 이전 연구와 달라진다. 실주문 상한(`REAL_CAPITAL_LIMIT`)과 모의 실행 승인 상한은 바꾸지 않았다.

## 3. 사전 등록 값

| 항목 | v4 | v5 |
| --- | --- | --- |
| 순위 후보 | momentum, risk_adjusted, trend_stability | **momentum, short_reversal, low_volatility, momentum_ex_max** |
| 사이클당 도전자 예산 | 8 | **11** (기간 3 × 순위 4 − 챔피언 1) |
| 그 밖 | 챔피언 momentum/20/top10, 기간 20/40/60, 자본 1억 원, 주문 한도 4,000만 원, 거래대금 하한 100억 원, 매월, 낙폭 한도 25%, 기준 ETF 069500, 주식만, 편도 slippage 25bp | 같음 |

- `risk_adjusted`·`trend_stability`는 v4까지 모멘텀과 같은 계열로 확인됐으므로(v4 사이클 7 승격 뒤 표본 밖 열세) 이번에는 빼고, 같은 예산 상한(12) 안에서 새 신호에 자리를 준다.
- 챔피언은 `momentum/20/top10`으로 시작한다. 새 신호는 기존 승격 절차(개발 선택 → 독립 검증 구간 게이트)로만 챔피언이 될 수 있다.
- 연구 이름 `explore-v5-signals10-1e8`, 새 DB, 검증 예산 8사이클. 실패해도 자동 재실행하지 않는다.

정책 생성:

```
uv run python tools/historical_policy.py --out <dir>/policy.json --top-n 10 \
  --champion-lookback 20 --lookbacks 20,40,60 --max-drawdown 0.25 \
  --decision-cadence monthly --market-etf 069500 --allowed-types STOCK \
  --min-average-turnover 10000000000 --capital 100000000 --max-order-notional 40000000 \
  --rankings momentum,short_reversal,low_volatility,momentum_ex_max --candidate-budget 11
```

v4 정책과의 차이는 `rankings`, `candidate_budget` 두 항목뿐이어야 한다.

## 4. 판정 기준 (실행 전 고정)

1. **신호 채택:** 새 순위 중 하나가 승격되고, 승격 뒤 남은 검증 구간과 관찰 장부에서 같은 실행의 기준 ETF 이상이면 채택 후보로 본다.
2. **연구 개선:** 검증 구간 8개 챔피언 복리가 같은 실행의 기준 ETF 복리 이상이면 개선이다(v4 기준과 같음).
3. **보고만:** 사이클마다 선택된 도전자의 순위 종류, 새 순위 도전자의 검증 구간 수익률, 관찰 장부 낙폭과 상위 5종목 비중.
4. 1·2를 모두 만족하지 못하면 가격 신호만으로는 이 데이터·비용에서 시장 ETF를 넘지 못한다고 기록하고, 시장 추세 필터(A3)나 재무 데이터(B1·B2)로 넘어간다.

## 5. 해석할 때 주의할 점

- 후보가 8→11개로 늘어 개발 구간에서 우연히 좋은 후보를 고를 확률이 커진다. 독립 검증 구간 게이트가 이를 막는 장치다.
- 단기 반전은 회전율이 높을 수 있어 비용(편도 25bp)에 민감하다.
- 데이터 한계는 v3 §4와 같다. 투자 판단에 쓰지 않는다.
