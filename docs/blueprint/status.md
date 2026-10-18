# 현재 진행 현황 — 구현과 검증을 분리한 기준표

작성일: 2026-10-16 KST. 구현 기준 `74e07e9`, 미국 연구 보류 기준 `577fede`. 본 표는 [블루프린트](README.md)의 현황판이다. C1~C4 구현으로 평가 계약·거래 가능성·기준 전략 보고·opt-in 통계 v2를 추가했다. 실데이터/상시 운영은 미검증이고 미국 연구는 계속 보류다.

## 1. 지금의 결론

**네 단계 자동 연구 v1은 코드와 합성 증거가 있다. 실제 시장 데이터와 상시 운용의 증거는 아직 없다.** 국내 후보 선정·공통 회계·제한된 파라미터 탐색·독립 구간의 논리적 격리·연구 champion 갱신·전진 replay·복구를 실행할 수 있다.

| 지금 가능 | 아직 완료되지 않은 것 |
| --- | --- |
| 키 없이 합성 네 단계 실행 | 실제 가격·역사 종목 상태·달력·기업행동 자동 수집 |
| 준비된 파일로 KRX/KRW 연구 | 실제 주식/ETF·계좌별 비용·호가·결제 검증 |
| 새 파일 감시·데이터 부족 시 대기 | 상시 서버·감시·알림·일관 backup/복원 운영 |
| 제한된 grid 탐색과 champion revision | 실패 지식으로 탐색 설계 변경, ML 모델 재학습 |
| 사용 구간 보존·복구·중복 차단 | 물리적 평가 격리, 연구군 전체 예산·외부 노출 관리 |
| 정해진 새 shadow 블록의 성과 비교 | 실제 계좌 포지션 연속 운용·브로커와의 자동 대사 |

### 4단계 루프 기준 요약

[블루프린트](README.md) §4의 단계별 현황이다. 증거 등급은 블루프린트 §5를 따른다.

| 단계 | 상태 | 지금 되는 것 | 목표와의 핵심 차이 | 작업 |
| --- | --- | --- | --- | --- |
| S1 후보 생성 | 부분 | KRX 필터·3가지 ranking·고정 grid 제안 | 시장 추상화·전략군 레지스트리·결정 주기 파라미터·가설형 제안·실패 기억 활용 없음(해제 기록은 LOOP-08a 합성 회귀 구현) | LOOP-01, LOOP-02, LOOP-08, RES-04, RES-05 |
| S2 동일 조건 평가 | 부분 | 공통 평가기·원장·비용 스트레스 | 평가 계약 ID·동일 조건 검사·거래 가능성 지표 구현(합성 회귀). 현금/적격 동일 비중 보고 구현. 기준 전략 gate·capacity는 후속 | LOOP-03, RES-01, FIN-01~04 |
| S3 독립 검증 | 부분 | 개발 종료 시 후보 사전 고정·구간 예약·시장 단위 소비·승격 조건 10개·E1/E2 기록(RES-03a 합성 회귀), kind 분리·고정 트랜잭션의 ValidationCohort 등록(LOOP-09a 합성 회귀) | 외부에서 확인 가능한 고정 시각(별도 저장소·서명)은 미구현. 시장·전략군 예산 원장 구현(LOOP-02a 합성 회귀), 명시적 할당은 미구현. opt-in 통계 v2와 합성 진단 도구 구현; 실제 오류율·개발 블록 선정 근거는 후속 | RES-02, RES-03, LOOP-07 |
| S4 갱신·추적 | 부분 | 시장·전략군 champion 레지스트리·신규 연구 동기화·legacy 이행(LOOP-04a 합성 회귀), 유지·20세션 shadow, 연속 관찰 원장(LOOP-05a 조회는 미저장), checkpoint 저장·사전 등록 열화 상태·cohort 봉인/기억 연동(LOOP-05b 합성 회귀 범위 구현), 관찰 노출 기록·다음 미고정 최종 구간 앞 상한·cohort 봉인 표시(LOOP-09a 합성 회귀 범위 구현) | ObservationRelease 등록·중단(주문 권한 없음)은 LOOP-06a 합성 회귀 범위 구현. 강등 실행·ExecutionRelease·알림·실데이터 검증, 다중 challenger cohort(자기 개선 §6)는 미구현 | LOOP-04~06, LOOP-09, OPS-05, OPS-06 |

