# RES-01: 사전 지정 시장 ETF 기준 전략

범위: 보고 전용 opt-in ETF 기준 전략. `AgentPolicy.market_etf_symbol`을 지정한 새 연구에서만 활성화하며 기본값은 null이다. 기존 현금·적격 동일 비중 보고, 기본 통계 v1, 승격 gate·일정·소비 규칙은 바꾸지 않는다.

정책 JSON의 예:

```json
{"market_etf_symbol": "069500"}
```

위 종목 코드는 설정 예시다. 입력 묶음에 해당 종목의 시점 보존 가격·ETF 메타데이터·기업행동 커버리지가 필요하다. 실제 상품별 비용·수익성 검증을 마쳤다는 뜻이 아니다. 기존 정책 파일에 병합해 새 연구를 등록하며, 결과를 본 뒤 기준 ETF를 교체하지 않는다. 새 필드가 없는 이전 policy JSON은 비활성 기본값으로 읽고 기존 build 변경 거부는 유지한다.

## 평가와 보고

- 모멘텀 순위로 기준 종목을 바꾸지 않는다. 관측 시점에 ETF임을 확인하고 기존 `select_candidates`의 시장·품질·유동성·예산 필터를 재사용한다. 일반 주식이나 다른 ETF로 대체하지 않는다.
- 같은 `CommonEvaluator`에서 전일 정보로 수량을 계획하고 다음 시가, 거래량 cap, 현금/위험 축소, 수수료·세금·slippage, 원장·기업행동을 적용한다. 실제 API·주문은 호출하지 않는다.
- 단일 ETF의 목표는 기존 총노출·종목·주문 상한으로 제한한다. 기본 종목 한도 40%를 완화하지 않는다. **일별 리밸런싱하는 위험 한도 적용 기준 전략**이며, 완전 투자 ETF buy-and-hold 또는 시장 지수 수익률과 다르다. 실제 투자/현금 비중을 함께 해석한다.
- `EvaluationContract` schema v2에는 활성 기준 전략 정의 목록을 고정한다. ETF 종목을 포함한 기준 전략 version hash와 정책이 달라지면 비교 계약/manifest도 달라진다. 같은 정책의 후보와 ETF 결과는 같은 계약을 갖는다.
- 최종(후보 없음 포함)·shadow의 `benchmarks.market_etf`에는 `status`, `symbol`, 성과·낙폭·거래 수·비교 계약·순우위·portfolio_diagnostics·allocation_rule을 기록한다. gate는 이 값을 읽지 않는다.
- 관측 시점 메타데이터·ETF 유형·가격/이력이 없으면 전용 `BenchmarkUnavailable`만 보고 경계에서 처리하여 `UNAVAILABLE`과 사유를 남긴다. 이때 수익률·거래 수·평가 계약이 있는 완성 결과를 만들어 내지 않는다. `reference_evaluation_contract_id`는 비교 상대의 조건만 식별한다.
- 실제 정지·관리 등 기존 필터로 제외되면 그 세션은 새 배분을 하지 않고 `screening_rejection_sessions`를 보고한다. 보유 중 거래 정지는 기존 체결 제약으로 처리하며, 보유 종목 종가 결측·원장 대사 오류는 기존대로 실패한다. 이러한 경제 오류를 UNAVAILABLE로 숨기지 않는다.

## 코드와 검증

변경: [contracts.py](../../src/momentum/agent/contracts.py)의 opt-in 정책, [evaluator.py](../../src/momentum/agent/evaluator.py)의 고정 ETF 선택·계약 정의, [service.py](../../src/momentum/agent/service.py)의 활성 기준 전략/불가 보고, [test_agent_market_etf.py](../../tests/unit/test_agent_market_etf.py)의 신규 합성 회귀 14개(파라미터화 포함).

회귀는 일반 주식의 점수가 더 높아도 지정 ETF만 선택, 같은 평가 계약, 종목별 version 차이, 기존 정책 변경 거부, 기본 비활성·이전 JSON 호환, 잘못된 코드, 일반 주식 대체 거부, 미래 공개 메타데이터 차단, 결측 시가 취소, 기존 주문 상한·수수료 손계산, 필터 제외·현금 비중, 유지/shadow 보고와 gate 비개입, 데이터 없음의 UNAVAILABLE를 검증한다.

검증(오프라인 uv cache 설정 사용):

| 명령 | 결과 |
| --- | --- |
| `uv run pytest -q` | `271 passed, 1 deselected in 76.98s (0:01:16)` — 기존 257 + 신규 14 |
| `uv run mypy src` | `Success: no issues found in 33 source files` |
| `uv run ruff check` | `All checks passed!` |
| KIS 앱 unittest | `Ran 122 tests in 7.613s`, `OK` |
| `git diff --check` | 통과 |

기존 테스트·KIS 앱·미국 엔진/골든·원장·의존성 파일은 수정하지 않았다. 실데이터 검증과 실제 ETF 비용 검증은 후속이다.
