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
| session_times | 개장·폐장·확정봉 공개 시각(시간대 포함) | 09:00 / 15:30 / 16:00 KST(현재 고정값) |
| tick_size_rule | 가격대·상품별 호가 단위 규칙 ID | FIN-01 결과 |
| cost_model_id | 상품·날짜·계좌별 비용 모델 | FIN-01 결과. 현재 예제 1.5/20/5bps |
| trading_constraints | 상하한가, 거래 정지, 공매도, 단주 등 | 현재 정지·관리·주의·레버리지·인버스 제외 |
| universe_filter_id | 기본 유니버스 필터 | KOSPI·KOSDAQ, STOCK·ETF |
| freshness_rule | 데이터 신선도를 세션 단위로 판정하는 규칙 | 현재 86,400초 벽시계 기준 → 세션 기준으로 교체(DATA-03) |

완료 조건: KRX 동작을 바꾸지 않고 `MarketProfile`로 옮긴 뒤 기존 회귀 20개가 그대로 통과한다. 미국 시장 profile은 정의할 수 있어도 최종 평가는 계속 잠근다(회귀 17 유지).

## 2. StrategyFamily — S1 (LOOP-02)

champion 갱신의 단위이자 탐색 예산의 단위다.

| 필드 | 뜻 | 현재 대응 |
| --- | --- | --- |
| family_id | 전략군 식별자 | 암묵적 `ranked-momentum` 하나 |
| market_id | 적용 시장 | KRX |
| signal_contract | 신호 정의·특징·필요 이력 | momentum / risk_adjusted / trend_stability |
| parameter_space | 파라미터별 허용 범위와 등록 값 | lookback {5,10,20}(허용 2~60), ranking 3종 |
| fixed_parameters | 이 전략군에서 바꾸지 않는 값 | top_n 2, gross 0.8, min_momentum 0 |
| rebalance_cadence | 재선정·리밸런싱 주기 | **매 세션**(현재 평가기가 세션마다 재선정). 명시 파라미터로 승격 |
| holding_horizon | 신호가 겨냥하는 보유 기간 | 미정. 블록 길이·purge 규칙의 입력(RES-02) |
| search_budget | 전략군 전체 누적 예산(사이클 수·후보 수) | 연구 단위 8 사이클·8 challenger |

규칙: 파라미터 공간이나 고정값을 바꾸면 새 `family_id` 또는 새 버전이고, 예산은 이어서 센다(INV-7). 다른 전략군끼리의 champion 비교는 S2의 기준 전략 비교로 한다.

## 3. CandidateProposal 보강 — S1 (RES-04, RES-05)

[상세 구조](architecture.md) §2의 필드에 다음을 더한다.

| 필드 | 뜻 |
| --- | --- |
| family_id | 소속 전략군 |
| proposal_kind | `grid`(사전 등록 열거) / `hypothesis`(실패 기억 기반) / `model`(LLM·학습) |
| evidence_refs | 근거가 된 개발 구간 결과·`FailureCase` ID. 최종·추적 구간의 gate 결과는 넣을 수 없음(INV-1) |
| expected_effect | 기대 효과와 측정 지표(예: 회전율 −20%) |
| falsification | 개발 구간에서 이 가설을 기각할 조건 |
| ancestor_ids | 이 후보를 낳은 이전 후보 |
| duplicate_key | 가설·데이터 버전·개발 구간·정책을 묶은 중복 판정 키 |

현재 grid 제안은 `proposal_kind = grid`, 가설 필드는 비어 있는 것으로 기록한다. grid 열거를 "개선 가설"이라고 부르지 않는다.

## 4. EvaluationContract — S2 (LOOP-03)

"같은 조건"을 하나의 ID로 묶는다. 계약 ID가 다른 결과는 비교하지 않는다.

