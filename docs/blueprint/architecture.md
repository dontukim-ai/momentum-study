# 상세 구조·계약·구현 방안

상위 기준: [블루프린트](README.md). 구현 현황은 [현황표](status.md), 작업 순서는 [개발 계획](implementation-plan.md)을 따른다. 아래의 **현재**는 코드에 존재하는 기능, **목표**는 추가 구현 계약이다.

## 1. 패키지와 계층

| 계층 | 현재 코드 | 역할과 경계 |
| --- | --- | --- |
| 인터페이스 | [`cli.py`](../../src/momentum/agent/cli.py) | init/run/status/recover/abandon/import-history/demo, 파일 감시, JSON 저장 |
| 계약 | [`contracts.py`](../../src/momentum/agent/contracts.py) | AgentPolicy, ResearchInput, AnnouncedAction, 빌드 ID·시점·prefix |
| 연구 제어 | [`service.py`](../../src/momentum/agent/service.py) | 일정, 등록·소비, 후보 평가, 연구 champion 갱신, 복구, shadow |
| 선정·제안 | [`selection.py`](../../apps/kis-agent/src/trading_agent/selection.py), [`logic.py`](../../apps/kis-agent/src/trading_agent/logic.py) | 시점 기반 필터·순위, 제한된 파라미터 후보 |
| 공통 평가 | [`evaluator.py`](../../src/momentum/agent/evaluator.py) | 일봉 다음 시가 체결 proxy, 공통 비용·위험·회계 연결 |
| 원장 | [`ledger.py`](../../src/momentum/ledger/ledger.py) | Decimal 현금·보유·배당채권·분할·독립 NAV |
| 저장 | [`store.py`](../../apps/kis-agent/src/trading_agent/store.py) + agent 테이블 | SQLite WAL, BEGIN IMMEDIATE, 과거 KIS 평가 소비와 연결 |
| 증권사 읽기 | [`kis_real.py`](../../apps/kis-agent/src/trading_agent/kis_real.py) | 실전 시세·잔고·최근 주문 읽기, 주문 API 거부. 연구 수집기로 자동 연결되지는 않음 |

미국 가설 코드는 보존·보류한다. 공통 원장 유지와 KRX 연구 개발은 계속할 수 있다. 기존 KIS 앱과 root 패키지의 평가 정책·DB 스키마·CLI를 한꺼번에 덮어쓰지 않는다.

## 2. 데이터 경로

**현재:** CSV/JSONL/JSON → 유효성 검사 → ResearchInput → 개발 prefix 또는 평가 prefix → 선정·평가. 공급자가 만든 파일을 읽으며 API에서 자동 수집하지 않는다.

**목표 (`DATA-01~06`):** 공급자 원본 보관 → 정규화 → 품질 검사 → 같은 버전의 가격·종목·달력·기업행동 묶음 공개 → 연구 실행. 수집 재시도와 최종 평가 재시도를 분리한다. 원본 오류를 수정할 때 이전 버전은 유지한다.

| 데이터 | 현재 계약 | 추가할 계약 |
| --- | --- | --- |
| 가격 | session/symbol/OHLC/volume/available_at/source, 원시 정수 원 가격 | 공급자 버전·수신 시각·정정 이력·누락 사유, 공급 지연과 최초 공개 시각의 구별 |
| 종목 이력 | effective_at/available_at, 시장·유형·거래 상태·주의 플래그·bid/ask | 영구 종목 ID, 코드 변경·합병·상장/폐지 이력, 필드별 출처·커버리지 |
| 달력 | 정렬된 명시 세션 배열, 가격 행의 날짜가 세션의 부분집합 | 실제 개장·폐장·휴장·단축장·특별장 시각, 거래소 버전 |
| 기업행동 | 분할/현금배당, ex/pay/ratio/amount/공개 시각 | 정정·합병·권리·현금청산·분배금·원천징수 범위, 신호용 가격 보정 계약 |
| 비용 | commission/sell_tax/slippage bps 시나리오 | 종목 유형·날짜·계좌별 근거와 유효 기간, 호가·정산·환급 구분 |
| 계좌 읽기 | 실전 읽기 어댑터의 현금 의미 구분 | 주문 가능 금액·기존 보유·미체결·결제 예정액과 일관된 시각의 계좌 snapshot |