상태는 **완료 / 부분 / 미착수 / 보류**다. '완료'는 해당 코드·시험 범위에 한정하며 실거래 준비 완료를 뜻하지 않는다. 전체 완성률 숫자는 데이터·코드·운영을 혼합하므로 사용하지 않는다.

## 2. 완료한 부분 — 코드 근거

| ID | 완료 기능 | 코드 근거 | 완료 증거 / 범위 |
| --- | --- | --- | --- |
| CODE-01 | canonical Git 저장소와 독립 KIS 앱 통합 | [root README](../../README.md), [패키지 설정](../../pyproject.toml) | 기존 엔진·앱 경로·환경 유지, agent extra·CLI 추가 |
| CODE-02 | 시점 기반 종목 상태·가격 조회 | [data.py](../../apps/kis-agent/src/trading_agent/data.py), [selection.py](../../apps/kis-agent/src/trading_agent/selection.py) | 미래 공개 제외·정정 시각·미상 상태·결측 처리 fixture |
| CODE-03 | 후보 필터·3가지 ranking | [selection.py](../../apps/kis-agent/src/trading_agent/selection.py) | 선정/제외 사유, 유동성·가격·spread·이력·예산 |
| CODE-04 | 사전 등록 lookback/ranking 제안 | [logic.py](../../apps/kis-agent/src/trading_agent/logic.py) | 제한 공간·후보 예산, 위험·비용·임계값 변경 금지 |
| CODE-05 | 공통 평가와 전일 수량 계획 | [evaluator.py](../../src/momentum/agent/evaluator.py) | 다음 시가 하락으로 최초 수량 증가 안 함, 결측 시가 취소 |
| CODE-06 | 원화 회계·분할·배당·독립 NAV | [ledger.py](../../src/momentum/ledger/ledger.py), [evaluator.py](../../src/momentum/agent/evaluator.py) | 원 단위·채권/지급일·정수 split·미지급 배당의 NAV/현금 구별 |
| CODE-07 | 동결 정책·데이터 prefix·소스 ID | [contracts.py](../../src/momentum/agent/contracts.py) | 변경된 과거 입력·다른 빌드·미지원 시장 거부 |
| CODE-08 | 개발 fold·기본 최종 gate | [service.py](../../src/momentum/agent/service.py) | 비용 차감 비교·활동·낙폭·block 추정·비용 스트레스 |
| CODE-09 | manifest·시장 단위 소비·구 CLI 연동 | [service.py](../../src/momentum/agent/service.py), [research.py](../../apps/kis-agent/src/trading_agent/research.py), [store.py](../../apps/kis-agent/src/trading_agent/store.py) | 평가 전 저장, 연구/종목명 변경·기존 CLI로 재사용 차단 |
| CODE-10 | 연구 champion 갱신·유지·revision | [service.py](../../src/momentum/agent/service.py) | gate 전체 통과만 갱신, 실패/동점/증거 부족은 유지 |
| CODE-11 | 중단 복구·폐기·같은 DB 동시 실행 방어 | [service.py](../../src/momentum/agent/service.py) | 완성 증거만 recover, 소비 보존, pending cycle·revision 충돌 거부 |
| CODE-12 | 파일 감시·전진 shadow·이력 가져오기 | [cli.py](../../src/momentum/agent/cli.py), [service.py](../../src/momentum/agent/service.py) | 새 입력·cache·고정 shadow 시작점·읽기 전용 소비 import |
| CODE-13 | 합성 종단 예제·통합 회귀 | [demo.py](../../src/momentum/agent/demo.py), [test_agent.py](../../tests/unit/test_agent.py) | 승격·유지·재실행·전진·복구·입력 격리 등의 분기 |

