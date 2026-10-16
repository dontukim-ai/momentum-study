# C1~C4 1차 구현 보고

Result: **complete — 첨부 1차 묶음의 합성 구현·회귀 범위**. LOOP-03, RES-01, LOOP-07 전체의 실데이터 완료를 뜻하지 않는다.

기준: PR #2 문서 브랜치 `claude/blueprint-loop-redesign`의 `f3e5653`. 작업 브랜치: `codex/agent-batch1-comparison`. C1 → C2 → C3 → C4 순서로 실패 테스트, 구현, diff 리뷰, 검증 후 로컬 커밋을 쌓았다. push·PR 생성은 하지 않았다.

## Changed files

| 파일 | 목적 |
| --- | --- |
| [evaluator.py](../../src/momentum/agent/evaluator.py) | C1 수량 단계·금액 가중 거래 가능성·turnover 두 분모, C2 불변 평가 계약·동일 조건 검사, C3 같은 엔진의 현금/적격 동일 비중 분기 |
| [service.py](../../src/momentum/agent/service.py) | 개발 fold·최종/스트레스 비교 계약 검사, 기준 전략 최종/유지/shadow 보고, C4 통계 버전·단측 gate·증거 metadata·이전 policy JSON 정규화 비교 |
| [contracts.py](../../src/momentum/agent/contracts.py) | C4 기본 v1, opt-in v2, 최소 tail draw 검증 |
| [stat_policy_check.py](../../tools/stat_policy_check.py) | 평균 0 AR(1) null·seed/재표집 수 안정성·개발 블록 민감도 JSON, 외부 입력/서비스 없음 |
| [test_agent_tradability.py](../../tests/unit/test_agent_tradability.py) | C1 손계산 회귀 8개 |
| [test_agent_evaluation_contract.py](../../tests/unit/test_agent_evaluation_contract.py) | C2 비교 계약 및 실패 보존 회귀 8개(파라미터화 포함) |
| [test_agent_benchmarks.py](../../tests/unit/test_agent_benchmarks.py) | C3 기준 전략·보고 전용·원화 대사 회귀 7개 |
| [test_agent_stat_policy.py](../../tests/unit/test_agent_stat_policy.py) | C4 비트 동일성·정책/JSON 호환·manifest·진단 도구 회귀 12개(파라미터화 포함) |
| README.md, loop-contracts.md, self-improvement.md, status.md, implementation-plan.md | 현재 구현과 후속 작업 구분, 통계 v2 사용법·증거 갱신 |

## Tests added

아래 함수는 파라미터화된 경우 여러 회귀로 실행된다. 기존 테스트와 단언은 수정하지 않았다.

| 테스트 | 고정하는 동작 |
| --- | --- |
| `test_tradability_preserves_pre_cap_demand` | 목표 100 → cap 10 → 체결 10, 10%/90%, trade log 수량 단계 |
| `test_missing_open_stays_in_reference_notional_denominator` | 같은 계획 금액의 1개 미체결 → 체결률/취소 감소율 각 50% |
| `test_zero_volume_cap_orders_are_not_silently_dropped` | 상한 0 이벤트·분모 보존 |
| `test_gap_risk_reduction_and_average_nav_turnover_are_separate` | 갭에서 위험 축소, 초기 자본/평균 NAV turnover 차이, 기존 evaluation_id 입력 유지 |
| `test_no_orders_has_no_defined_fill_ratios` | 빈 주문의 비율 N/A |
| `test_buy_sell_notionals_are_added_and_missing_reference_invalidates_ratios` | 매수·매도 금액 합산, 기준가격 누락 시 지표 전체 보류·주문 식별 |
| `test_split_adjusts_all_planned_quantity_stages_consistently` | 분할에 따른 desired/capped/planned 단위 일치 |
| `test_fractional_desired_split_does_not_cancel_integer_capped_execution` | 보고용 분수 수요가 기존 정수 체결을 취소하지 않음 |
| `test_candidate_parameters_do_not_change_the_comparison_contract` | lookback/ranking 제외, 결과 계약 hash 대사 |
| `test_different_economic_conditions_cannot_be_compared` | 구간·자본·위험·비용 시나리오 변경 거부, 차이 필드 표시 |
| `test_same_stress_contract_is_shared_by_distinct_candidates` | 같은 스트레스의 후보 간 계약 일치 |
| `test_contract_ids_are_stored_for_each_comparison` | 개발 3 fold 및 최종/스트레스 비교 계약 ID 보존 |
| `test_final_contract_mismatch_aborts_without_promoting_or_releasing_claim` | 최종 계약 불일치 시 승격 금지, 소비 이력 유지 |
| `test_cash_benchmark_uses_same_contract_and_constant_nav` | 현금 기준 전략도 공통 평가 계약·원장·결과 구조 사용 |
| `test_equal_weight_all_eligible_ignores_momentum_rank_and_threshold` | 적격 2종목 동일 비중 손계산, 모멘텀 순위/양수 임계 배제 |
| `test_benchmark_does_not_redistribute_rejected_slots` | 동일 비중 예산에서 1주 불가인 슬롯은 현금, 다른 종목에 재배정 금지 |
| `test_no_eligible_benchmark_stays_in_cash` | 적격 없음 → 거래 없음 |
| `test_rebalanced_benchmark_nav_is_exact_at_declared_money_precision` | 추가 매수 시 원화 정밀도로 독립 NAV 대사 |
| `test_benchmarks_are_reported_for_kept_cycle_and_shadow_without_changing_gates` | KEEP·shadow 기준 전략 보고, gate 비개입 |
| `test_candidate_benchmark_advantages_are_reporting_only` | 후보 대비 순성과·계약 ID 보고, 기준 전략 수치가 승격 gate를 바꾸지 않음 |
| `test_v1_confidence_interval_is_bit_identical` | 변경 전 3개 입력의 float hex 결과 유지 |
| `test_v2_rejects_unstable_tail_and_accepts_explicit_larger_budget` | v2 2,000회 거부·10,000회 허용, 하한만 추정 |
| `test_missing_version_in_legacy_policy_json_defaults_to_v1` | 이전 JSON의 새 필드 기본값 |
| `test_statistical_policy_fields_are_strict` | 미지원 버전·0/불리언/소수 draw 하한 거부 |
| `test_policy_version_changes_manifest_and_cycle_identity` | v1/v2 manifest·cycle ID 차이, metadata·confidence 외 gate 동일 |
| `test_legacy_policy_json_can_initialize_and_finish_without_rewriting_history` | 같은 build의 이전 JSON 비교/완료, 저장 원문 보존, v2로 덮어쓰기 금지 |
| `test_offline_stat_check_emits_json_for_explicit_development_window` | 명시한 개발 구간·필수 JSON·정책 불변 |