목표 데이터 묶음의 식별자에는 구성 파일 해시·스키마 버전·대상 세션·공개 마감 시각·품질 상태를 포함한다. 아직 `dataset_bundle` manifest 파일은 구현되어 있지 않다. 현재 CLI의 JSON 출력 교체가 원자적이라고 해서 입력 여러 파일의 교체도 원자적인 것은 아니다.

### 다음 구현에서 고정할 교환 계약 — 설계안

아래 이름·필드는 신규 계약안이며 현재 CLI가 이 JSON을 직접 받는다는 뜻은 아니다. 스키마 버전을 고정하고 unknown 필드를 거부하는 방식으로 구현한다.

| 계약안 | 필수 필드 | 검증과 인계 |
| --- | --- | --- |
| DataBundle | schema_version, bundle_id, known_as_of, session_range, files(path/hash/schema), coverage, quality, source_versions | 파일 해시·시점·필드 커버리지 통과 후 포인터 공개. 실제 자료와 예제 자료 구분 |
| CandidateProposal | proposal_id, parent_version, search_space_id, development_context_hash, allowed_parameters, hypothesis, falsification, proposer_version | 개발 context와 허용 공간 일치. 위험·비용·합격 기준·실행 코드는 입력으로 받지 않음 |
| ExperimentCommitment | cohort_id, policy_id, candidate_budget, calendar_version, future_windows, exposure_status, environment_id | 새 결과 도착 전에 등록한 구간인지 확인. 현재 계산 직전 manifest와 연결 |
| FailureCase | candidate_id, development_range, data_id, failure_code, evidence_ref, visible_scope, next_action | 개발 가설 추천용 정보와 최종 표본 노출 정보를 구분 |
| ObservationRelease | research_revision, selector_id, costs_id, risk_id, dataset_basis, evidence_refs, observation_state | 운용 후보가 원래 선정·비용·위험 계약을 잃지 않음 |
| RuntimeHealth | worker_id, build/environment_id, last_bundle, last_success, pending_cycle, wait_reason, remaining_budget | 프로세스 생존과 데이터/연구 정상 상태를 구별 |

필드가 부족하면 가정값을 넣어 통과시키기보다 지원 불가·품질 보류를 반환한다. 기존 파일 계약은 어댑터로 유지하며, 새로운 계약을 넣는 변경에도 이전 소비 이력은 보존한다.

## 3. 시점·결측·정정 규칙

| 상황 | 현재 처리 | 구현 보강 |
| --- | --- | --- |
| 미래 공개 가격·종목 상태 | Snapshot.view / SecurityHistory가 관측 시각 이후 정보를 제외 | 실제 공급자의 최초 공개와 정정 공개 기록 확보 |
| 과거 가격·종목·달력·행동 prefix 변경 | checkpoint 해시가 다르면 거부 | 분기 데이터 버전·영향 분석과 연구 마이그레이션 도구 |
| 늦게 공개된 종목 상태 정정 | available_at을 보존한 새 이력만 이후 시점에 사용 | 종목 ID 변경까지 연결 |
| 거래일 전체의 가격 누락 | 명시 달력이 있으면 연속 이력을 끊어 처리 | 실제 달력 공급자와 누락 사유 코드 |
| 현재 봉·메타데이터·spread 없음 | 제외 사유를 기록 | 수집 실패와 실제 거래 불가 구별 |
| 보유 종목 가격 없음 | 평가 실패. 0원 청산·이전 종가 가상 체결 없음 | 검증된 stale mark/청산 정책을 별도 정책 버전으로 설계 |
| ex-date 개장 이후에야 공개된 기업행동 | 현재 계약은 평가를 거부 | 지원 가능한 공식 공개·실제 경제 효력 계약 검토 |

시각은 timezone-aware이며 KRX 계산은 현재 고정 09:00/15:30/16:00 기준을 사용한다. 기본 freshness 86,400초는 거래소 세션을 이해하지 못하므로 주말·연휴 뒤 상태가 오래됐다고 판정할 수 있다. `DATA-03`에서 휴장과 실제 데이터 지연을 구별해야 한다.

