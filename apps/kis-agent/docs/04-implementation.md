**로컬 MVP 구현 현황 — 2026-10-16**

이 문서는 v0.1 구현 기록이다. 이후 추가된 LLM·KIS 모의 어댑터와 현재 상태는 [v0.2 연동 문서](05-integrations.md)를 기준으로 확인한다.

첫 수직 경로를 로컬에서 실행할 수 있게 구현했다. 이 문서는 [전체 설계](02-design.md) 중 실제 구현된 범위와 이후 통합할 범위를 구분한다. 한투 자격증명이 구성되지 않아 네트워크 인증·조회·모의 주문은 실행하지 않았다.

| 영역 | 현재 구현 | 다음 통합 경계 |
| --- | --- | --- |
| 데이터 | CSV 검증, SHA-256 스냅샷, 시점 필터, 확정 일봉·중복·격자 검사 | 실제 시장 캘린더, 과거 유니버스, 기업행위·분배금 |
| 전략 | 순수 모멘텀 함수, 양의 모멘텀 종목 선택, 목표 비중·정수 수량 | 추가 전략·모델, 공식 .kis.yaml 변환 |
| 평가 | 전일 확정 종가 신호→익일 시가+슬리피지, 비용·거래 로그·낙폭·회전율 | Lean 어댑터, 실제 실행창 재생·미체결 모델 |
| 연구 | 허용된 lookback 제안, 시간순 개발 3개 구간, 최종 구간 소비 기록, 비용 스트레스 | 외부 LLM 후보 생성기, OS/서비스 수준 평가 격리 |
| 주문 | SQLite 예약·상태·이벤트·체결 원장, 단일 주문의 경쟁 전송 차단 | PostgreSQL, 분산 lease/fencing, KIS 모의 REST/WS |
| 리스크 | 허용 종목·최신 호가·만료·현금·매도 가능 수량·미체결 포함 노출 검사 | 일일 손실·드로다운·시장 상태·거래소 호가 단위 정책 |
| 복구 | UNKNOWN/SUBMITTING 대사, 중복 체결 방지, 취소 중 추가 체결, 잔고 차이 정지 | 실제 브로커 상태·외부 입출금/기업행위와의 대사 |
| KIS | 고정 호스트·인증·읽기 전용 원주가 일봉 수집기, 모의 응답 테스트 | 자격증명·네트워크 허용 후 실제 응답 검증 |
| 스킬 | 프로젝트 전용 6개 SKILL.md 초안 | 클라이언트별 등록·런타임 권한 분리 |

초기 데이터·연구·운영 영속성은 표준 라이브러리와 SQLite로 구현했다. PostgreSQL·Parquet/DuckDB·Lean은 아직 설치·통합하지 않았다. 같은 프로세스 안의 모듈 경계가 운영 보안 격리를 대신한다고 주장하지 않는다.

**실행 방법.** 프로젝트 루트는 /workspace/kis-self-improving-agent다. 아래 명령은 설치 없이 표준 Python으로 실행된다.

```bash
PYTHONPATH=src python -m trading_agent demo --config configs/paper.toml --output artifacts/demo
PYTHONPATH=src python -m trading_agent backtest --csv artifacts/demo/synthetic.csv --output artifacts/backtest
PYTHONPATH=src python -m trading_agent research --csv artifacts/demo/synthetic.csv --db artifacts/demo/agent.sqlite3 --output artifacts/research
PYTHONPATH=src python -m trading_agent paper --csv artifacts/demo/synthetic.csv --db artifacts/paper.sqlite3 --account paper-local --output artifacts/paper
PYTHONPATH=src python -m trading_agent status --db artifacts/paper.sqlite3 --account paper-local
PYTHONPATH=src python -m trading_agent reconcile --db artifacts/paper.sqlite3 --account paper-local
```

uv sync 후에는 각 명령의 PYTHONPATH=src python -m trading_agent를 uv run kis-agent로 바꿀 수 있다. uv 캐시는 프로젝트의 .cache/uv를 사용한다. 패키지 빌드 시 setuptools가 필요하지만 런타임 의존성은 없다.

데모 산출물은 다음과 같다.

- synthetic.csv: 실제 종목이 아닌 DEMO_A/B/C의 재현 가능한 합성 가격. 평일만 만들며 실제 한국 거래소 휴장일을 재현하지 않는다.
- backtest.json: 버전·스냅샷·평가 ID, 일별 평가액, 거래, 비용·성과·체결 가정.
- research.json: 개발 구간별 후보 성과, 총 시도 수, 선택·유지 판정, 최종 구간 소비 여부·비용 스트레스·불확실성.
- paper.json: 로컬 모의 계좌의 주문·체결·현금·수량·대사·위험 설정.
- agent.sqlite3: 로컬/모의 브로커의 독립 원장, 주문·이벤트·체결·실험·평가 구간 소비 기록.
- report.md: 합성 데모임을 표시한 읽기 쉬운 요약.

평가와 로컬 모의 실행은 같은 신호·정수 사이징·비용 함수를 사용한다. 최종 평가액과 체결 건수의 일치를 테스트한다. 로컬 예시 한도는 총 노출 80%, 종목별 40%, 주문당 5,000,000이며 실제 계좌 정책으로 승인된 값은 아니다.

**CSV 계약.** 필수 열은 session,symbol,open,high,low,close,volume,available_at이다. source는 선택 열이다. 날짜는 ISO 날짜, 가격은 양수, 거래량은 0 이상 정수, 시각은 시간대가 있는 ISO 형식이다.

