**v0.5 — 종목 선정·전략 판단·자동 개선 선행 개발**

자동 주문보다 로직과 자동 개선을 먼저 완성하라는 요청에 따라, **입력 데이터 → 시점 기반 종목 필터 → 점수·목표 비중 → 제한된 개선 후보 → 독립 평가 → 연구용 기준 전략 갱신 → 새 데이터의 다음 사이클**을 실행 가능한 경로로 연결했다. 이 경로는 계좌를 만들거나 브로커·OMS·shadow 포인터를 호출하지 않는다.

완료 기준은 소프트웨어 동작과 재현성이다. 실제 전 종목 데이터·과거 종목 상태·수수료 계약·실제 시장 수익성은 검증되지 않았다. 현재 구현한 자동 개선은 lookback과 허용된 순위 규칙의 제한된 탐색이며 코드·모델 가중치의 자기 수정이 아니다.

| 영역 | 완료한 동작 |
| --- | --- |
| 시점 데이터 | 가격 available_at과 종목 상태 effective_at/available_at을 함께 검사. 나중에 발표된 거래정지·과거 사건의 수정본을 소급하지 않음 |
| 동적 목록 | 신규 상장 등 가격 이력이 짧은 종목과 관측된 종목의 결측을 표현. 현재 존재하는 종목 목록을 과거에도 그대로 적용하지 않음 |
| 종목 필터 | 거래정지·상장폐지·관리·주의·레버리지·역방향·범위 밖 상품 제외. 상태 누락·노후·호가 누락·넓은 spread·낮은 유동성·높은 변동성·짧은 이력·최근 결측·예산 부족을 기록 |
| 판단 | 모멘텀, 변동성으로 나눈 모멘텀, 상승 관측 비율로 조정한 모멘텀의 세 규칙. 상위 N개와 선정/탈락 사유·특징·순위를 출력 |
| 예산 | 최대 100만 원, 예시 총 노출 80%, 종목당 40%, 한 주문 40만 원. 수수료와 슬리피지를 포함해 1주를 살 수 없는 종목 제외. 빈 자리는 현금 유지 |
| 평가 | 61개 이상의 고정 준비 이력, 시간순 개발 3개 구간, 최종 구간 1회 소비, 비용 스트레스·최소 활동·낙폭·개선·신뢰구간 검사 |
| 자동 개선 | 통과하면 연구용 champion만 자동 갱신. 실패·동률·증거 부족은 기존 버전 유지. 후보·실패·판정·버전과 근거를 SQLite에 보존 |
| 운영 | 같은 입력의 재실행은 저장 결과 재사용. 연구별 한 실행자. 원본 과거 데이터 변경·평가 구간 재사용·기준 변경·빌드 혼용 거절. 완료된 평가만 복구 |

주요 코드는 selection.py, logic.py, logic_cli.py다. 공통 평가기는 evaluation.py/research.py를 재사용한다. 기존 모멘텀 경로의 기본 동작을 유지하고, 새로운 평가 경로에는 종목 상태·고정 위험·자본 한도를 명시적으로 연결한다. 스키마 v4는 logic_studies와 logic_cycles를 추가하며 기존 평가·주문·체결·소비 기록을 삭제하지 않는다.

**키 없이 전체 경로 실행하기.** 프로젝트 루트에서 실행한다.

```bash
uv sync --offline
uv run python -m unittest discover -s tests -v
uv run python -m examples.offline_logic --output artifacts/logic-v5
```

119개 테스트로 기존 연동과 새로운 선정·개선·복구 경계를 검사했다. 통합 예제는 합성 가격과 합성 종목 상태만 사용하며 외부 요청과 주문은 0회다. 일반 합성 연구는 9개 전략을 비교하고 KEEP_CHAMPION을 유지한다. 별도의 반복 주기 fixture는 성공 분기를 시험하도록 만든 것으로, 연구 champion 갱신과 520개 세션으로 이어지는 다음 사이클을 검증한다. 이 통과 결과를 일반 연구의 판정이나 실제 시장 증거로 바꾸지 않는다.