## 4. 후보 선정

**현재:** 시점별 이력과 자본을 입력받아 후보 특징, 제외 사유, 순위, 선택 종목과 목표 비중을 반환한다. 순위는 전략별로 달라도 동일한 선정·위험 필터를 통과해야 한다.

| 설정 | 현재 기본값 | 의미 |
| --- | --- | --- |
| 자본 / 총 노출 / 종목 비중 / 주문 금액 | 100만 원 / 80% / 40% / 40만 원 | 연구의 현금·위험 계산 상한 |
| 선택 슬롯 | 2 | 부족한 슬롯은 현금. 남은 종목에 자동 집중하지 않음 |
| 시장 / 유형 | KOSPI·KOSDAQ / STOCK·ETF | KONEX·ETN 등은 기본 제외 |
| 최소 가격 / 이력 / 특징 창 | 1,000원 / 60세션 / 20세션 | 단기 신호에도 충분한 스크리닝 이력을 요구 |
| 평균 거래량 / 거래대금 proxy | 10,000주 / 1억 원 | 거래대금은 close × volume의 근사 |
| 일 변동성 / spread | 8% 이하 / 30bps 이하 | 일 변동성은 특징 창의 표준편차 |
| 상태·최종 봉 허용 나이 | 86,400초 | 실제 거래일 달력 보완 필요 |

거래 정지·상장폐지·관리·주의·레버리지·인버스·메타데이터 미상은 제외한다. 매도 가능 여부와 신규 매수 가능 여부는 별도 확인한다. 위 값은 현행 연구 기본값이며 실계좌 손실 한도의 승인이나 상품별 실제 수수료를 뜻하지 않는다.

**목표:** 영구 ID와 실제 시장 상태를 반영하고, 예산 내 1주 매수 가능성·거래 비용·회전율을 선정 이유와 함께 기록한다. 신용·차입·외부 입출금·기존 실보유는 실행 계층에서 별도 통제한다.

## 5. 계획·체결·비용

**현재:** 신호 시각의 가격으로 원래 수량을 계산하고, 다음 개장 가격으로 체결 또는 축소한다. 이전 일봉 거래량의 1%를 체결 수량 cap으로 사용한다. 매도를 먼저 처리하고 다음 매수를 처리한다.

```text
사용 자본 = min(관측 시점 NAV, 연구 자본 상한)
종목 예산 = min(사용 자본 × gross_weight / top_n,
               사용 자본 × max_position, max_order_notional)
계획 목표 수량 = floor(종목 예산 / 관측 시점 종가)
개장 매수 수량 <= 이미 계획한 수량
```

다음 시가가 싸졌다는 이유로 원래 수량을 늘리지 않는다. 단, 사전에 알려진 분할은 같은 경제적 노출을 유지하도록 수량 단위를 조정한다. 체결 로그는 원래 시가와 시나리오 slippage 반영 가격을 구분한다.

| 현재 구현 | 후속 구현 (`FIN-01~04`) |
| --- | --- |
| 원 단위 실행 가격·수수료 반올림 | 실제 가격대·상품별 호가와 비용 최소 단위 |
| 기본 commission 1.5 / sell tax 20 / slippage 5bps | 실계좌·상품·날짜별 확인된 비용. ETF에도 동일 매도세가 적용되는 현재 시나리오 교체 |
| 수수료·slippage 2배 스트레스 | 유동성·갭·미체결·부분 체결의 검증된 추가 시나리오 |
| 일봉 cap·전량/축소 체결 proxy | 개장 경매·호가 대기열·정지·실제 유효 주문 조건 |
| 매도 후 현금 즉시 반영 | 계좌의 주문 가능 현금·결제 일정을 검증한 모델 |

## 6. 원장과 성과

금액은 Decimal이다. 원화 quantum은 1원이며 금액 정밀도와 실제 시장 호가 단위는 다른 계약이다. 원장은 현금, 주식 수, 원가, mark, 배당채권과 독립 NAV를 기록한다.