## 3. 목표 기준에서 부분 구현인 부분

| 영역 | 지금 있는 것 | 남은 차이 | 작업 ID |
| --- | --- | --- | --- |
| 데이터 | 파일 계약·시점 조회·prefix | 실제 공급자·역사적 상태·영구 ID·정정 원본 | DATA-01,02,05,06 |
| 달력 | 명시 세션·고정 KRX 시각 | 실제 휴장·특별장·주말 freshness | DATA-03 |
| 입력 일관성 | 출력 JSON 원자적 교체 | 가격/상태/달력/행동 입력 bundle의 원자적 공개 | DATA-04 |
| 비용·호가 | 예제 bps·원 단위; FIN-01a 과거 호가 없음 사전 등록 모드·편도 25bp 하한·관찰 release 실행 자격 표시(합성 회귀 범위 완료) | 주식/ETF·유효 날짜·계좌·호가·정산의 실제 계약 | FIN-01 |
| 기업행동 | raw ledger split/dividend | 신호 보정·정정·권리·합병·일부 미지원 행동 | FIN-02 |
| 경제 평가 | NAV·현금·채권·일봉 체결 | 실제 결제·기말 청산·장중/부분 체결·capacity | FIN-03,04 |
| 비교군 | champion 및 현금·적격 동일 비중 성과/투자·현금 비중/보유 수/예산 부족 보고(최종/유지/shadow) | 기준 전략을 승격 gate에 반영하는 새 정책·실제 ETF 가격/메타데이터/비용 검증 | RES-01 |
| 독립 검증 | 개발 prefix 기반 EvaluationCommitment·최종 구간 사전 예약·E1/E2 기록·재시작 복구(RES-03a 합성 회귀) | 실데이터 전진 검증·OS 격리·외부에서 확인 가능한 고정 시각(별도 저장소·서명)·외부 노출 증거 | RES-02,03 |
| 통계 | 기본 v1 유지, opt-in 단측 v2·꼬리 draw 하한·합성 AR(1)/seed/블록 진단 | 실데이터 블록 고정·오류율 검토·전략군 전체 예산·학습 라벨 격리 | LOOP-07, RES-02,03 |
| 기억 | 실험·사유·gate·shadow 저장 | 실패 분류·중복 가설·개발 전용 기억 검색 | RES-04 |
| 제안 | grid, 별도 앱의 strict JSON 코드 | 통합 포트의 LLM 계약·실제 호출 검증·예산 | RES-05 |
| 전진 | 파일 replay 블록과 열화 flag | selector-bound 운용 버전·실시간 캠페인·중단/재개 | OPS-05,06 |
| 운영 | CLI watcher·JSON·DB event | 서비스·heartbeat·알림·backup·전체 migration | OPS-01~04 |
| 증권사 | 실전 읽기·모의 OMS 코드와 수기 fixture | 실제 읽기/모의 계약·새 연구 흐름 연결 | ORD-01,02 |

RES-03a는 공개 거래일 `calendar`와 관측 `sessions`를 분리하고, 개발 종료 후 최종 날짜가 달력에 있으면 후보·baseline·기준·개발 증거를 추가 전용 `agent_commitments`에 고정한다. 같은 트랜잭션에서 예산과 최종 구간을 예약한다. 최종 자료 도착 후 manifest를 저장하고 고정된 후보만 평가하며 개발 지문·최종 달력 변경을 거부한다. 내부 저장 시각이 첫 세션 09:00보다 이르면 E2, 그 외 E1을 evidence·result·status에 기록한다. 기존 commitment 없는 종료 사이클은 변경 없이 E1로 표시한다. 구현 완료 근거는 합성 회귀 범위이며 실데이터 전진 검증 완료를 뜻하지 않는다. 외부에서 확인 가능한 고정 시각(별도 저장소·서명)과 commitment만 있는 ordinal의 폐기는 미구현이다.

