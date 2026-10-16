# 루프 계약 설계안

상위 기준: [블루프린트](README.md) §3~§6. 이 문서의 계약 이름·필드는 **모두 설계안이며 현재 코드에 없다.** 기존 [상세 구조](architecture.md) §2의 DataBundle·CandidateProposal·FailureCase·ObservationRelease·RuntimeHealth 설계안을 4단계 루프 기준으로 보완한다.

공통 규칙:

- 모든 계약은 `schema_version`을 갖고, 모르는 필드를 거부한다.
- 계약 ID는 정규화된 JSON의 해시다(현재 `digest()`와 같은 방식).
- 필드가 부족하면 가정값을 넣어 통과시키지 않고 "지원 불가" 또는 "품질 보류"를 반환한다.
- 기존 파일 계약과 DB 소비 이력은 어댑터로 유지하며, 새 계약을 도입해도 이전 소비 이력을 지우지 않는다.

## 1. MarketProfile — S1·S2 (LOOP-01)

시장별로 달라지는 규칙을 한곳에 묶는다. 지금은 KRX 규칙이 `AgentPolicy`(KRX/KRW 강제), `session_time`(09:00/15:30/16:00), `SelectionPolicy`(KOSPI/KOSDAQ, STOCK/ETF)에 흩어져 있다.

| 필드 | 뜻 | KRX 첫 구현 값 |
| --- | --- | --- |
| market_id | 시장 식별자 | `KRX` |
| currency, money_quantum | 통화와 금액 최소 단위 | `KRW`, `1` |
| calendar_source, calendar_version | 세션·휴장·단축장 출처와 버전 | DATA-03 결과 |
| execution_clock | 시장 실행 시계: 관측 시각, 주문 가능 시각, 체결 시각, 확정봉 공개 시각(시간대 포함) | 관측 전일 16:00, 체결 다음 세션 09:00 개장, 확정봉 16:00 KST(현재 고정값) |
| tick_size_rule | 가격대·상품별 호가 단위 규칙 ID | FIN-01 결과 |
| cost_model_id | 상품·날짜·계좌별 비용 모델 | FIN-01 결과. 현재 예제 1.5/20/5bps |
| trading_constraints | 상하한가, 거래 정지, 공매도, 단주 등 | 현재 정지·관리·주의·레버리지·인버스 제외 |
| universe_filter_id | 기본 유니버스 필터 | KOSPI·KOSDAQ, STOCK·ETF |
| freshness_rule | 데이터 신선도를 세션 단위로 판정하는 규칙 | 현재 86,400초 벽시계 기준 → 세션 기준으로 교체(DATA-03) |

완료 조건: KRX 동작을 바꾸지 않고 `MarketProfile`로 옮긴 뒤 기존 회귀 20개가 그대로 통과한다. 미국 시장 profile은 정의할 수 있어도 최종 평가는 계속 잠근다(회귀 17 유지).

## 2. StrategyFamily — S1 (LOOP-02)

champion 갱신의 단위다. 예산은 §3의 예산 계정이 따로 관리한다.

| 필드 | 뜻 | 현재 대응 |
| --- | --- | --- |
| family_lineage_id | 변하지 않는 계보 키. champion·예산·실패 기억이 이 키에 묶인다 | 암묵적 `ranked-momentum` 하나 |
| family_version | 파라미터 공간·신호 정의가 바뀔 때마다 올라가는 버전 | 없음 |
| parent_version | 이전 family_version | 없음 |
| budget_account_id | 이 버전이 소비하는 예산 계정(§3) | 없음(연구 단위 8 사이클) |
| market_id | 적용 시장 | KRX |
| signal_contract | 신호 정의·특징·필요 이력 | momentum / risk_adjusted / trend_stability |
| parameter_space | 파라미터별 허용 범위와 등록 값 | lookback {5,10,20}(허용 2~60), ranking 3종 |
| fixed_parameters | 이 전략군에서 바꾸지 않는 값 | top_n 2, gross 0.8, min_momentum 0 |
| decision_cadence | 전략이 재선정·리밸런싱을 **결정**하는 주기. 후보 spec의 변수가 될 수 있다 | **매 세션**(현재 평가기가 세션마다 재선정) |
| holding_horizon | 신호가 겨냥하는 보유 기간 | 미정. 블록 민감도·purge 규칙의 입력 중 하나(RES-02) |