| 필드 | 뜻 | 현재 대응 |
| --- | --- | --- |
| data_bundle_id | 평가 입력 묶음 | `ResearchInput.fingerprint(end)` |
| market_profile_id | 시장 규칙 | 없음(코드 고정) |
| execution_model | 수량 계획 시각, 체결 가격, 거래량 상한, 매도 우선 | 전일 종가 계획, 다음 시가, 전일 거래량 1%, 매도 먼저 |
| cost_model_id, stress_scenarios | 비용과 스트레스 | 1.5/20/5bps, 수수료·slippage 2배 |
| capital_rule | 사용 자본 | `min(관측 시점 NAV, 연구 자본 상한)` |
| position_reset | 평가 구간마다 초기화 여부 | 초기화(`positions_reset_per_evaluation: true`) |
| rebalance_clock | 재선정 시각·주기 | 매 세션, 전일 16:00 관측 |
| ledger_version | 원장 계약 | `Ledger` 소스 해시(현재 `build_id`에 포함) |
| benchmarks | 함께 평가할 기준 전략 | 기존 champion만 → 현금, 동일 비중 유니버스, 시장 ETF 추가(RES-01) |
| metrics_spec_id | 지표 정의 표(§5) | 없음 |

### EvaluationResult에 추가할 거래 가능성 지표

| 지표 | 정의 |
| --- | --- |
| fill_ratio | 체결 수량 / 계획 수량 |
| volume_capped_ratio | 거래량 상한 때문에 줄어든 수량의 비율 |
| cancelled_orders | 결측 시가·정지 등으로 취소된 주문 수 |
| unaffordable_rate | 예산 안에서 1주도 살 수 없어 제외된 후보 비율 |
| turnover | 기간 거래대금 / 평균 NAV |
| capacity_estimate | 체결률이 기준 아래로 떨어지기 시작하는 자본 규모(민감도) |

## 5. 지표 정의 표 — S2·S3 (LOOP-07)

현재 신뢰구간은 **일 수익률 차이의 평균**으로, 순개선 gate는 **누적 순수익률 차이**로 계산한다. 둘은 다른 지표라서 한쪽만 통과할 수 있다. 지표 정의를 한 표로 고정하고 gate마다 어느 지표를 쓰는지 명시한다.

| 지표 | 정의 | 쓰는 곳 |
| --- | --- | --- |
| daily_net_return | 비용 차감 NAV의 일간 변화율 | 개발 점수, 신뢰구간 |
| period_net_return | 구간 시작 대비 끝 NAV 비율 − 1(평가 NAV, 청산 비용 미차감) | 순개선 gate, 비용 스트레스 |
| max_drawdown | 일별 NAV 기준 최대 낙폭 | 개발·최종 낙폭 gate, 열화 |
| fills | 체결 기록 수(왕복 거래 수 아님) | 활동 gate |

정책 v2에서는 순개선 gate와 신뢰구간을 같은 지표로 맞추는 방안을 검토한다. 기존 연구의 정책은 바꾸지 않는다(INV-6).

## 6. CycleCommitment — S3 (RES-03)

현재 사이클 manifest는 최종 구간 데이터가 도착한 뒤, 계산 전에 저장된다. 이를 둘로 나눈다.

| 단계 | 저장 시점 | 내용 |
| --- | --- | --- |
| commitment | 최종 구간 **첫 세션 공개 전** | family_id, 정책 ID, 후보 공간·예산, 합격 기준, `EvaluationContract` ID(데이터 묶음 제외), 구간 시작·끝 세션(달력 버전 포함), 저장 시각 |
| manifest | 구간 도착 후, 계산 전(현재와 같음) | commitment ID, 실제 data_bundle_id, 개발 선택 결과 |

증거 등급 판정: commitment 저장 시각 < 구간 첫 세션의 최초 공개 시각이면 E2, 아니면 E1. 저장 시각을 외부에서 확인할 수 있게 하는 방법(별도 저장소, Git 태그 등)은 RES-03에서 정한다.

## 7. ChampionRegistry — S4 (LOOP-04)

| 필드 | 뜻 |
| --- | --- |
| key | `(market_id, family_id)` |
| revision | 단조 증가 번호 |
| spec, evaluation_contract_id | 현재 champion과 평가 조건 |
| evidence_grade | 이 revision을 만든 증거 등급(E1/E2/E3) |
| source_cycle_id | 근거 사이클 |
| previous_revision | 강등 시 돌아갈 revision |

