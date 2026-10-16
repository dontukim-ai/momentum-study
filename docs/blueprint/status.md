# 현재 진행 현황 — 구현과 검증을 분리한 기준표

작성일: 2026-10-16 KST. 구현 기준 `74e07e9`, 미국 연구 보류 기준 `577fede`. 본 표는 [블루프린트](README.md)의 현황판이다. 이번 문서 변경에서 새 런타임 기능을 구현하거나 미국 연구를 실행하지 않는다.

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
| S1 후보 생성 | 부분 | KRX 필터·3가지 ranking·고정 grid 제안 | 시장 추상화·전략군 레지스트리·결정 주기 파라미터·가설형 제안·실패 기억 해제 기록 없음 | LOOP-01, LOOP-02, LOOP-08, RES-04, RES-05 |
| S2 동일 조건 평가 | 부분 | 공통 평가기·원장·비용 스트레스 | 평가 계약 ID·거래 가능성 지표(cap 이전 분모)·현금/단순 기준 전략 없음, gate별 지표 역할 미정 | LOOP-03, RES-01, FIN-01~04 |
| S3 독립 검증 | 부분 | 예약 구간·시장 단위 소비·승격 조건 10개 | 증거 등급 E1만 가능(선택 후보를 첫 실행 시각 전에 고정하지 않음), 예산이 연구 단위, 통계 정책 정리 필요 | RES-02, RES-03, LOOP-07 |
| S4 갱신·추적 | 부분 | 연구 단위 champion revision·유지·20세션 shadow | `(market, family)` 키·연속 관찰 원장·열화 상태 전이·두 단계 Release·관찰과 최종 검증의 동시 사용 정책 없음 | LOOP-04~06, LOOP-09, OPS-05, OPS-06 |

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
| 비용·호가 | 예제 bps·원 단위 | 주식/ETF·유효 날짜·계좌·호가·정산의 실제 계약 | FIN-01 |
| 기업행동 | raw ledger split/dividend | 신호 보정·정정·권리·합병·일부 미지원 행동 | FIN-02 |
| 경제 평가 | NAV·현금·채권·일봉 체결 | 실제 결제·기말 청산·장중/부분 체결·capacity | FIN-03,04 |
| 비교군 | 기존 champion | 현금·단순 기준 전략·절대 순성과 정책 | RES-01 |
| 독립 검증 | 코드의 prefix·동결·소비 gate | 미래 자료 도착 전 commitment·OS 격리·외부 노출 증거 | RES-02,03 |
| 통계 | 5일 block·고정 cycle 예산 | 블록/국면 민감도·전략군 전체 예산·학습 라벨 격리 | RES-02,03 |
| 기억 | 실험·사유·gate·shadow 저장 | 실패 분류·중복 가설·개발 전용 기억 검색 | RES-04 |
| 제안 | grid, 별도 앱의 strict JSON 코드 | 통합 포트의 LLM 계약·실제 호출 검증·예산 | RES-05 |
| 전진 | 파일 replay 블록과 열화 flag | selector-bound 운용 버전·실시간 캠페인·중단/재개 | OPS-05,06 |
| 운영 | CLI watcher·JSON·DB event | 서비스·heartbeat·알림·backup·전체 migration | OPS-01~04 |
| 증권사 | 실전 읽기·모의 OMS 코드와 수기 fixture | 실제 읽기/모의 계약·새 연구 흐름 연결 | ORD-01,02 |

cycle manifest는 현재 **최종 데이터가 충분히 도착한 뒤, 계산 전에** 저장된다. 초기 소스·정책은 연구 생성 시 고정되지만, 미래 구간 도착 전에 외부에 증명 가능한 일정 commitment까지 저장한 시스템은 아니다. 독립 검증을 코드상의 분할·소비 보호 이상으로 주장하지 않는다.

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
| MarketProfile·전략군 레지스트리·예산 계정 | 미착수 | LOOP-01, LOOP-02. 현재 KRX 규칙과 결정 주기가 코드에 고정 |
| 평가 계약 ID·거래 가능성 지표 | 미착수 | LOOP-03 |
| `(market, family)` champion·관찰 원장·열화 상태 | 미착수 | LOOP-04, LOOP-05 |
| ObservationRelease·ExecutionRelease | 미착수 | LOOP-06, 주문 구현과 별개 |
| 통계 정책 v2 | 미착수 | LOOP-07. 현재 승격의 명목 단측 예산은 0.025(실제 오류율 검증 없음), 하한은 재표집 분포의 0.3125% 분위수(2,000회 중 약 6번째 값) |
| 실패 기억 개발 해제 | 미착수 | LOOP-08. 최종·관찰 결과를 제안기에 넘기는 권한 기록이 없음. 현재 shadow 보고는 COMPLETE 뒤에도 같은 행에 덮어써져 확정 snapshot이 없음 |
| 구간 예약·동시 사용 정책 | 미착수 | LOOP-09. 현재 `_claim`은 용도와 상관없이 겹침을 모두 거부해, 연속 관찰과 다음 최종 검증을 함께 할 수 없음 |
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