`decision_cadence`는 전략 쪽 변수다. 평가 쪽 `execution_clock`(§1, §5)은 모든 후보에 공통이므로, 일간·주간 결정 주기를 가진 후보도 같은 `EvaluationContract`에서 비교한다. 주간 전략은 결정하지 않는 세션에 주문을 내지 않을 뿐 같은 실행 시계 위에서 평가된다.

## 3. 예산 계정 — S1·S3·S4 (LOOP-02)

탐색 예산(사이클 수·후보 수·모델 시도 수)을 계정 원장으로 관리한다. INV-6(새 정책은 명시적 할당)과 INV-7(소비 기록은 사라지지 않음)을 이 원장이 지킨다.

| 필드 | 뜻 |
| --- | --- |
| budget_account_id | 계정 ID. 보통 `(market_id, family_lineage_id)`마다 하나 |
| parent_account_id | 상위 계정(연구군 전체 예산). 없으면 독립 연구군 |
| total | 등록 시 정한 총 예산(사이클 수, 후보 수, family alpha) |
| entries | 거래 기록: `allocate`(하위 버전·정책에 배정), `reserve`(사이클 등록), `consume`(최종 평가 실행), `release_unused`(평가하지 않고 끝난 예약의 반환. 최종 구간은 반환하지 않음) |
| remaining | `total − Σconsume − Σ미해제 reserve`. 계산값이며 직접 고치지 않는다 |

규칙:

- 연구 이름·빌드·정책·family_version을 바꾸거나 실행을 ABANDONED로 폐기해도 이미 기록된 `reserve`·`consume`은 지워지지 않는다. 현재처럼 폐기한 사이클의 최종 구간 소비도 유지한다.
- 새 정책 버전이나 새 family_version은 기존 계정의 `remaining`에서 `allocate`로 예산을 받는다. 새로운 독립 연구군으로 등록하려면 `parent_account_id` 없이 계정을 만들고 그 근거(다른 신호 계열, 다른 시장 등)를 남긴다. 독립 연구군끼리도 같은 시장의 최종 구간 소비(INV-4)는 공유한다.

## 4. SearchPlan과 CandidateProposal — S1 (RES-04, RES-05)

### SearchPlan — 사이클 시작 시 등록

| 필드 | 뜻 |
| --- | --- |
| plan_id, budget_account_id, family_version | 어떤 계정·버전의 예산을 쓰는지 |
| proposal_space | 허용 파라미터 공간과 제안 방식(grid/hypothesis/model) |
| candidate_budget | 이 사이클의 후보 수 상한 |
| development_window | 개발 구간 시작·끝 세션 |
| proposer_inputs | 제안기가 읽을 수 있는 해제된 실패 기억 ID 목록(§9) |

### CandidateProposal 보강

[상세 구조](architecture.md) §2의 필드에 다음을 더한다.

| 필드 | 뜻 |
| --- | --- |
| plan_id | 소속 SearchPlan |
| proposal_kind | `grid`(사전 등록 열거) / `hypothesis`(실패 기억 기반) / `model`(LLM·학습) |
| evidence_refs | 근거가 된 개발 구간 결과와 **`eligible_for_proposer = true`인** 실패 기억 ID만. 진행 중이거나 해제되지 않은 최종·추적 결과는 넣을 수 없음(INV-1) |
| expected_effect | 기대 효과와 측정 지표(예: 회전율 −20%) |
| falsification | 개발 구간에서 이 가설을 기각할 조건 |
| ancestor_ids | 이 후보를 낳은 이전 후보 |
| spec_hash | 후보 spec·모델·특징 코드의 해시(§6 commitment에 고정) |
| duplicate_key | 가설·데이터 버전·개발 구간·정책을 묶은 중복 판정 키 |

