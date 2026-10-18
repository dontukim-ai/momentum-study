# 루프 계약 설계안

상위 기준: [블루프린트](README.md) §3~§6. 목차: §1 MarketProfile, §2 StrategyFamily, §3 예산 계정, §4 SearchPlan·CandidateProposal, §5 EvaluationContract·거래 가능성 지표, §6 지표 정의, §7 EvaluationCommitment, §8 ChampionRegistry, §9 실패 기억 해제, §10 관찰 원장·열화, §11 Release, §12 구간 예약·동시 사용. 이 문서는 목표 계약 설계안이다. C1/C2에서 §5 평가 계약 ID·수량 단계·reference_price 지표와 두 turnover 분모를 현재 KRX 값으로 구현했다. C3의 기준 전략은 보고만, C4의 통계 v2는 opt-in이다. 나머지 계약·필드의 현재 상태는 [현황](status.md)과 [구현 보고](batch1-implementation.md)를 따른다. 기존 [상세 구조](architecture.md) §2의 DataBundle·CandidateProposal·FailureCase·ObservationRelease·RuntimeHealth 설계안을 4단계 루프 기준으로 보완한다.

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

**현재 구현(LOOP-02a):** `agent_budget_accounts`와 `agent_budget_entries`가 `(market, family_lineage_id)` 계정을 관리한다. 현재 계보는 `ranked-momentum` 하나이며 첫 사이클 등록 연구의 고정 사이클 예산을 사용한다. 추가 전용 원장은 `reserve`(RES-03a 이후 commitment 고정, 기존 사이클은 등록)와 `consume`(COMPLETE·ABANDONED·평가 실패)만 사용한다. `remaining = total_cycles − reserved`로 계산하며 소비는 예약을 다시 차감하지 않는다. 이름 변경·실패·폐기로 예약을 반환하지 않고, 다른 총예산 정책의 등록은 명시적 할당이 필요하다는 오류로 거부한다. 기존 사이클은 REGISTERED·EVALUATED에는 reserve만, 그 외 상태에는 reserve와 consume을 멱등 역채움한다. `release_unused`·`allocate`·독립 연구군·`family_version`은 미구현이며, 구현 완료 근거는 합성 회귀 범위다.

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

집계 규칙:

- 주문마다 **계획 시점에 알 수 있었던 `reference_price`**(현재 계획에 쓰는 전일 종가 mark)를 보존한다. 체결 가격은 미체결 주문에 없으므로 가중에 쓰지 않는다.
- 모든 수량 단계에 같은 `reference_price`를 곱해 금액으로 합산한다. 매수·매도는 주문별 **절대 수량**으로 더해 서로 상쇄되지 않게 한다.

```text
fill_ratio = Σ(|filled_qty| × reference_price) / Σ(|desired_qty| × reference_price)
```

- 위 식처럼 감소 지표도 같은 분모(`Σ|desired_qty| × reference_price`)를 쓴다. 주문 건수 가중 값은 보조로 둔다.
- 분모가 0이면 N/A로 기록하고 0이나 100%로 바꾸지 않는다.
- `reference_price`가 없는 주문이 있으면 금액 가중 지표를 품질 보류(N/A)로 두고, 누락된 주문 수·종목·세션을 따로 보고한다. 그 주문을 집계에서 빼지 않는다.

완료 조건 회귀: 계획 금액이 같은 주문 2개 중 하나는 전량 체결, 하나는 시가 결측이면 fill_ratio 50%.

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

사이클 manifest는 최종 구간 데이터가 도착한 뒤, 최종 계산 직전에 저장한다. 후보 **공간**만 미리 고정하면 평가 기간이 지난 뒤 최종 후보를 골라도 E2라고 기록할 수 있으므로, 후보 고정과 최종 평가를 두 단계로 나눈다.

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