## 4. 미착수·후순위·보류

| 대상 | 상태 | 비고 |
| --- | --- | --- |
| 실제 데이터 수집 scheduler | 미착수 | DATA-06, 필드 적합성 확인 선행 |
| 학습 모델 fit/retrain·학습 pipeline | 미착수 | RES-06, 현재 자동 파라미터 선택과 구별 |
| 실패 기억을 사용하는 제안 의사결정 | 미착수 | 기록의 자동 해석·추천은 후속 |
| 검증된 backup·복원 리허설 | 미착수 | OPS-02, DB 과거 복원이 소비를 지우지 않아야 함 |
| 실제 새 데이터 전진 캠페인 | 미실시 | OPS-06, 파일 replay 성공을 대신 사용하지 않음 |
| 실제 모의 서버 운용 검증 | 미실시·후순위 | ORD-02, 현재 fixture와 분리 |
| 실전 주문 어댑터·계좌 위험 집행 | 미착수·후순위 | ORD-03,04, 읽기 키 설정과 별개 |
| 코드 자동 수정·자동 배포 | 현재 범위 밖 | 별도 격리·회귀·연구 버전 통제가 필요 |
| MarketProfile·전략군 레지스트리·예산 계정 | 부분 | LOOP-02a: `(market, family_lineage_id)` 예산 계정과 reserve/consume 추가 전용 원장, 이름 변경·실패·폐기 후 소비 유지, 기존 DB 역채움을 합성 회귀 범위에서 구현. 명시적 할당·독립 연구군·family_version·MarketProfile·전략군 레지스트리는 미구현. 실데이터 검증 미완료 |
| 평가 계약 ID·거래 가능성 지표 | 부분 | C1/C2: 수량 단계·reference_price 지표·turnover·평가 계약 ID와 개발/최종 동일 조건 검사 구현(합성 회귀). decision_cadence 후보 변수·capacity는 후속. LOOP-03 |
| `(market, family)` champion·관찰 원장·열화 상태 | 부분: LOOP-04a 레지스트리 합성 회귀 구현. LOOP-05a 연속 관찰 원장(조회는 미저장)과 LOOP-05b checkpoint 저장·사전 등록 열화 상태·봉인/기억 연동 합성 회귀 범위 구현. LOOP-09a kind 분리·cohort 봉인 표시·관찰 노출 기록 합성 회귀 구현. ObservationRelease 등록·중단(주문 권한 없음)은 LOOP-06a 합성 회귀 범위 구현. 강등 실행·ExecutionRelease·알림·다중 challenger cohort 미구현, 실데이터 검증 미완료 | LOOP-04, LOOP-05 |
| ObservationRelease·ExecutionRelease | 부분: LOOP-06a 관찰 등록·사람/열화/데이터 장애 중단·상태/CLI는 합성 회귀 범위 구현(주문 권한 없음). ExecutionRelease·알림·실데이터 검증 미구현 | LOOP-06, 주문 구현과 별개 |
| 통계 정책 v2 | 부분 | C4: opt-in 단측 v2·min_tail_draws=50·증거 metadata·오프라인 진단 구현(합성 회귀). 기본 v1의 CI 비트 결과 유지. 오류율 보장·실데이터 블록 선정·전략군 예산은 미완료. LOOP-07 |
| 실패 기억 개발 해제 | 부분 | LOOP-08a: 개발·최종·완결 shadow 실패 snapshot, 추가 전용 해제 기록, 해제된 기억 조회와 commitment의 `proposer_inputs`를 합성 회귀 범위에서 구현. LOOP-09a 관찰 cohort 봉인 표시와 제안기 비공개 합성 회귀 구현. LOOP-05b 관찰 checkpoint·봉인 해제 후 기억 연결을 합성 회귀 범위에서 구현. 기억을 쓰는 제안(RES-04/05)은 미구현. 실데이터 검증 미완료 |
| 구간 예약·동시 사용 정책 | 부분 | LOOP-09a: kind 분리·추가 전용 cohort·관찰 노출 기록·미고정 구간 앞 상한·봉인 표시를 합성 회귀 범위에서 구현. shadow는 최종 검증과 배타이나 관찰 노출과 겹침 허용. ObservationRelease 등록·중단은 LOOP-06a 합성 회귀 범위 구현. 다중 challenger cohort·ExecutionRelease·알림·실데이터 검증은 미완료 |
| 미국 H1~H4 개발·연구 실행 | **보류** | 사용자 명시 재개 전 진행하지 않음 |