현재 grid 제안은 `proposal_kind = grid`, 가설 필드는 비어 있는 것으로 기록한다. grid 열거를 "개선 가설"이라고 부르지 않는다.

## 5. EvaluationContract — S2 (LOOP-03)

"같은 조건"을 하나의 ID로 묶는다. 계약 ID가 다른 결과로는 동일 조건 우위를 판정하지 않는다.

| 필드 | 뜻 | 현재 대응 |
| --- | --- | --- |
| evaluation_window | 평가 시작·끝 세션 | 개발 fold 또는 최종 구간 경계 |
| data_bundle_id | 평가 입력 묶음 | `ResearchInput.fingerprint(end)` |
| market_profile_id | 시장 규칙과 `execution_clock` | 없음(코드 고정) |
| valuation_basis | NAV 산정 기준(보유 평가 mark / 청산 기준) | 보유 평가 NAV, 청산 비용 미차감 |
| capital | 초기 자본과 자본 상한의 **수치** | 1,000,000원 / 1,000,000원 |
| capital_rule | 사용 자본 규칙 | `min(관측 시점 NAV, 연구 자본 상한)` |
| risk_limits | 수치화한 위험 한도 | 총 노출 0.8, 종목 0.4, 주문 400,000원 |
| initial_portfolio | 시작 현금·보유·배당채권과 reset 규칙 | 현금만, 구간마다 초기화(`positions_reset_per_evaluation: true`) |
| execution_model | 수량 계획 시각, 체결 가격, 거래량 상한, 매도 우선 | 전일 종가 계획, 다음 시가, 전일 거래량 1%, 매도 먼저 |
| cost_model_id | 기본 비용 시나리오 | 1.5/20/5bps |
| stress_scenarios | 스트레스 시나리오 목록. 시나리오마다 별도 하위 계약 ID | 수수료·slippage 2배 |
| ledger_version | 원장 계약 | `Ledger` 소스 해시(현재 `build_id`에 포함) |
| benchmarks | 함께 평가할 기준 전략 | 기존 champion만 → 현금, 동일 비중 유니버스, 시장 ETF 추가(RES-01) |
| metrics_spec_id | 지표 정의 표(§6) | 없음 |

후보의 `decision_cadence`는 이 계약에 넣지 않는다(§2).

완료 조건:

- 같은 data bundle이어도 평가 구간·초기 자본·위험 한도·초기 포트폴리오가 다르면 우위 판정을 거부하는 회귀가 있다.
- 스트레스 비교는 시나리오별로 candidate와 baseline의 하위 계약 ID가 같은지 확인한다.
- 등록된 일간·주간 결정 주기 후보가 같은 계약에서 비교된다.

### EvaluationResult에 추가할 거래 가능성 지표

현재 `rebalance_plan`은 거래량 상한을 적용한 **뒤의** 수량을 돌려주고, 평가기는 그 값을 `planned_quantity`로 기록한다. 목표 100주 → 상한 10주 → 체결 10주이면 지금 로그로는 체결률 100%가 되어 유동성 제약이 드러나지 않는다. 수량을 단계별로 보존한다.

```text
desired_qty        상한 적용 전 목표와 현재 보유의 차이
→ volume_capped_qty  거래량 상한 적용 후
→ cash_risk_capped_qty  현금·위험 한도 적용 후
→ filled_qty       실제 체결(결측 시가·정지면 0, 사유 기록)
```

| 지표 | 정의 | 위 예시 |
| --- | --- | --- |
| fill_ratio | Σfilled_qty / Σdesired_qty | 10% |
| volume_cap_reduction | Σ(desired − volume_capped) / Σdesired | 90% |
| cash_risk_reduction | Σ(volume_capped − cash_risk_capped) / Σdesired | 0% |
| cancel_reduction | Σ(cash_risk_capped − filled) / Σdesired, 사유별 분해 | 0% |
| unaffordable_rate | 예산 안에서 1주도 살 수 없어 제외된 후보 비율 | — |
| capacity_estimate | fill_ratio가 기준 아래로 떨어지기 시작하는 자본 규모(민감도) | — |