| 산출물 | 내용 |
| --- | --- |
| prices.csv / securities.jsonl | 합성 입력과 명시된 관측 시각·출처 |
| research.sqlite3 | 모든 시도·실패·최종 구간 소비·연구 champion·사이클 이력 |
| development-context.json | 최종 평가 구간을 제외한 제안용 문맥 |
| research.json | 후보별 개발 결과, 선택된 후보, 최종 구간 판정·제한 |
| selection.json | 현재 연구 champion의 종목별 특징·선정 및 탈락 사유·목표 비중·현금 비중 |
| run.json / study.json | 사이클 결과와 현재 연구 상태 |
| engineered-promotion.json / engineered-forward.json | 성공 경로 전용 fixture의 갱신과 후속 사이클 |
| verification.json | 구현 해시, 외부 호출·주문 0회, 경계 검증 결과 |

**데이터 계약.** 가격은 기존 CSV 컬럼 session,symbol,open,high,low,close,volume,available_at,source를 사용한다. logic 경로만 불완전한 종목×일자 격자를 허용한다. 신규 종목은 관측 시작 전 가짜 가격을 채우지 않는다. 충분한 최근 이력이 없거나 관측된 시장 세션 중 가격이 빠졌으면 제외한다. 보유 종목의 평가 가격이 없으면 현금이나 0으로 대체하지 않고 해당 평가를 실패 처리한다.

종목 상태는 한 줄에 한 JSON 객체인 JSONL이다. 예시는 **형식 설명용 가상 종목**이다.

```json
{"symbol":"EXAMPLE","effective_at":"2026-10-16T15:30:00+09:00","available_at":"2026-10-16T15:40:00+09:00","status":"NORMAL","asset_type":"STOCK","market":"KOSPI","managed":false,"caution":false,"leveraged":false,"inverse":false,"bid":"10000","ask":"10010","source":"YOUR_VERIFIED_ARCHIVE"}
```

effective_at은 상태·호가가 유효한 시각, available_at은 이를 실제로 알 수 있었던 시각이다. 둘 다 판단 시각 이전인 버전만 사용한다. 동일 사건의 나중 수정본은 발표 전 시점에 보이지 않는다. 오래된 사건의 수정본이 더 최근에 유효해진 상태를 덮어쓰지도 않는다. 생략된 위험 플래그를 정상으로 추정하지 않으며, bid/ask가 없으면 spread 검증 불가로 제외한다.

현재 선정 예시는 STOCK/ETF, KOSPI/KOSDAQ, 최소 60개 관측, 최근 20개 관측의 거래량 1만 주·거래대금 대용값 1억 원 이상, 일별 변동성 8% 이하, spread 30bps 이하, 상태 최대 24시간을 사용한다. 모두 **연구용 예시 기준**으로 configs/logic.json에서 초기화 전에 명시할 수 있다. 거래대금은 실제 체결 대금이 아니라 close×volume의 평균이다. 최근 세션 연속성은 입력 데이터에서 관측된 시장 날짜를 사용하며 KRX 공식 휴장 캘린더를 대신하지 않는다.

현재 KIS 일봉·실전 읽기 어댑터와 별개로, 이 모듈은 정규화된 가격·종목 상태 기록을 입력받는다. 전 시장의 과거 구성·상장폐지·기업행위·호가 아카이브를 자동 수집·확정하는 공급자는 아직 연결하지 않았다. 오늘 상태를 과거 정상 상태로 복사해서는 안 된다. 실제 데이터 공급자를 연결할 때도 이 계약을 만족해야 한다.

**연구 설정과 실행.** 아래는 예제에서 만든 DB와 입력을 재조회하는 명령이다.