## Verification

최종 검증 결과(기존 210개 + 신규 35개):

| 명령 | 마지막 요약 |
| --- | --- |
| `uv run pytest -q` | `245 passed, 1 deselected in 75.00s (0:01:14)` |
| `uv run mypy src` | `Success: no issues found in 33 source files` |
| `uv run ruff check` | `All checks passed!` |
| `uv run --directory apps/kis-agent python -m unittest discover -s tests -q` | `Ran 122 tests in 8.534s` / `OK` |
| `uv run mypy src tools/stat_policy_check.py` | `Success: no issues found in 34 source files` |
| `git diff --check` | 오류 없음 |

상대 문서·코드 링크 88개 존재 확인, 변경 파일 허용 범위 검사 통과. 환경의 기본 uv cache가 읽기 전용이므로 모든 uv 명령에 `UV_CACHE_DIR=/tmp/momentum-uv-cache UV_OFFLINE=1`을 앞에 붙였다. 의존성을 추가하거나 다운로드하지 않았다.

C1~C3 각각에 전체 pytest, mypy, ruff, KIS unittest를 실행했다. 수집 뒤 추가된 테스트는 각 파일의 별도 실행으로 확인했고, 아래 최종 전체 실행에 모두 포함한다. 기존 20개 agent 회귀는 무변경 통과했다.

통계 도구의 기본 크기 검증:

```bash
uv run python tools/stat_policy_check.py \
  --development-start 2024-01-02 --development-end 2024-04-30
```

합성 weekday 86개, 정책별 null 100회와 seed·재표집·블록 민감도를 약 1.8초에 완료했다. 출력 원본은 작업 환경의 `/tmp/momentum-stat-policy-check.json`이며 Git에 포함하지 않았다. 실제 시장 오류율의 결론은 내리지 않는다.

## Remaining risks

- 실제 비용·시장 데이터·체결 가능성·수익성은 미검증이다. capacity·unaffordable_rate·시장 ETF, 기준 전략을 반영하는 승격 정책은 후속이다.
- `MarketProfile`, `decision_cadence`, 전략군 예산, E2 사전 고정, 연속 관찰/cohort/실패 기억 해제는 이 묶음의 대상이 아니다.
- v2 최소 꼬리 draw는 분위수 추정의 기계적 하한이다. 실제 오류율 보장이나 개발 구간 블록 선정 완료를 뜻하지 않는다. 진단은 단일 사이클 하한 양수 비율이며 결합 승격률이나 평생 family 오류율이 아니다.
- 이전 policy JSON 파싱은 호환되지만 소스 build 불일치 거부는 유지한다. 기존 실험 DB를 새 코드에서 쓰려면 기존 마이그레이션 경계를 따른다. 소비 이력을 지우거나 build를 덮어쓰지 않는다.

## Deviations from the spec