## 5. 검증 증거

코드 통합 당시 기록(`74e07e9`):

| 증거 | 결과 | 무엇을 증명하지 않는가 |
| --- | --- | --- |
| root pytest | 210개 통과: 기존 190 + 통합 20, slow 1개 제외 | 실제 미국/국내 시장의 수익성, 실서버 운영 |
| 독립 KIS unittest | 122개 통과 | 실제 KIS 인증·체결·수수료·취소 계약 |
| mypy / ruff | root 소스 33개 타입 검사, lint 통과 | 외부 입력의 사실성·운영 준비 |
| 합성 종단 예제 | 첫 연구 승격, 다음 연구 유지, 별도 flat 유지, cache·20세션 shadow | 실제 데이터 우위·실계좌 연속 운용 |
| CLI 감시 재실행 | 완료 cycle 2개 cache 재사용, revision 1, shadow 20 | 상시 배포·수집기·서비스 복구 |
| 외부 호출·주문 | 위 합성 검증에서 0회 | 실제 서비스 준비 완료 |

이 문서 작성에서는 위 기록과 코드를 대조하고 문서 링크·설정·상태를 검증한다. 미국 연구 보류 뒤에 과거 미국 통과 기록을 새로 수행한 연구라고 표시하지 않는다.

로컬 합성 증거는 `artifacts/agent-demo-v1/verification.json`, `agent.json`, `watch-check/agent.json`에 있다. 생성 DB·입력·결과는 Git에서 제외되며 다른 checkout에 자동 포함되지 않는다. 재현 방법은 [실행 안내](../automatic-research-agent.md)에 있다.

## 6. 통합 회귀 20개의 범위

원본: [test_agent.py](../../tests/unit/test_agent.py). 아래는 구현 검증 범위이며 데이터·모델·실거래 테스트 개수와 합산해 시장 성숙도로 계산하지 않는다.

| 번호 | 검증 내용 |
| --- | --- |
| 01 | 통화 quantum과 독립 원장 대사 |
| 02 | 전일 수량 계획·시가 하락·결측 시가 |
| 03 | 승격·cache·새 shadow·다음 유지 |
| 04 | 개선 없음·데이터 대기·과거 변경 거부 |
| 05 | 연구 이름 변경·기존 KIS 소비 우회 차단 |
| 06 | 완성 평가 후 process fault의 증거 복구 |
| 07 | 최종 평가 중 실패의 소비 보존 |
| 08 | 짧은 표본의 신뢰 기준 실패는 유지 |
| 09 | manifest·gate 증거 변경 거부 |
| 10 | 배당 accrual/pay·정수 split·NAV |
| 11 | 미래 가격/상태 변경이 개발 trials에 영향 없음 |
| 12 | 다른 종목명으로 기존 KIS CLI를 사용해도 차단 |
| 13 | 과거 소비 import의 멱등성과 차단 |
| 14 | watcher 새 데이터 진행·재시작 cache |
| 15 | 같은 DB worker의 중복 최종 구간 등록 차단 |
| 16 | abandon 후 구간 보존·다음 일정 사용 |
| 17 | 정책으로 미국 최종 평가를 켤 수 없음 |
| 18 | 미지급 배당은 NAV에 포함, 현금으로 사용 불가 |
| 19 | 명시 달력에서 시장 전체 가격 누락을 숨기지 않음 |
| 20 | 첫 최종 계산 전에 manifest·소비 저장 확인 |