**현재 구현(RES-03a, 합성 회귀 범위):** 추가 전용 `agent_commitments(commitment_id, study, ordinal, payload_json, committed_at)`에 연구별 ordinal 하나를 저장한다. payload 해시가 ID이며 개발 계획의 인덱스·날짜, 빌드·정책, baseline·선택 후보 spec/version(동일하면 후보 없음), 개발 trials·scores·fold 계약 ID, 승격 기준·통계 정책, 최종 날짜·달력 목록 해시, 개발 prefix 지문을 고정한다. `ResearchInput.calendar`는 미래 거래일을 포함할 수 있고 관측 범위는 `sessions`와 정확히 일치해야 한다. 생략하면 기존 `sessions`를 따르며, `prefix`는 달력도 자른다. 미래 달력은 개발 지문에서 제외한다. CLI `run --calendar`는 날짜 문자열 JSON 배열을 읽는다. 달력에 최종 끝 날짜가 있고 개발 데이터가 도착하면 개발 선택을 수행하고, 같은 트랜잭션에서 예산 reserve·최종 구간 `_claim`·commitment 저장을 한다. 예산 원장의 `cycle_id`는 이 단계부터 commitment ID를 예약 식별자로 유지한다. 평가 등록 시 구간의 소유자만 cycle ID로 넘기며 소비 날짜는 보존한다.

최종 데이터 도착 시 commitment ID를 manifest에 포함하고, 저장된 baseline·후보로만 평가한다. 개발 지문·최종 달력 변경은 오류이며 commitment만 있는 재시작은 다시 선택하지 않는다. `committed_at < session_time(holdout_start, 9)`이면 E2, 그 밖에는 E1이다. 등급·commitment ID·저장 시각·첫 실행 시각을 evidence에, 등급을 result·status에 기록한다. gate는 등급과 무관하게 기존 정책을 유지한다. commitment 없는 기존 COMPLETE/ABANDONED는 역채움 없이 status에서 E1로 표시한다. 달력 부족 시 고정 없이 대기하고, 고정 후에는 `committed_cycle`로 남은 세션 수를 보고한다. 외부에서 확인 가능한 고정 시각(별도 저장소·서명), 달력 공급자의 사실성 검증, commitment만 있는 ordinal의 폐기 경로는 미구현이다. 실데이터 전진 검증 완료를 주장하지 않는다.

## 8. ChampionRegistry — S4 (LOOP-04)

| 필드 | 뜻 |
| --- | --- |
| key | `(market_id, family_lineage_id)` |
| revision | 단조 증가 번호 |
| spec, family_version, evaluation_contract_id | 현재 champion과 평가 조건 |
| evidence_grade | 이 revision을 만든 증거 등급(E1/E2/E3) |
| source_cycle_id, commitment_id | 근거 사이클과 고정 기록 |
| previous_revision | 강등 시 돌아갈 revision |

**현재 구현 (LOOP-04a, 합성 회귀 범위).** `agent_champion_registry`는 예산 계정과 같은 `(market, family_lineage_id)` 키의 추가 전용 이력이다. 최대 revision이 현재 champion이며 spec·버전·증거 등급·source cycle·commitment·이전 revision·연구·시각을 보존한다. 첫 commitment 트랜잭션에서 연구 champion을 revision 0으로 등록하고, 이후 고정은 연구 spec과 revision이 현재 레지스트리와 일치해야 한다. 승격은 manifest revision을 다시 검사한 같은 트랜잭션에서 새 행과 연구 상태를 기록하며 유지는 행을 추가하지 않는다.

새 연구만 정책의 제안 공간·위험 한도와 호환되는 현재 registry champion 및 revision으로 초기화한다. 기존 연구는 자동 동기화하지 않고 불일치 시 고정을 거부한다. `champion_registry(market)`는 키별 오름차순 전체 이력과 현재 행을, `status`는 registry key·현재 revision·spec version·증거 등급을 제공한다.

기존 DB는 첫 접근 시 과거 revision을 역채우지 않는다. 해당 키에 registry가 없고 COMPLETE 사이클이 있는 연구 중 현재 revision이 가장 큰 연구(동률이면 이름 오름차순)의 현재 champion을 그 revision 그대로 단 한 행, `evidence_grade='legacy'`로 기록한다. 이행은 멱등이며 과거 평가 증거를 새로 만들지 않는다. 실데이터 검증·연속 관찰 원장·열화 상태·Release는 이 구현 범위에 포함되지 않는다.