```bash
uv run kis-agent logic status --db artifacts/logic-v5/research.sqlite3 --study main
uv run kis-agent logic run --db artifacts/logic-v5/research.sqlite3 --study main --csv artifacts/logic-v5/prices.csv --securities artifacts/logic-v5/securities.jsonl --output artifacts/logic-v5
```

새 실제 연구는 별도의 최초 DB에 한 번 초기화한 뒤 같은 DB를 계속 사용한다.

```bash
uv run kis-agent logic init --db "$RESEARCH_DB" --study main --config configs/logic.json --output artifacts/logic
uv run kis-agent logic run --db "$RESEARCH_DB" --study main --csv "$PRICE_CSV" --securities "$SECURITY_HISTORY" --output artifacts/logic
uv run kis-agent logic screen --db "$RESEARCH_DB" --study main --csv "$PRICE_CSV" --securities "$SECURITY_HISTORY" --session "$KST_SESSION" --output artifacts/logic
```

run은 전체 입력의 역사적 연구 시계로 평가하고 마지막 입력 세션 16시 기준의 연구 선정 보고서를 만든다. 실제 현재 시각의 신호를 뜻하지 않는다. screen은 기본 현재 UTC를 사용하므로 옛 데이터나 상태는 오래된 것으로 제외한다. `--as-of`는 과거 시점 재현용이며 실제 전진 검증으로 표현하지 않는다. 설정은 init 시 고정되고 run에 다른 --config를 줄 수 없다.

기본 탐색은 lookback 5/10/20과 세 순위 규칙을 조합하며 challenger 최대 8개와 champion 1개를 비교한다. champion이 기본 격자 밖으로 바뀌면 사전 지정한 순서에서 최대 8개만 선택한다. 허용된 lookback 전체 범위는 정수 2~60이다. 순위 규칙 변경으로 비중·필터·비용·예산·평가 기준을 변경할 수 없다. 목표 비중은 등액의 고정 슬롯이며, 높은 점수를 이유로 한 종목에 빈 슬롯을 몰아주지 않는다. 자산이 100만 원을 넘어도 새 노출 산정에 쓰는 자본은 100만 원으로 제한한다. 가격 상승에 따른 보유 평가액 증가까지 100만 원 이하로 보장한다는 뜻은 아니다.

**독립 평가와 유지 조건.** 모든 후보는 같은 준비 기간 이후 개발 3구간에서 비교한다. 각 구간 최소 2번의 체결과 낙폭 기준을 통과한 후보만 개발 평균 일수익률로 선택한다. 그중 champion보다 나은 후보 하나만 최종 구간을 열 수 있다. 최종 구간은 전체의 20%, 최소 30개 세션이며 평가를 시작하기 전에 소비 기록을 저장한다. 후보의 순수익률 개선 0.1%p 이상, 낙폭 20% 이하, 최소 4번의 체결, 짝지은 블록 bootstrap 평균 차이 구간의 하단 >0, 수수료·슬리피지 2배에서도 기준 전략 대비 개선을 함께 요구한다. 기준은 예시 연구 정책이며 통계적 수익 보장이 아니다.

새 경로의 기본 비용은 매매 수수료 1.5bps, 매도세 20bps, 슬리피지 5bps인 **시나리오 수치**다. 20bps는 모든 상품에 일괄 적용하는 연구 시나리오이며 2026년의 실제 세율이나 ETF 세율을 확인한 값이 아니다. 상품·시기·계좌별 실제 비용표가 필요하다. 스트레스는 수수료와 슬리피지를 두 배로 하고 이 매도세 시나리오는 유지한다.

체결은 이전 확정 종가로 신호를 내고 다음 관측 세션 시가에 비용을 더하는 선별용 근사다. 당시 알려진 상태에서 거래정지·상장폐지·상태 누락·노후인 종목은 가상 체결도 만들지 않는다. 거래정지 종목을 선정에서 제외했다는 이유만으로 다음 시가에 팔아 현금화하지 않는다. 일봉 체결 대기열·VI·당일 나중에 발생한 정지·기업행위·분배금·실제 상장폐지 정산은 지원하지 않는다.