```text
NAV = 현금 + 보유 평가액 + 미지급 배당채권
현금 >= 0
배당채권은 목표 비중의 NAV에 포함하지만 매수 가능 현금에는 포함하지 않음
```

분할은 정수 주식 변환만 지원한다. 현금배당은 ex-date 채권 인식, pay-date 현금 전환이다. 수정 총수익률을 원시 가격 원장에 다시 곱하지 않는다. 기업행동 회계가 존재해도 기존 KIS 신호의 분할 보정·분배금 포함 수익률까지 완성됐다는 의미는 아니다.

각 개발 fold·holdout·shadow 블록은 별도 초기 자본과 포지션으로 시작한다. 블록 간 실제 보유를 이어가는 원장·성과 조정은 후속 대상이다. 현재 기말 수익률은 보유 평가 NAV이며 강제 청산 비용을 모두 차감한 현금 수익률이 아니다. `FIN-03`에서 평가 NAV와 청산 NAV·기말 inventory를 분리한다.

## 7. 저장·식별자·무결성

| 저장 대상 | 현재 테이블/식별자 | 유지할 계약 |
| --- | --- | --- |
| 연구 정책·현재 전략 | agent_studies, revision | 초기 정책·빌드 고정, 연구 champion만 갱신 |
| 한 번의 평가 일정 | agent_cycles, manifest 해시인 cycle_id | ordinal별 1건, 중단 상태에서 새 실행 차단 |
| 평가 계약 | evaluation_manifests | 평가 전에 데이터·코드·비용·일정·후보 공간 저장 |
| 시장 단위 소비 | agent_intervals | holdout/shadow의 owner와 구간 보존 |
| 과거 KIS 소비 | holdouts + guard_unified_interval | 새/구 CLI와 종목명 변경으로 우회하지 않음 |
| 전진 결과 | agent_shadow | 고정 시작점의 최신 끝 시점·입력 해시·결과 해시 |
| 운영 이벤트 | events | 완료·중단 폐기·이력 가져오기 기록 |

`build_id`는 두 패키지 Python 소스의 해시다. 현재 Git commit·의존성 lock·실행 이미지·모델 버전까지 모두 기록하는 환경 manifest는 없다. `OPS-04`에서 소스 ID와 실행 환경 ID를 분리한다. 해시는 우발적 변경 탐지이며 DB 쓰기 권한이 있는 공격자에 대한 서명·외부 감사 저장소가 아니다.

SQLite WAL과 BEGIN IMMEDIATE, pending cycle의 unique index, champion revision 검사가 같은 DB의 동시 변경을 막는다. 여러 DB나 서버에 자동 분산 잠금은 없다. 소비 이력 가져오기는 원본의 구간만 복사하며 champion·학습 메모리·실행 상태 전체를 마이그레이션하지 않는다.

## 8. 외부 기능과 분리 경계

| 기능 | 현재 상태 | 통합 조건 |
| --- | --- | --- |
| 실전 KIS 읽기 | 어댑터·진단·수기 fixture 존재, 실제 계약 검증 미완료 | 실제 읽기 응답·현금 의미·페이지 일관성 확인 |
| KIS 모의 OMS | 별도 코드 존재, UNKNOWN 재전송 차단 등 fixture 검증 | 연구 결과와 selector 계약을 유지하는 연결, 실제 모의 서버 검증 |
| 기존 release/shadow | 별도 momentum 흐름에 존재 | selector-bound 결과를 버리고 이전 경로로 승격시키지 않음 |
| LLM 제안 | 기존 KIS 앱에 strict JSON 경로 존재 | 통합 CLI에는 미연결. 개발 전용 입력·예산·출처 계약 필요 |
| 실전 주문 | 미구현·현재 후순위 | 별도 운용 단계에서 계좌·주문·위험 계약을 완성 |

신규 수집·알림·모델 API의 자격증명은 실행 환경 비밀 설정을 사용하고 연구 결과에 키·토큰·계좌 원문을 넣지 않는다. 본 설계 작성에서 실제 API 호출이나 주문을 수행하지 않는다.