## 9. 실패 기억과 개발 해제 — S4 → S1 (LOOP-08, RES-04)

### FailureCase 필드 추가

[상세 구조](architecture.md) §2 `FailureCase`에 다음을 더한다.

| 필드 | 뜻 |
| --- | --- |
| source_cycle_id | 실패가 나온 사이클 |
| source_phase | `development` / `final` / `observation` |
| source_window | 이 결과를 만든 구간의 시작·끝 세션 |
| observed_through | 결과에 반영된 마지막 세션 |
| result_hash | 확정된 결과 snapshot의 해시 |
| sealed_at | 결과 snapshot을 확정(봉인)한 시각. 비어 있으면 미확정 |
| released_to_development_at | 개발로 해제된 시각. 비어 있으면 미해제 |
| eligible_for_proposer | 제안기가 읽어도 되는지. 해제 기록이 있어야 true |
| consumed_windows | 이 실패가 나온 최종·관찰 구간. 해제 뒤에도 최종 검증 재사용 금지 |

해제 단위는 사이클 상태가 아니라 **확정된 결과 snapshot**(`source_window`, `observed_through`, `result_hash`, `sealed_at`)이다. 현재 COMPLETE는 최종 평가와 champion 결정이 끝났다는 뜻일 뿐이다. `_finish`가 COMPLETE를 저장한 **뒤에** `_shadow`가 시작되고, shadow 보고는 같은 행(`agent_shadow`, `ON CONFLICT DO UPDATE`)에 계속 덮어써진다. 그래서 사이클 상태만으로는 관찰 결과가 확정됐는지 알 수 없다.

해제 규칙:

- `development` 결과는 개발 평가가 끝나 snapshot이 확정되면 바로 해제할 수 있다.
- `final` 결과는 그 사이클의 최종 결정(champion 갱신 또는 유지)이 확정된 뒤 해제한다. ABANDONED 사이클의 최종 결과는 남은 부분 결과를 봉인한 뒤 해제한다.
- `observation` 결과는 **종료·봉인된 관찰 구간 또는 checkpoint**만 해제한다. 열려 있는 관찰 구간은 계속 비공개다. 연속 관찰에는 끝이 없으므로 관찰 정책에 checkpoint 주기(예: N세션마다)를 사전 등록한다.
- 해제한 snapshot은 이후 관찰 갱신으로 바뀌지 않는다. 새 관찰 결과는 새 snapshot이 된다.
- 검증 cohort(§12) 기간과 겹치는 관찰 결과는 그 cohort의 최종 결정이 확정될 때까지 봉인한다.
- 해제된 실패를 근거로 만든 후보는 `consumed_windows`와 겹치지 않는 **새로운** 평가 구간에서만 검증한다.
- 해제는 기록이지 삭제가 아니다. 해제 전후 모두 원래 결과는 감사용으로 보존한다.

완료 조건 회귀:

- 사이클은 COMPLETE지만 관찰 구간이 열려 있으면 해제를 거부한다.
- 봉인된 과거 구간만 다음 개발 입력으로 허용한다.
- 뒤에 추가된 관찰 결과가 이미 해제된 snapshot에 섞이지 않는다.

### 실패 코드

| 코드 | 뜻 | 해제 후 다음 S1 입력으로 쓰는 방식 |
| --- | --- | --- |
| DATA_DEFECT | 누락·상태 미상·정정 | 수익 개선 가설로 쓰지 않음. 데이터 작업으로 보냄 |
| DEV_NO_IMPROVEMENT | 개발에서 champion보다 나은 후보 없음 | 같은 공간의 재시도 비용 기록 |
| DEV_RISK | 개발 낙폭·활동 조건 실패 | 위험·활동 관련 가설 후보 |
| FINAL_UNDERPERFORM | 최종 순개선 또는 비용 스트레스 실패 | 해제 후에만 비용·회전율 가설 후보. 같은 구간 재선택 금지 |
| INSUFFICIENT_EVIDENCE | 신뢰구간 하한 ≤ 0 또는 관측 수 부족 | 새 데이터 대기. 길이·블록을 바꿔 재검정 금지 |
| TRACKING_DEGRADED | 관찰 열화 상태 진입 | 해제 후에만 국면·비용·데이터 변화 조사 입력 |