평가 통과는 연구 champion의 자동 갱신으로만 연결된다. 기존 shadow release나 실전 전략에는 연결하지 않는다. 선정 정책에 결합된 연구를 옛 momentum-only shadow release로 등록하면 거절하므로 필터·순위 규칙이 빠진 채 활성화될 수 없다.

**선택적 LLM 제안.** 키 없이 고정 후보 생성기만으로 전체 경로가 작동한다. LLM을 선택하면 기존 OpenAI Responses 구조화 제안과 요청 예산·캐시를 재사용한다.

```bash
uv run kis-agent logic run --db "$RESEARCH_DB" --study main --csv "$PRICE_CSV" --securities "$SECURITY_HISTORY" --llm --model "$OPENAI_MODEL" --output artifacts/logic
```

OPENAI_API_KEY는 비밀 설정에서 읽는다. 제안 입력은 개발 종료 시점에 실제로 보였던 특징과 최대 20개 종목의 요약뿐이며 최종 구간 가격·상태를 제외한다. 모델은 가설·반증 조건·lookback 정수 목록만 반환한다. 순위 규칙은 사전 허용 목록과 조합하며 기본 3개 규칙에서는 최대 2개 lookback을 받아 예산을 지킨다. 코드·실행·위험·비용·합격 기준 필드는 거절한다. 원래 momentum-only 문맥으로 만든 제안 파일은 이 경로와 맞지 않아 거절된다.

실패를 임의 후보로 대체하거나 모델 요청을 자동 재시도하지 않는다. 같은 데이터의 완료된 사이클에 새 제안을 주어 다시 평가할 수 없다. 완료 결과 재조회는 --llm/--proposal-file 없이 run 또는 status를 사용한다. 실제 모델 호출은 이 개발 환경에서 미검증이다.

**재시작과 다음 사이클.** 새 사이클은 이후 세션을 추가해야 하며 이전 가격·당시 알려진 종목 상태를 그대로 보존해야 한다. 다음 최종 구간이 과거 소비 구간과 겹치면 거절한다. 예를 들어 400개 관측에서 마지막 80개를 소비했다면 20개만 추가한 평가의 최종 구간은 겹친다. 고정 분할 정책에 따라 충분한 새 관측을 확보해야 한다. 자동 개선을 계속하려고 DB를 바꾸거나 합격 기준을 낮추지 않는다.

```bash
uv run kis-agent logic recover --db "$RESEARCH_DB" --study main --cycle-id "$CYCLE_ID" --output artifacts/logic
uv run kis-agent logic abandon --db "$RESEARCH_DB" --study main --cycle-id "$CYCLE_ID" --reason "$STOPPED_WORKER_REASON" --output artifacts/logic
```

평가 완료 후 champion 갱신 전에 중단됐다면 recover는 저장된 평가·정책·버전·소비 기록·수치 기준을 다시 확인하고 한 번만 반영한다. 평가를 재실행하거나 LLM을 다시 호출하지 않는다. 평가 도중 중단되어 완료 결과가 없으면 recover로 구간을 다시 열 수 없다. 해당 작업자가 종료됐음을 확인한 뒤 사유를 남겨 abandon하면 실행 잠금만 종료한다. 소비 구간과 원본 데이터는 보존하고 충분한 새 데이터로 다음 사이클을 진행한다. 완료 평가가 있으면 abandon으로 버리지 못하고 recover를 사용해야 한다.

연구 DB에 쓰기 권한이 있는 코드를 막는 OS 보안 경계는 아직 없다. 현재 평가는 별도 함수·계약·입력 해시·영속 상태로 분리한 논리적 경계이며 프로세스·계정 권한 분리는 추후 운영 작업이다. 현 단계의 선행 개발 완료 범위는 이 주문 없는 선정·평가·개선 엔진이다.