- C3 추가 매수 기준 전략에서 반복 소수인 가중평균 mark의 원 미만 잔차로 기존 대사가 실패했다. 공통 원장 소스는 그대로 두고 KRX 평가 경계의 독립 NAV를 선언된 원화 quantum으로 정규화한 뒤 대사한다. 실패 회귀를 먼저 추가했고 기존 테스트를 수정하지 않았다.
- C2 계약에는 기존 선정 필터의 hash도 포함했다. 구간·위험이 같아도 시장/품질 필터가 다르면 동일 조건으로 오인하지 않도록 한 보강이다. 후보 파라미터는 포함하지 않는다.
- push/PR 금지에 따라 선행 작업의 원격 merge 대신 동일 작업 브랜치의 검증된 로컬 커밋을 다음 작업의 기준으로 삼았다.

앱·미국 엔진/설정/골든·기존 테스트·의존성 파일을 수정하지 않았고 실제 증권사/모델 API 호출·주문도 하지 않았다.

## 리뷰 보완

리뷰 후 같은 작업 브랜치에서 보완했다. 변경은 `evaluator.py`, `service.py`, 새 [test_agent_review_followups.py](../../tests/unit/test_agent_review_followups.py) 및 현황/계획/이 문서에 한정한다.

**NAV 대사:** 원시 독립 잔차 `ledger.nav − (cash + positions_value + dividend_receivable)`가 유한하고 절대값이 `Decimal("1e-9")`원 이하인지 먼저 검사한다. 원 단위 반올림 후 정확 일치 검사도 남긴다. 일별 `nav_residual`과 최대 절대 잔차를 보존하고 허용 한도는 평가 계약에 포함한다. 원 단위 반올림으로 실제 계산 오류를 숨기지 않기 위한 별도 조건이며 상대 오차를 쓰지 않는다.

**기준 전략의 실제 투자 상태:** `EvaluationResult.portfolio_diagnostics`를 최종(후보 없음 포함)·shadow의 기준 전략 요약에도 넣는다.

| 필드 | 정의 |
| --- | --- |
| average_invested_weight / average_cash_weight / average_receivable_weight | 각 평가 세션 종가의 실제 보유 평가액·현금·배당채권 / NAV를 세션별 동일 가중 평균 |
| average_holding_count / last_holding_count / max_holding_count | 수량이 양수인 실제 보유 종목 수의 평균·마지막·최대 |
| budget_candidate_observations | 예산 부족 외 시장/품질 필터를 통과한 종목-세션 수 |
| unaffordable_observations / unaffordable_rate | 최대 허용 종목 예산 또는 동일 비중 슬롯 예산으로 비용 포함 1주도 못 사는 종목-세션 수 / 위 분모. 두 단계에서 중복 집계하지 않음 |
| budget_screening_applies | 적격 동일 비중에서만 true. 현금·일반 후보의 예산 부족 비율은 N/A |
| max_abs_nav_residual | 반올림 전 독립 NAV 절대 잔차의 구간 최댓값(Decimal 문자열) |

분모가 0이면 `unaffordable_rate = null`이다. 체결 시점의 갭·현금/위험 축소는 기존 거래 가능성 지표로 따로 보고한다. 이 진단은 성과/배분/승격을 바꾸지 않는다. 예를 들어 100종목 모두 적격이어도 100만 원의 동일 비중 슬롯으로 각각 1주를 못 사면 실제 투자 비중 0, 현금 비중 1, 보유 수 0, 예산 부족 비율 1로 드러난다. 시장 ETF·사전 고정한 종목 수 상한은 후속 범위다.

추가 회귀 12개(파라미터화 포함): `test_sub_won_nav_errors_cannot_hide_behind_rounding`(5개), `test_small_absolute_nav_residual_is_recorded_without_changing_economics`, `test_normal_repeated_buys_and_sells_pass_tight_raw_nav_check`, `test_many_eligible_stocks_report_effective_cash_and_budget_exclusions`, `test_budget_exclusion_ratio_counts_only_otherwise_eligible_candidates`, `test_stocks_unaffordable_at_maximum_budget_are_counted_once`, `test_no_budget_candidates_is_na_instead_of_zero_and_cash_is_explicit`, `test_portfolio_diagnostics_are_in_holdout_and_shadow_reports_only`.

리뷰 보완 최종 검증:

| 명령 | 마지막 요약 |
| --- | --- |
| `uv run pytest -q` | `257 passed, 1 deselected in 78.84s (0:01:18)` — 기존 245 + 신규 12 |
| `uv run mypy src` | `Success: no issues found in 33 source files` |
| `uv run ruff check` | `All checks passed!` |
| `uv run --directory apps/kis-agent python -m unittest discover -s tests -q` | `Ran 122 tests in 7.868s` / `OK` |
| `git diff --check` | 오류 없음 |

문서 상대 링크 90개 존재 확인, 변경 범위 검사 통과. 위 uv 명령은 동일한 오프라인 cache 설정으로 실행했다. 공통 원장·미국 코드/골든·KIS 앱·기존 테스트·승격 gate·일정·소비 규칙·의존성은 수정하지 않았다.