### 현재 구현 (LOOP-08a)

합성 회귀 범위에서 `agent_failure_snapshots`와 `agent_memory_releases`를 추가 전용으로 구현했다. 개발 실패(DATA_DEFECT·DEV_RISK·DEV_NO_IMPROVEMENT)는 commitment 저장 트랜잭션에서 봉인·해제(`development_sealed`)한다. 최종 실패(FINAL_UNDERPERFORM·INSUFFICIENT_EVIDENCE)는 COMPLETE 결정 트랜잭션에서만 봉인·해제(`final_decision_confirmed`)한다. ABANDONED에는 최종 snapshot을 만들지 않는다. 위 ABANDONED 부분 결과 봉인은 향후 계약이며 현재 구현 범위가 아니다.

개발 시점에는 최종 입력 fingerprint를 포함한 cycle ID가 아직 존재하지 않으므로 개발 snapshot의 `source_cycle_id`는 commitment ID다. 이후 `agent_cycles.manifest_json.commitment_id`로 연결한다. 최종·관찰 snapshot은 cycle ID를 사용한다. 구간·마지막 반영 세션·payload 해시·봉인 시각을 저장하고, payload는 관련 trial·gate·집계 지표로 제한한다. 원시 일별 수익률은 복사하지 않는다.

관찰 실패(TRACKING_DEGRADED)는 `observations == policy.shadow_sessions`인 완결 shadow만 봉인·해제(`observation_window_closed`)한다. 부분 shadow는 열화가 있어도 해제하지 않는다. `agent_shadow` 갱신은 이미 봉인된 snapshot을 변경하지 않는다. 현재 고정 길이 shadow는 한 완결 구간만 만들며, 연속 관찰 checkpoint는 LOOP-05b에서 별도 추가 전용 테이블과 실패 코드로 연결한다.

`development_memory(market="KRX")`는 해제 기록과 결합된 snapshot만 삽입 순서(SQLite rowid)로 반환한다. `eligible_for_proposer=True`와 최종·관찰의 `consumed_windows`를 포함하며 개발 기억의 소비 구간은 빈 목록이다. commitment의 `proposer_inputs`는 같은 트랜잭션에서 새 개발 snapshot 봉인 전에 보이던 snapshot ID 목록이다. grid 제안기는 아직 기억을 사용하지 않는다. `status(name).memory`는 연구별 snapshot·해제 수와 코드별 개수를 보고한다. LOOP-09a는 관찰 cohort 봉인 표시와 제안기 비공개를 합성 회귀로 고정한다. 관찰 checkpoint 저장·봉인 해제 후 기억 연결은 LOOP-05b 합성 회귀 범위에서 구현했다. 기억을 쓰는 제안(RES-04/05)과 실데이터 검증은 미구현이다.

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
- 관찰 원장이 쓰는 날짜와 다음 사이클의 최종 검증 구간이 겹치는 문제는 §12의 구간 예약 정책을 따른다.

**현재 구현 (LOOP-05a).** `CommonEvaluator.evaluate_schedule`은 하나의 기존 Ledger와 체결 경로로 날짜별 spec을 선택하며 현금·보유·배당채권을 유지한다. `portfolio_diagnostics.schedule.revision_boundaries`는 변경 세션의 전일 종가 NAV·당일 종가 NAV·당일 수수료를 기록한다. `ResearchAgent.observation_ledger`와 `tick.observation`은 첫 COMPLETE 최종 검증 다음 calendar 세션부터 registry revision을 이어 계산하고 동일 계약의 현금·적격 동일 비중 기준과 비교한다. 시작 시 현금 초기화는 한 번뿐이다. 관찰 원장 계산은 미저장이며 실패 기억·제안기·예산·주문 권한에 연결하지 않는다. LOOP-09a에서 `tick`은 성공한 보고 구간의 노출만 별도 트랜잭션의 `_claim`으로 기록한다(§12). legacy 근거가 없으면 해당 legacy 연구의 마지막 COMPLETE 이후로 시작하며 과거 revision을 복원하지 않는다. 관찰 계산이 데이터 문제(`AgentError`, `SpecUndefinedError`)로 실패하면 `tick`은 멈추지 않고 `observation.status = "unavailable"`과 오류 종류·메시지를 보고한다. 연구 사이클의 등록·평가·champion 갱신은 그대로 진행된다. 그 밖의 예외(코드 결함)는 숨기지 않고 그대로 올린다. 기존 `_shadow`는 이 격리를 적용하지 않았다. 합성 회귀 범위에서 구현됐으며 checkpoint 저장·열화 상태는 아래 LOOP-05b 구현을 따른다. 실데이터 검증은 미완료다. §12의 단일 challenger cohort 동시 사용·봉인 표시는 LOOP-09a 합성 회귀 범위에서 구현했다.