## 7. 변경 이력과 문서 기준

| 기준 | 내용 |
| --- | --- |
| `8f410a5` | 기존 미국 엔진 보존, KIS 앱 저장소 통합·경제 불변 회귀 |
| `7a439f0` | 미국 회계/KIS 연구 루프의 통합 설계 문서 |
| `74e07e9` | 자동 연구 v1·공통 평가·소비·watch·복구·20개 통합 회귀 |
| `577fede` | 미국 H1~H4 보류, KRX 연구 집중 지침 |
| 본 블루프린트 v1.0 | 최종 기준 문서·상세 작업/운영/학습·분리 현황. 런타임 변경 없음 |
| 본 블루프린트 v1.1 | 4단계 루프 중심 재구성, 루프 불변식 INV-1~8, 증거 등급 E1~E3, 루프 계약 설계안, LOOP-01~09. PR #2 리뷰 두 차례 반영(2차: 검증 cohort와 구간 동시 사용, snapshot 단위 해제, reference_price 가중)(E2 판정·평가 계약 범위·실패 기억 해제·두 단계 Release·cap 이전 지표·명목 예산·예산 계정). 런타임 변경 없음 |

다음 갱신에는 작업 ID, 변경 commit, 코드 완료 여부, synthetic/실데이터/전진/외부 서비스 각각의 증거, 남은 제약을 함께 추가한다.

## 8. C1~C4 구현 증거

[1차 구현 보고](batch1-implementation.md)에 작업별 변경·테스트·검증 명령·남은 범위를 기록한다. 새 테스트는 기존 `test_agent.py` 20개와 분리한 4개 파일에 추가했다. 기존 테스트/임계값/미국 코드/골든/KIS 앱은 수정하지 않았다. 최종 `pytest`: 245 passed, 1 deselected(기존 210 + 신규 35); KIS unittest 122개 OK; mypy src 33개, ruff 통과. 통계 스크립트를 포함한 mypy도 34개 통과했다. 이 결과는 합성·오프라인 회귀이며 미국 가설 연구 재개나 실데이터 검증을 뜻하지 않는다.

## 9. 1차 묶음 리뷰 보완