현재 `agent_studies.champion_json`/`revision`은 연구 단위다. 이전 경로: 기존 연구 하나를 `(KRX, ranked-momentum)` 키로 등록하고, 같은 키에 다른 연구가 champion을 쓰려면 같은 전략군 예산을 공유하게 한다.

## 8. 연속 추적 원장과 열화 정책 — S4 (LOOP-05)

### 추적 원장

- champion revision이 바뀌어도 이어지는 시장·전략군 단위 원장이다. revision 경계를 기록한다.
- 매 세션 champion, 직전 champion, 기준 전략(현금·단순 전략)의 성과를 같은 `EvaluationContract`로 기록한다.
- champion을 유지한 사이클에서는 직전 champion이 같으므로 비교 대상은 기준 전략이 된다.
- 현재 20세션 shadow 블록은 원장의 한 구간 보고로 남긴다.

### 열화 상태

| 상태 | 진입 조건(예시, 정책으로 고정) | 동작 |
| --- | --- | --- |
| OBSERVING | 기본 | 기록만 |
| WARNING | 추적 낙폭이 정책 한도 초과, 또는 기준 전략 대비 누적 열세가 사전 등록 임계 초과 | 보고·알림, 원인 분류 |
| DEMOTION_CANDIDATE | WARNING이 사전 등록 기간 이상 지속 | 사람 확인 후 이전 revision 또는 이전 Release로 강등 |

규칙:

- 현재 열화 flag는 1원만 뒤처져도 켜진다. 정책에서는 임계와 지속 기간을 **사전 등록**한다.
- 강등은 이미 검증된 이전 revision으로만 한다. 강등을 이유로 과거 구간을 다시 합격시키지 않는다.
- 열화 판정은 S1의 실패 기억(`FailureCase`)으로 들어간다.

## 9. FailureCase 실패 코드 — S4 → S1 (RES-04)

[상세 구조](architecture.md) §2 `FailureCase`의 `failure_code` 값을 고정한다.

| 코드 | 뜻 | 다음 S1 입력으로 쓰는 방식 |
| --- | --- | --- |
| DATA_DEFECT | 누락·상태 미상·정정 | 수익 개선 가설로 쓰지 않음. 데이터 작업으로 보냄 |
| DEV_NO_IMPROVEMENT | 개발에서 champion보다 나은 후보 없음 | 같은 공간의 재시도 비용 기록 |
| DEV_RISK | 개발 낙폭·활동 조건 실패 | 위험·활동 관련 가설 후보 |
| FINAL_UNDERPERFORM | 최종 순개선 또는 비용 스트레스 실패 | 같은 구간으로 재선택 금지. 비용·회전율 가설 후보 |
| INSUFFICIENT_EVIDENCE | 신뢰구간 하한 ≤ 0 또는 관측 수 부족 | 새 데이터 대기. 길이·블록을 바꿔 재검정 금지 |
| TRACKING_DEGRADED | 추적 열화 상태 진입 | 국면·비용·데이터 변화 조사 |

## 10. Release — S4 → 운용 (LOOP-06)

[상세 구조](architecture.md) §2의 `ObservationRelease`를 운용 전환 계약으로 확장한다.

| 필드 | 뜻 |
| --- | --- |
| release_id | 운용 버전 식별자 |
| champion_key, champion_revision | 근거 champion |
| evidence_grade | 최소 E2 |
| selector_policy_id, cost_model_id, risk_limits_id | 운용에서 그대로 쓸 계약 |
| target_mode | `paper` / `live` |
| approved_by, approved_at | 사람 승인 |
| rollback_release_id | 강등 시 돌아갈 Release |
| capital_scope | 전체 운용 자본 상한(100만 원은 모든 주문에 걸친 총 상한) |

Release는 주문을 만들지 않는다. 주문 경로(ORD-02~04)가 Release를 읽어 실행한다.