**현재 구현 (LOOP-05b).** `agent_observation_checkpoints`는 스트림별 `seq`와 창·revision·spec_version·상태·최초 봉인 여부·집계 payload·입력/결과 해시를 추가 전용으로 저장한다. 관찰 시작부터 관측 세션 N개마다 완결 창만 순서대로 저장하며 부분 창은 저장하지 않는다. `observation_checkpoint_sessions=20`(최소 2), `degradation_reference="equal_weight_eligible"`(또는 `cash`), `degradation_underperformance=0.03`, `degradation_drawdown=None`(기존 평가 낙폭 한도 사용), `demotion_checkpoints=3`을 정책으로 사전 등록한다. 명시적 낙폭 한도와 열세 임계는 0 초과 1 미만, 지속 checkpoint 수는 최소 1이다. 기존 정책 JSON은 기본값으로 읽는다.

매 창 끝까지 관찰 시작에서 이어 계산한 일별 NAV로 창 수익률과 낙폭을 구한다. 기준점은 창 시작 전일 NAV(첫 창은 초기 자본)이며 낙폭 peak에도 포함한다. 동일 계약 기준 전략도 관찰 시작부터 같은 끝까지 계산한다. `breach = 창 낙폭 > 한도 or (창 수익률 - 기준 창 수익률) < -열세 임계`다. breach가 없으면 OBSERVING, 같은 revision에서 연속 breach 수가 지속 수 미만이면 WARNING, 이상이면 DEMOTION_CANDIDATE다. revision 변경과 정상 창은 연속 수를 초기화한다. 창 끝 유효 revision과 그보다 작은 최대 registry revision의 강등 대상만 기록하며 강등은 실행하지 않는다.

`input_hash=data.fingerprint(창 끝)`, `result_hash=digest(payload)`, `checkpoint_id=digest({stream, seq, result_hash})`다. 기존 seq와 입력 또는 결과 해시가 달라지면 `Stored observation checkpoint changed`를 보고하고 행을 보존한다. checkpoint 오류는 연구 진행과 격리한다. 겹치는 진행 cohort가 있으면 최초 저장의 `sealed=1`을 보존하고 보고에서 수치를 숨긴다. 이후 현재 cohort 상태를 다시 읽어 모두 해제됐을 때만 WARNING/DEMOTION_CANDIDATE를 `TRACKING_WARNING`/`TRACKING_DEMOTION_CANDIDATE` snapshot으로 봉인·개발 해제한다. 최초 비봉인 실패는 checkpoint와 같은 트랜잭션에서 기억을 만든다. OBSERVING은 기억을 만들지 않는다. 일별 원자료는 payload에 넣지 않는다.

`tick.observation_checkpoints`는 이번 저장 수·최근 요약·오류를, `status.observation_state`는 스트림의 최근 상태를 보고한다. 봉인 중에는 성과·임계·강등 대상 payload를 보고하지 않으며 해제 뒤 공개한다. `observation`은 LOOP-09a의 보고 계약을 유지하며 직접 `observation_ledger` 조회 결과와 같다. 관찰이 None/unavailable이면 `observation_checkpoints`도 None이다. `observation_ledger` 조회는 계속 DB에 쓰지 않는다. 기존 `_shadow`·TRACKING_DEGRADED 규칙은 유지한다. 합성 회귀 범위 구현이며 실데이터 검증, 강등 실행·알림·ObservationRelease(LOOP-06)는 미구현이다.