```csv
session,symbol,open,high,low,close,volume,available_at,source
2024-01-02,DEMO_A,10000,10100,9900,10050,100000,2024-01-02T06:40:00+00:00,SYNTHETIC
```

available_at은 KRX 정규장 종료 시각인 15:30 KST 이후여야 한다. 전략은 전 거래일 16:00 KST 시점에 볼 수 있는 최종 일봉만 사용하고 다음 세션 09:00 KST에 실행한 것으로 모사한다. 늦게 공표된 값은 임의로 앞당기지 않고 평가를 거절한다. 이는 현재 선별 엔진의 고정 시각 가정이며 실제 거래 세션 확정은 후속 통합이 필요하다.

현재는 원주가 기준의 기업행위 없는 데이터가 필요하다. adjusted_close가 close와 다르면 명시적으로 거절한다. 분할·분배금이 있는 실제 데이터는 별도 원장·조정 모델을 구현하기 전까지 신뢰할 수 있는 수익성 평가에 사용하지 않는다.

**자기 개선의 현재 범위.** BoundedLookbacks는 기존 전략의 배분·비용·판정 기준을 유지하면서 2~60 거래일 정수 lookback만 제안한다. 기본 후보는 5,10,20, 예산은 8개이며 전부 기록한다. 향후 LLM 어댑터는 Proposer 인터페이스로 같은 StrategySpec을 반환하게 할 수 있다. 임의 코드 실행은 현재 지원하지 않는다.

훈련이 필요 없는 룰 전략이므로 개발 구간은 고정 후보의 시간순 성과를 비교한다. 마지막 구간은 후보를 선택한 뒤에만 열고, 개발에서 개선 후보가 없으면 열지 않는다. 최종 구간을 사용하면 종목·기간별로 소비 기록을 남기며 CSV나 후보 목록을 수정해도 겹치는 구간의 재평가를 막는다. 완전히 같은 실행은 보관한 보고서를 반환한다. DB를 직접 바꾸거나 지우는 공격까지 막는 운영 접근 통제는 후속 구현 대상이다.

평가 판정은 KEEP_CHAMPION 또는 ELIGIBLE_FOR_PAPER_REVIEW다. 후자는 사전 기준·단순한 블록 bootstrap·비용 스트레스를 통과한 선별 결과다. 실제 체결·독립 장기 성과를 검증하거나 활성 전략을 변경하지 않는다. 현재 paper 명령은 사용자가 지정한 기본 전략을 재생하며 연구 결과를 자동 승격하지 않는다.

**한투 읽기 전용 연결.** 모의 조회는 KIS_PAPER_APP_KEY와 KIS_PAPER_APP_SECRET, 실전 조회 전용 선택은 KIS_APP_KEY와 KIS_APP_SECRET 환경변수를 읽는다. 계좌번호·주문 키는 이 수집기에 필요하지 않다. 고정된 공식 호스트에서 tokenP와 일봉 조회만 사용하고 거래 endpoint 호출은 거절한다.

```bash
PYTHONPATH=src python -m trading_agent kis-download --symbols 069500 --start 2024-01-02 --end 2024-12-30 --environment paper --output artifacts/kis.csv
```

기본 available_at은 실제 수집 당시의 시각이므로, 과거 날짜의 백테스트에서 데이터가 당시 이용 가능했다고 주장하지 않는다. 과거 15:40 KST 이용 가능을 명시적으로 가정할 때만 --historical-availability-assumption을 추가한다. 이 경우 source에 ASSUMED_HISTORICAL_1540을 기록하며 실제 시점 아카이브와 구별한다. 현재 주식·ETF별 과거 데이터 범위·계약·호출 제한은 계정으로 직접 검증하지 않았다. 요청은 보수적인 프로세스 내 간격 제한만 적용하며 초당 허용량 보장은 아니다.

**검증 방법.**

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

장애·재시작·현금 예약 경쟁·부분 체결/취소 경합·반복 체결·불명 주문·오래된/미래 호가·시점 누수·후보 변경 경계·평가 구간 소비·비용 포함 백테스트/리플레이 일치를 검사한다. KIS 테스트는 주입한 가짜 transport를 사용하며 네트워크 인증이 성공했다는 근거로 사용하지 않는다.

2026-10-16 검증에서 34개 테스트를 통과했다. 합성 220세션·3종목 데모의 로컬 모의 주문 253건이 체결됐고, 현금·수량 대사와 백테스트/리플레이 최종 평가액이 일치했다. 같은 CLI 데모를 재실행해도 현금·수량·체결 건수가 바뀌지 않았다. 연구 판정은 KEEP_CHAMPION이었다. 상세 검증 기록은 실행 후 생성되는 artifacts/demo/validation.json에 있다. 이 결과는 실제 시장 수익성이나 한투 서버 연결의 검증 결과가 아니다.

**다음 작업.** 자격증명·해당 호스트 네트워크 접근을 준비한 환경에서 한투 일봉 조회 계약과 시세 품질을 먼저 확인한다. 이어 모의 주문·취소·주문 조회·잔고·WebSocket을 Broker 인터페이스와 대사에 연결하고 실제 장애·일자 전환을 관찰한다. 실제 계좌로 확장하기 전에는 시장 캘린더·기업행위·리스크 브레이커·평가 격리·승격 registry를 구현한다.