집계 규칙: 종목·세션 단위 수량을 체결 가격 기준 **금액 가중**으로 합산한 값을 주 지표로, 주문 건수 가중 값을 보조로 둔다. `Σdesired = 0`이면 비율은 N/A로 기록하고 0이나 100%로 바꾸지 않는다.

**turnover:** 현행 코드는 `거래대금 합 / 초기 자본`이다. 정책 v2에서는 `거래대금 합 / 평균 NAV`로 정의한다. 두 값은 이름을 구별해(`turnover_on_initial_capital`, `turnover_on_average_nav`) 기록하고 섞어 비교하지 않는다.

## 6. 지표 정의 표 — S2·S3 (LOOP-07)

현재 신뢰구간은 **일 수익률 차이의 평균**으로, 순개선 gate는 **누적 순수익률 차이**로 계산한다. 단위가 다르다고 그 자체로 오류는 아니다. 각 gate의 **역할**(주 검정 대상 / 경제적 최소 효과 / 보조 조건)과 단위를 먼저 고정한다.

| 지표 | 정의 | 역할 |
| --- | --- | --- |
| daily_excess_return | 같은 날짜의 candidate − baseline 비용 차감 일 수익률 | 주 검정 대상: 평균의 block bootstrap 하한 > 0 |
| period_net_return | 구간 시작 대비 끝 NAV 비율 − 1(보유 평가 NAV, 청산 비용 미차감) | 경제적 최소 효과: candidate − baseline ≥ 0.001. 비용 스트레스 비교 |
| max_drawdown | 일별 NAV 기준 최대 낙폭 | 보조 조건: 개발·최종 낙폭, 열화 |
| fills | 체결 기록 수(왕복 거래 수 아님) | 보조 조건: 활동 |

정책 v2에서도 두 지표를 함께 둘 수 있다. 바꾸는 경우 새 정책 버전과 새 예산 할당으로 적용하고, 기존 연구의 기준은 소급해 바꾸지 않는다(INV-6).

## 7. EvaluationCommitment — S3 (RES-03)

현재 사이클 manifest는 최종 구간 데이터가 도착한 뒤, 개발 선택과 함께 계산 직전에 저장된다. 후보 **공간**만 미리 고정하면 평가 기간이 지난 뒤 최종 후보를 골라도 E2라고 기록할 수 있으므로, 등록을 두 단계로 나눈다.

| 단계 | 저장 시점 | 고정하는 것 |
| --- | --- | --- |
| SearchPlan(§4) | 사이클 시작 | 탐색 공간·후보 예산·개발 구간·예산 계정 |
| **EvaluationCommitment** | 개발 선택 직후, 평가 구간 **첫 실행 시각 전** | 선택 후보 `spec_hash`(모델·특징 코드 포함), baseline spec, 개발 구간 끝, `EvaluationContract` ID(데이터 묶음 제외), 합격 기준·통계 정책, 평가 구간 시작·끝 세션(달력 버전 포함), 저장 시각 |
| manifest | 구간 도착 후, 계산 전(현재와 같음) | commitment ID, 실제 data_bundle_id |

증거 등급 판정([블루프린트](README.md) §5):

- **E2:** commitment 저장 시각 < 평가 구간의 첫 실행 시각(첫 수익이 형성되기 시작하는 주문 체결 시각. KRX 일봉 전략은 첫 세션 09:00 개장)이고, 평가 구간의 모든 세션이 저장 시각 이후에 열렸다.
- **E1:** 그 밖의 경우. 이미 발생한 기간의 자료를 늦게 수집하거나 정정 공개를 받아도 E2가 되지 않는다.