**현재 구현 (LOOP-05c).** 매 tick 시작 시 가장 최근에 저장된 seq 하나는 build가 같아도 관찰 시작부터 다시 계산해 결과 해시까지 비교한다. 새 seq도 계산하며, 나머지 같은 build의 저장된 seq는 창 끝의 입력 지문만 확인한다. 동일 입력 재방문은 최신 저장 창 1개, 한 창 증가 시에는 이전 최신 저장 창과 새 창 2개를 평가한다. 직전 저장 payload의 revision·breach·consecutive_breaches를 이어받는다. 봉인 중 payload도 내부 계산에만 사용하고 기존 공개 제한은 유지한다. 새 checkpoint payload에 service의 `build_id()`인 `build`를 추가한다. build가 다르거나 없는 기존 행은 전체 계산 경로로 결과 해시까지 재검증하며 저장 행은 갱신하지 않는다. 따라서 이전 build 행은 이후 tick에서도 재검증 대상이다. `build`는 경제 결과 해시에서 제외하여 LOOP-05b의 result_hash·checkpoint_id와 호환한다. 입력 지문 또는 재계산 결과가 달라지면 기존과 같은 `Stored observation checkpoint changed`로 격리한다. 매 tick 모든 저장 checkpoint의 현재 봉인 여부를 확인하고 저장 payload로 실패 기억 해제를 판정한다. 봉인 해제 자체는 추가 재계산을 요구하지 않는다. 최신 창 안전 재검증에서 데이터 결함이 발생하면 기존 오류 종류를 보존하여 격리하고 활성 ObservationRelease의 DATA_FAILURE 중단으로 연결한다. 증분·전체 계산의 동일성과 호출 수는 합성 회귀 범위에서 확인하며 실데이터 성능·운영 검증은 미완료다.

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

FIN-01a: 관찰 release는 근거 사이클 정책의 `spread_mode`와 `execution_eligible`(quoted일 때만 true)을 동결한다. 후속 ExecutionRelease는 `execution_eligible=false`인 관찰 release를 거부해야 한다.

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

**현재 구현 (LOOP-06a).** 추가 전용 `agent_observation_releases`와 `agent_release_events`에 현재 레지스트리 E2/E3 revision의 사람 승인·관찰 payload 및 STOPPED 기록을 저장한다. 근거 사이클의 연구 정책·최종 후보 평가 계약과 시장의 최대 노출 날짜를 고정한다. 같은 revision의 active 등록은 멱등이고 다른 revision은 기존 등록 중단 뒤 승인한다. 사람 중단, 같은 revision의 최근 DEMOTION_CANDIDATE, 관찰 unavailable/checkpoint 오류에 따른 자동 중단을 구현했다. 자동 중단은 관찰·checkpoint 처리 뒤 연구 진행과 분리하며 champion·예산·소비 구간을 바꾸지 않는다. 봉인 중 안전 중단도 허용하고 이벤트 detail에는 순번·창만 기록한다. `status`는 active 등록·레지스트리 지연 여부·마지막 중단을, `tick.release_events`는 이번 tick의 중단을 보고한다. CLI는 `release-observation`·`release-stop`이다. `order_authority`는 항상 0이며 주문 경로와 연결하지 않는다. 합성 회귀 범위 구현으로 한정하며 ExecutionRelease·알림·실시간 E3와 실데이터 검증은 미구현이다.

## 12. 구간 예약·동시 사용·공개 정책 — S3·S4 (LOOP-09)

### 문제

LOOP-09a 이전 `ResearchAgent._claim`은 시장과 날짜가 겹치고 owner가 다르면 **용도(kind)와 상관없이** 거부한다. 고정 20세션 shadow는 다음 사이클의 최종 구간과 날짜가 겹치지 않아서 이 규칙으로도 돌아간다.

그런데 관찰을 매일 이어지는 원장(§10)으로 바꾸고 같은 규칙을 적용하면 두 경우 모두 막힌다.