- NAV 정규화 전에 독립 합계와의 원시 Decimal 절대 잔차를 검사한다. 허용 한도는 `1e-9`원이며 초과/비유한 값은 실패한다. 원 단위 정규화 후 정확 대사도 유지한다. 일별 `nav_residual`, 요약 최대 절대 잔차와 계약의 한도를 남긴다. 공통 원장 소스는 수정하지 않았다.
- 기준 전략 `portfolio_diagnostics`: 세션 종가 기준 평균 투자/현금/배당채권 비중, 평균·마지막·최대 보유 수, 후보-세션별 예산 부족 제외 비율. 시장/품질 필터 탈락은 분모에서 제외하고, 최대 종목 예산 또는 동일 비중 예산에서 1주도 못 사는 종목은 한 번만 센다. 분모가 없거나 현금 전략에는 비율 N/A다.
- 최종(유지 포함)·shadow 요약에도 진단을 전달하며 승격 gate에는 사용하지 않는다. 동일 비중 전략의 종목 수/배분 규칙은 유지했다. 시장 ETF·종목 수 상한 연구는 후속이다.
- 새 `test_agent_review_followups.py`에 원 미만 오류 주입, 정상 반복 매수/매도, 100종목 현금화, 시장 탈락/예산 제외 구별, 중복 집계 방지, 요약 전달 회귀를 추가했다. 검증 결과는 [구현 보고의 리뷰 보완](batch1-implementation.md#리뷰-보완)을 따른다.

리뷰 보완 검증: pytest **257 passed, 1 deselected**(신규 12), KIS unittest **122 OK**, mypy src **33개 통과**, ruff 통과. 합성/오프라인 회귀 범위이며 실데이터 검증은 후속이다.

## 10. RES-01 시장 ETF 기준 전략 보고

`AgentPolicy.market_etf_symbol`(기본 null)로 사전 지정한 ETF만 평가하는 opt-in `market_etf` 보고를 추가했다. 기본 현금/동일 비중 보고와 승격 gate·일정·소비 규칙은 유지한다. 종목을 바꾸면 새 정책/기준 전략 버전이고 기존 연구에 덮어쓸 수 없다. 최종(유지 포함)·shadow에 같은 계약으로 연결했다. 평가 계약은 v2로 올려 기준 전략 정의 목록을 hash에 고정했다.

관측 시점 ETF 유형·가격/이력 커버리지가 없으면 `UNAVAILABLE`와 사유만 보고하며 수익률 0으로 만들지 않는다. ETF로 확인되어도 시장/품질 필터에서 탈락하면 현금 유지·해당 세션 수를 보고한다. 기존 단일 종목/주문/총노출 한도를 그대로 적용하는 일별 리밸런싱 기준 전략이므로, 기본 최대 종목 비중 40%에서 완전 투자 지수 추종 성과로 해석하지 않는다. 실제 ETF별 비용 모델과 데이터 확보는 여전히 FIN/DATA 후속이다. 사용법·검증은 [ETF 구현 보고](market-etf-benchmark.md)에 있다.

ETF 보고 검증: pytest **271 passed, 1 deselected**(신규 14), KIS **122 OK**, mypy **33개** 및 ruff 통과. 합성/오프라인 범위다.

## 11. LOOP-02a 예산 계정 원장

`agent_budget_accounts`·`agent_budget_entries`로 시장·전략군 예산을 공유한다. [신규 회귀](../../tests/unit/test_agent_budget.py) 12개는 이름 변경·완료/cache/recover·예산 소진 시 등록 롤백·정책 예산 불일치 거부·실패/폐기 소비 유지·claim 실패 시 예약 롤백·5개 상태의 기존 DB 멱등 역채움·첫 등록의 할당 확정·EVALUATED 복구를 고정한다. 기존 회귀·통계·미국 코드·KIS 앱은 변경하지 않았다. 명시적 할당·독립 연구군·family_version과 실데이터 검증은 후속이다.

LOOP-02a 최종 검증(합성·오프라인): `uv run pytest -q` **269 passed, 1 deselected**(신규 12); `uv run mypy src` **33개 소스 통과**; `uv run ruff check` **All checks passed**; KIS unittest **122개 OK**. `UV_CACHE_DIR=/tmp/momentum-uv-cache`, `UV_OFFLINE=1`로 실행했다. 실데이터 검증이나 미국 가설 연구 재개를 뜻하지 않는다.


## 12. LOOP-05b 관찰 checkpoint·사전 등록 열화 상태

[신규 합성 회귀](../../tests/unit/test_agent_degradation.py) 22개는 정책 기본값·기존 JSON·거부 조건, 완결 창 경계·멱등·조회 무쓰기, 일별 NAV 창 계산, 임계·지속 열화·revision 초기화·강등 대상, 가격 수정 불변성, cohort 봉인·기억 해제, 강등 미실행과 노출 기록 실패 격리를 확인한다. `_shadow`와 TRACKING_DEGRADED는 변경하지 않았다. 강등 실행·알림·ObservationRelease(LOOP-06)와 실데이터 검증은 미구현이다.

주관자 결정에 따라 checkpoint 보고는 최상위 `tick.observation_checkpoints`로 분리했다. `observation`은 LOOP-09a 및 직접 조회와의 동등성 계약을 유지하고, 관찰이 None/unavailable이면 checkpoint 보고도 None이다. 기존 테스트는 수정하지 않았다. 신규 회귀는 이 보고 위치와 관찰 부재·실패 시 무저장을 포함한다. `UV_CACHE_DIR=/tmp/momentum-uv-cache`, `UV_OFFLINE=1`에서 최종 전체 pytest **367 passed, 1 deselected**(신규 합성 회귀 22개 포함), mypy **33개 소스 통과**, ruff **All checks passed!**, KIS unittest **122개 OK**다. 기존 관찰·cohort 동등성 회귀와 `_shadow`·TRACKING_DEGRADED 회귀도 그대로 통과한다. 실데이터 검증 완료를 뜻하지 않는다.

## LOOP-06a ObservationRelease (합성 회귀 범위)

추가 전용 관찰 등록·STOPPED 이벤트, E2/E3 현재 champion 승인, 근거 사이클 정책/최종 후보 평가 계약 동결, 멱등 등록·사람 중단·같은 revision 열화/데이터 오류 자동 중단, status·tick 이벤트·CLI를 구현했다. 봉인 중에도 열화 안전 중단은 가능하며 이벤트에는 checkpoint 순번·창만 남긴다. 주문 권한은 항상 0이며 주문 모듈 연결은 없다. ExecutionRelease·알림 전송·E3 실시간 관찰 검증·실데이터 검증은 미구현이다. 회귀 근거는 `tests/unit/test_agent_release.py`다.

검증: 오프라인 UV 캐시 환경에서 전체 pytest `389 passed, 1 deselected`; 이후 오류 종류 보존 보완을 포함한 최종 열화·release 회귀 `35 passed`(release 13개), mypy `Success: no issues found in 33 source files`, ruff `All checks passed!`, KIS unittest `Ran 122 tests / OK`. 기존 테스트·임계값·골든 픽스처는 수정하지 않았다. 실데이터/운영 검증 근거가 아니다.

## FIN-01a 과거 호가 없음 모드 (합성 회귀 범위)

`historical_unavailable`은 결측 호가를 만들지 않고 해당 호가 필터만 생략하며 다른 선정 필터는 유지한다. 기본 `quoted`는 기존 선정 사유·특성을 유지한다. `AgentPolicy`는 편도 slippage 25bp 이상을 요구하고 선정 digest가 평가 계약 ID에 반영된다. 기존 status·tick의 `policy.logic.selection.spread_mode`로 정책을 확인하므로 별도 보고 필드와 portfolio_diagnostics 필드는 추가하지 않았다. 관찰 release는 근거 사이클 정책의 `spread_mode`와 `execution_eligible`을 동결하며 quoted만 실행 자격 true다. 주문 권한은 항상 false다. 주문 경로가 SelectionPolicy를 받지 않으므로 주관자 결정에 따라 주문 거부 검사는 추가하지 않았다. 후속 ExecutionRelease는 실행 자격 false를 거부해야 한다.

신규 회귀는 `apps/kis-agent/tests/test_spread_mode.py`와 `tests/unit/test_agent_spread_mode.py`다. 기본/기존 JSON·결측/실제/넓은 호가·잘못된 모드·비용 하한·계약 ID·현금 유지와 4,000원→4,010원 손계산 체결·두 모드의 E2 승격 후 release 실행 자격을 고정한다. 실데이터 비용 검증·전진 호가 수집·ExecutionRelease는 후속이다.

FIN-01a 최종 검증(`UV_CACHE_DIR=/tmp/momentum-uv-cache`, `UV_OFFLINE=1`): root pytest **409 passed, 1 deselected**(신규 5개), mypy **Success: no issues found in 33 source files**, ruff **All checks passed!**, KIS unittest **Ran 126 tests / OK**(신규 4개). 구현 전 신규 회귀의 미지원 모드·release 필드 실패를 확인했다. 합성·오프라인 회귀 근거이며 실데이터 검증 완료를 뜻하지 않는다.