완료 조건:

- 개장 후 고정, 평가 기간 시작 후 후보·기준·합격 기준 변경, 과거 자료 재수집은 E2로 기록되지 않는 회귀가 있다.
- commitment에 없는 후보는 그 구간에서 평가되지 않는다.
- 저장 시각을 외부에서 확인하는 방법(별도 저장소, Git 태그 등)을 정한다.

## 8. ChampionRegistry — S4 (LOOP-04)

| 필드 | 뜻 |
| --- | --- |
| key | `(market_id, family_lineage_id)` |
| revision | 단조 증가 번호 |
| spec, family_version, evaluation_contract_id | 현재 champion과 평가 조건 |
| evidence_grade | 이 revision을 만든 증거 등급(E1/E2/E3) |
| source_cycle_id, commitment_id | 근거 사이클과 고정 기록 |
| previous_revision | 강등 시 돌아갈 revision |

현재 `agent_studies.champion_json`/`revision`은 연구 단위다. 이전 경로: 기존 연구 하나를 `(KRX, ranked-momentum)` 키로 등록하고, 같은 키에 다른 연구가 champion을 쓰려면 같은 예산 계정에서 할당을 받게 한다.

## 9. 실패 기억과 개발 해제 — S4 → S1 (LOOP-08, RES-04)

### FailureCase 필드 추가

[상세 구조](architecture.md) §2 `FailureCase`에 다음을 더한다.

| 필드 | 뜻 |
| --- | --- |
| source_cycle_id | 실패가 나온 사이클 |
| source_phase | `development` / `final` / `observation` |
| released_to_development_at | 개발로 해제된 시각. 비어 있으면 미해제 |
| eligible_for_proposer | 제안기가 읽어도 되는지. 해제 기록이 있어야 true |
| consumed_windows | 이 실패가 나온 최종·관찰 구간. 해제 뒤에도 최종 검증 재사용 금지 |

해제 규칙:

- `source_phase = development`인 실패는 바로 해제할 수 있다.
- `final`·`observation` 실패는 그 사이클이 COMPLETE 또는 ABANDONED가 된 뒤에만 해제한다. 진행 중인 사이클의 최종 gate·열화 결과는 해제할 수 없다.
- 해제된 실패를 근거로 만든 후보는 `consumed_windows`와 겹치지 않는 **새로운** 평가 구간에서만 검증한다.
- 해제는 기록이지 삭제가 아니다. 해제 전후 모두 원래 결과는 감사용으로 보존한다.

### 실패 코드

| 코드 | 뜻 | 해제 후 다음 S1 입력으로 쓰는 방식 |
| --- | --- | --- |
| DATA_DEFECT | 누락·상태 미상·정정 | 수익 개선 가설로 쓰지 않음. 데이터 작업으로 보냄 |
| DEV_NO_IMPROVEMENT | 개발에서 champion보다 나은 후보 없음 | 같은 공간의 재시도 비용 기록 |
| DEV_RISK | 개발 낙폭·활동 조건 실패 | 위험·활동 관련 가설 후보 |
| FINAL_UNDERPERFORM | 최종 순개선 또는 비용 스트레스 실패 | 해제 후에만 비용·회전율 가설 후보. 같은 구간 재선택 금지 |
| INSUFFICIENT_EVIDENCE | 신뢰구간 하한 ≤ 0 또는 관측 수 부족 | 새 데이터 대기. 길이·블록을 바꿔 재검정 금지 |
| TRACKING_DEGRADED | 관찰 열화 상태 진입 | 해제 후에만 국면·비용·데이터 변화 조사 입력 |

## 10. 관찰 원장과 열화 정책 — S4 (LOOP-05)

### 실험 원장과 관찰 원장의 분리