- 관찰이 먼저 날짜를 차지하면, champion 관찰이 다음 challenger의 미래 최종 구간까지 소비한다.
- 최종 구간을 먼저 예약하면, 관찰이 그 날짜에서 막힌다.

그러면 "계속 관찰하면서 새 후보를 독립 검증"하는 루프를 만들 수 없다. 해법은 날짜 사용을 두 종류로 나누고, 동시 사용은 사전 등록한 cohort 안에서만 허용하는 것이다.

### 날짜 사용의 두 종류

| 종류 | 뜻 | 배타성 |
| --- | --- | --- |
| `final_validation` | 고정된 후보의 최종 검증에 쓰는 소비 | 시장 단위로 **배타적**. 한 번 쓴 날짜는 다시 최종 검증에 쓰지 않는다(INV-4) |
| `observation_exposure` | 이미 고정된 버전(`ObservationRelease`)이 그 날짜를 관찰함 | 소비가 아니라 **노출 기록**. 다른 날짜 사용을 막지 않지만, 겹치는 기간의 결과는 봉인 규칙을 따른다 |

### ValidationCohort

최종 검증 구간과 관찰이 같은 날짜를 함께 쓰려면, 구간 첫 실행 시각 전에 cohort를 등록한다.

| 필드 | 뜻 |
| --- | --- |
| cohort_id | 식별자 |
| market_id, window | 시장과 최종 검증 구간 |
| commitment_id | 이 구간에서 평가할 **단일** challenger의 `EvaluationCommitment`(§7) |
| baseline | 비교 대상(현재 champion의 고정 spec) |
| observation_release_ids | 같은 기간에 관찰을 이어 갈 관찰 버전. 보통 현재 champion 하나 |
| registered_at | 등록 시각. 구간 첫 실행 시각보다 앞서야 한다 |
| seal_until | cohort 최종 결정이 확정되는 시점 |

규칙:

- cohort 구간의 `final_validation`은 등록된 `commitment_id` 하나만 쓴다. 구간이 시작된 뒤 추가하는 후보나 다른 연구의 같은 날짜 최종 검증은 거부한다.
- 같은 구간의 `observation_exposure`는 `observation_release_ids`에 등록된 버전만 허용한다. 등록된 관찰 버전은 cohort 구간이 시작되기 **전에** 고정된 것이어야 한다.
- cohort 기간에 나온 관찰 성과·열화 결과는 `seal_until`까지 봉인한다. 이 결과는 후보 선택, 기준 변경, 제안기 입력에 들어가지 않는다. 봉인이 풀린 뒤에는 §9의 snapshot 해제 규칙을 따른다.
- 열화 상태 전이는 봉인 중에도 운영 안전을 위해 계산하고 사람에게 알릴 수 있다. 다만 그 결과로 같은 cohort의 후보·기준·합격 기준을 바꾸지 않는다. 바꾸면 cohort는 E1로 강등되고 그 구간은 소비된 것으로 남는다.
- 여러 challenger를 같은 구간에서 비교하려면 [자기 개선](self-improvement.md) §6의 다중 비교 계약으로 cohort를 등록하고, 예산 계정에서 비교 수만큼 할당받는다. 그 밖의 중복 최종 검증은 허용하지 않는다.
- cohort 없이 관찰만 한 날짜는 `observation_exposure`로 기록되고, 그 관찰 결과는 운영자에게 공개된 것으로 본다. cohort는 구간 시작 전에만 등록할 수 있으므로, 이런 날짜는 나중에 최종 검증 구간으로 쓸 수 없다.

### 현재 코드에서 바뀌어야 할 것

- `agent_intervals`의 `kind`를 배타성 판정에 반영한다(`final_validation`끼리만 배타, `observation_exposure`는 cohort 규칙으로 판정).
- 현재 shadow 소비 기록(`kind = shadow`)은 이전 데이터 호환을 위해 `final_validation`과 같이 배타적으로 취급한다. 새 관찰부터 `observation_exposure`로 기록한다.

완료 조건 회귀:

- 관찰을 중단하지 않고 다음 E2 사이클을 끝까지 완료한다.
- 구간 시작 후 추가한 후보와 다른 연구의 같은 날짜 최종 검증은 거부한다.
- 봉인된 관찰 결과가 제안기에 전달되지 않는다.


### 현재 구현 (LOOP-09a, 합성 회귀 범위)

추가 전용 `agent_cohorts(cohort_id, market, start, end, commitment_id UNIQUE, baseline_version, observation_streams_json, registered_at, first_execution_at, registered_before_start)`를 저장한다. cohort ID는 시장·구간·commitment ID·baseline version·관찰 스트림 목록의 해시다. RES-03a 고정 트랜잭션에서 최종 구간 claim·commitment와 함께 등록하며 commitment payload·ID는 바꾸지 않는다. 등록 시각은 clock, 첫 실행 시각은 최종 시작일 09:00이다. E1도 cohort를 등록하고 시각 비교 플래그를 남기며, E1/E2 등급 판정은 RES-03a 그대로다. UPDATE/DELETE 및 과거 cohort 역채움은 하지 않는다.

저장 kind `holdout`은 `final_validation`을 뜻하며 기존 이름을 유지한다. `EXCLUSIVE_KINDS = {'holdout', 'shadow', 'final_validation'}`끼리는 기존처럼 배타다. 관찰 스트림 owner는 revision과 무관한 `observation:<market>:<family_lineage_id>`이고, `observation_exposure`는 스트림당 한 행의 end만 늘린다. 관찰 노출은 legacy `holdouts`에 기록하지 않는다. 주관자 결정에 따라 shadow와 관찰 노출은 겹칠 수 있다. 최종 검증 claim은 이미 기록된 관찰 노출과 한 날짜라도 겹치면 거부하며 새 cohort로 소급 허용하지 않는다. 고정 이후 늘어나는 관찰 노출만 해당 최종 구간·소유자·스트림을 포함한 cohort로 허용한다. legacy `holdouts`의 shadow 복제 행도 확인하고, 확인되지 않은 legacy/import 최종 검증은 cohort 없이 관찰과 겹칠 수 없다.

관찰 끝은 데이터 끝과 다음 미고정 사이클의 holdout_start 전날 중 빠른 날짜다. COMPLETE/ABANDONED를 제외한 ordinal을 순서대로 찾고, commitment가 있는 구간은 자르지 않는다. `_plan`과 입력 calendar에 다음 시작일이 없거나 남은 예산이 없으면 자르지 않는다. 관찰 세션이 두 개 미만이면 None이다. `observation_limit`은 end·reason(`data_end`/`next_uncommitted_holdout`)·next_holdout_start를 보고한다.

`observation_ledger` 자체는 DB에 쓰지 않는다. `tick`은 status ok인 관찰 구간을 별도 트랜잭션으로 노출 기록하고 `exposure_recorded`를 보고한다. AgentError는 연구 진행과 격리하여 false와 `exposure_error`로 남긴다. 직접 조회의 `exposure_recorded`는 기존 저장 구간이 보고 구간을 덮는지 읽기만 한다. 관찰 보고의 `cohorts`와 `sealed`는 겹치는 cohort의 최종 결정이 COMPLETE/ABANDONED 전인지 나타낸다. `status(name).cohorts`는 이 연구의 cohort와 등록 시각 비교·봉인 상태를 보고한다. 관찰 성과는 계속 `proposer_visible=false`이고 개발 기억과 commitment의 proposer_inputs에 넣지 않는다.

기존 DB의 holdout/shadow/import 행에는 cohort를 역채우지 않으며 날짜·소유자·소비 기록을 보존한다. cohort 없는 기존 holdout/final_validation은 관찰 노출과 겹칠 수 없고, 기존 shadow는 최종 검증과 배타성을 유지하면서 관찰 노출과는 겹칠 수 있다. 관찰 checkpoint 저장·열화 상태·봉인 연동은 LOOP-05b 합성 회귀 범위에서 구현했다. 다중 challenger cohort(자기 개선 §6)와 ObservationRelease(LOOP-06)는 미구현이다. 실데이터 검증이나 운용 준비 완료를 뜻하지 않는다.