| 원장 | 쓰는 곳 | 초기 조건 | 연속성 |
| --- | --- | --- | --- |
| 실험 원장 | S2·S3 | 후보마다 같은 초기 자본·빈 포트폴리오(`EvaluationContract.initial_portfolio`) | 구간마다 초기화 |
| 관찰 원장 | S4, `ObservationRelease` | 관찰 등록 시점의 포트폴리오 | revision이 바뀌어도 현금·보유·배당채권을 이어 감 |

관찰 원장 규칙:

- revision 교체는 새 champion의 목표 포트폴리오로 가는 **실제 거래**로 기록하고, 그 거래 비용을 관찰 성과에 포함한다.
- 교체 직전·직후 자산은 실제 거래·비용·기업행동 외에는 불연속이 없어야 한다(완료 조건).
- 현재 20세션 shadow 블록은 실험 원장 결과다. 이를 이어 붙여 연속 NAV라고 부르지 않는다.
- 비교 대상: 직전 revision과 기준 전략(현금·단순 전략)을 같은 실행 시계의 **가상 관찰 원장**으로 함께 기록한다. champion을 유지한 사이클은 직전 revision과 같으므로 기준 전략 비교만 의미가 있다.

### 열화 상태

| 상태 | 진입 조건(예시, 정책으로 사전 등록) | 동작 |
| --- | --- | --- |
| OBSERVING | 기본 | 기록만 |
| WARNING | 관찰 낙폭이 정책 한도 초과, 또는 기준 전략 대비 누적 열세가 사전 등록 임계 초과 | 보고·알림, 원인 분류 |
| DEMOTION_CANDIDATE | WARNING이 사전 등록 기간 이상 지속 | 사람 확인 후 이전 revision 또는 이전 Release로 강등 |

규칙:

- 현재 열화 flag는 1원만 뒤처져도 켜진다. 정책에서는 임계와 지속 기간을 **사전 등록**한다.
- 강등은 이미 검증된 이전 revision으로만 한다. 강등을 이유로 과거 구간을 다시 합격시키지 않는다.
- 열화 판정은 해제 규칙(§9)을 거쳐 S1의 실패 기억이 된다.

## 11. Release — S4 → 운용 (LOOP-06)

관찰 등록과 실행 자격을 분리한다. 이렇게 해야 사전 E3 기록이 없어도 첫 관찰 버전을 등록할 수 있다.

### ObservationRelease

[상세 구조](architecture.md) §2의 `ObservationRelease`를 다음 필드로 확장한다.

| 필드 | 뜻 |
| --- | --- |
| observation_release_id | 관찰 버전 식별자 |
| champion_key, champion_revision | 근거 champion |
| evidence_grade | E2 이상 |
| selector_policy_id, cost_model_id, risk_limits_id, evaluation_contract_id | 관찰에서 그대로 쓸 계약 |
| starting_portfolio | 관찰 원장 시작 상태 |
| data_exposure | 관찰 시작 시 이 버전이 이미 본 자료 범위(E3 해석용) |
| approved_by, approved_at | 사람 승인 |
| stop_conditions | 열화 DEMOTION_CANDIDATE, 데이터 장애, 사람 중단 |
| order_authority | 항상 `false` |

### ExecutionRelease

| 필드 | 뜻 |
| --- | --- |
| execution_release_id | 실행 버전 식별자 |
| observation_release_id | 근거 관찰 버전 |
| e3_window | 사전 등록한 최소 관찰 기간과 실제 관찰 구간 |
| target_mode | `paper` / `live` |
| capital_scope | 전체 운용 자본 상한(100만 원은 모든 주문에 걸친 총 상한) |
| approved_by, approved_at | 사람 승인 |
| rollback_release_id | 강등 시 돌아갈 Release |
| stop_conditions | 열화, 계좌 대사 불일치, 주문 상태 UNKNOWN, 사람 중단 |

Release는 주문을 만들지 않는다. 주문 경로(ORD-02~04)가 `ExecutionRelease`를 읽어 실행한다.

완료 조건: 사전 E3 기록이 없는 첫 관찰 버전을 등록할 수 있다. 관찰 등록만으로 주문 권한이 생기지 않는다.
