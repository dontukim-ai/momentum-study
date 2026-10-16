# 자동 자기 개선 연구 에이전트 v1

기준 설계·개발 순서·학습·운영은 [상세 블루프린트](blueprint/README.md), 현재 완료와 미완료의 구분은 [진행 현황](blueprint/status.md)을 따른다. 이 문서는 현재 CLI 실행 안내다.

2026-10-16. `momentum-agent`는 후보 생성 → 동일 조건 평가 → 독립 검증 → 연구 champion 갱신을 실행한다. 미국 엔진의 `momentum.Ledger`와 한투의 시점 기반 `SelectionEngine`을 연결했다. 현재 전략 연결기는 **KRX/KRW, 연구 자본 최대 100만 원**이다. 미국 H1~H4의 잠긴 최종 검증을 대신 실행하거나 해제하지 않는다.

실전 키와 외부 LLM 없이 동작한다. 자동 주문보다 로직·자동 개선을 선행하는 범위에 따라 연구 전략만 갱신한다. 입력 파일의 새 데이터를 감시할 수 있지만 실제 시장 데이터 수집기는 아직 연결하지 않았다.

## 바로 실행

저장소 루트에서 실행한다. 개발 환경에는 로컬 한투 패키지가 포함된다. 개발 의존성을 제외하는 배포에서는 `uv sync --no-dev --extra agent --frozen`을 사용한다.

```bash
uv sync --extra agent --frozen
uv run --extra agent momentum-agent demo --output artifacts/agent-demo-v1
```

합성 예제는 다음 분기를 검증한다.

- 의도적으로 주기가 있는 합성 시장에서 80세션 독립 검증을 거친 연구 champion 갱신.
- 다음 일정에서 동일 champion 유지, 새 구간의 전진 shadow 비교.
- 같은 입력 재실행 시 저장한 결과 재사용.
- 별도 횡보 합성 시장에서 개선 없음 → 기존 전략 유지.
- 계좌·주문·체결 저널 생성 0건, 외부 호출 0회.

`verification.json`, `agent.json`, `repeat.json`, `flat.json`, 재현 입력과 SQLite 이력이 생성된다. 만들어진 수익은 소프트웨어 분기 검증용이며 시장 성과 증거가 아니다. 소스 변경 후 기존 예제 DB가 거부되면 과거 DB를 보존하고 새 출력 경로를 사용한다. 실제 연구의 소비 이력은 새 DB에도 가져와야 한다.

## 실제 입력으로 실행

먼저 정책을 고정한다. 기본 정책을 쓰려면 `--policy`를 생략한다. 예제에서 생성한 `policy.json`은 인위적 주기 fixture용이므로 실제 시장용으로 간주하지 않는다.

```bash
uv run --extra agent momentum-agent init \
  --db artifacts/live-research/research.sqlite3 --study krx-main

uv run --extra agent momentum-agent run \
  --db artifacts/live-research/research.sqlite3 --study krx-main \
  --csv data/krx/prices.csv --securities data/krx/securities.jsonl \
  --sessions data/krx/sessions.json --actions data/krx/actions.json \
  --output artifacts/live-research --watch --interval 30
```

`--watch`는 새 입력이 도착하면 동결된 일정을 따라 실행하고, 부족하면 다음 데이터를 기다린다. 연구 전략 갱신 뒤 매번 전체 탐색 범위를 바꾸지 않는다. `--max-ticks 2`로 유한 실행할 수 있다. `--max-cycles`는 이번 명령이 처리할 **전체 일정의 앞 N개**를 뜻하며, 재실행마다 N개의 새 검증을 추가하는 설정이 아니다. 생략하면 연구 초기화 시 고정한 예산을 사용한다.

입력 파일을 쓸 때는 임시 파일에서 완성한 후 교체하고, 같은 버전의 가격·종목 이력·달력을 함께 제공한다. 불완전한 입력이나 검증 오류는 worker를 멈춘다. 검증 구간을 사용한 실행을 자동 재시도하지 않는다.

입력 계약:

| 입력 | 필수 정보 |
| --- | --- |
| `prices.csv` | 기존 KIS CSV: session, symbol, open, high, low, close, volume, available_at, source. KRX 원시 가격은 정수 원 단위이며 수정가격과 혼용하지 않음 |
| `securities.jsonl` | 기존 KIS SecurityHistory: 유효·공개 시각, 거래 상태, 시장·상품, 관리·주의·레버리지·인버스 여부, bid/ask, 출처 |
| `sessions.json` | 거래일의 정렬된 JSON 배열. 실제 거래소 공급자에서 가져옴. 전체 종목의 가격이 빠진 날도 달력에서 지우지 않음 |
| `actions.json` | 기업행동 JSON 배열. security_id, action_type, ex_date, pay_date, split_ratio, cash_amount, available_at. 분할 또는 현금배당을 지원하며 미정·늦은 공개·소수 주식 처리 미정은 거부 |

```json
[
  {
    "security_id": "005930",
    "action_type": "cash_dividend",
    "ex_date": "2026-03-30",
    "pay_date": "2026-04-17",
    "split_ratio": null,
    "cash_amount": "100",
    "available_at": "2026-03-20T00:00:00+00:00"
  }
]
```

위 기업행동은 형식 예시다. 실제 종목의 배당 사실을 나타내지 않는다. `--actions`를 생략하면 공급자가 해당 데이터에 적용할 기업행동이 없다고 선언한 입력으로 다룬다. `--sessions` 생략 시 가격 행의 날짜를 사용하므로 전체 시장 결측을 감지할 수 없다. 실제 연구에서는 두 파일과 출처·커버리지를 확인해야 한다. 소스·정책·데이터 식별자는 동결 manifest에 묶인다.

## 네 단계와 일정

1. **후보 생성:** 고정한 lookback × ranking 조합을 생성한다. 현재 momentum / risk_adjusted / trend_stability를 지원한다. LLM이나 실행 코드 생성은 사용하지 않는다. 개발 데이터만 후보 평가에 전달한다.
2. **동일 조건 평가:** 시간 순서의 개발 fold 3개에서 같은 원장·비용·시점 필터·자본·체결 규칙으로 비교한다. 실제 다음 시가는 최초 수량을 늘리는 데 쓰지 않는다. 결측 시가는 취소, 보유 가격 결측은 실패로 처리한다.
3. **독립 검증:** 개발에서 선택한 단일 후보와 기존 전략만 예약한 최종 구간에서 비교한다. manifest와 소비 이력을 먼저 저장한다. 비용 차감 개선·낙폭·거래 수·paired block 신뢰구간·비용 스트레스가 모두 통과해야 한다.
4. **연구 전략 갱신:** 통과하면 revision을 증가시키고 연구 champion만 갱신한다. 이후 고정된 전진 shadow 구간에서 이전 champion과 비교한다. 실패하면 유지한다. shadow의 열화 표시는 별도 관찰 결과이며, 평가 기준을 바꾸거나 자동 주문하는 권한이 없다.

기본 일정:

| 단계 | 세션 수 |
| --- | --- |
| 최초 warmup / 개발 / 격리 / 최종 검증 | 61 / 120 / 5 / 40: 합계 226 |
| 다음 주기의 전진 shadow / 격리 / 최종 검증 | 20 / 5 / 40: 새 세션 65개 |
| 최대 최종 검증 횟수 | 초기화 시 8회로 고정 |

개발은 최근 120세션을 다시 사용할 수 있다. 최종 검증은 과거 최종 검증·shadow와 겹치지 않는 새 구간이다. 입력 파일에 더 많은 미래 구간이 있더라도 현재 후보 평가는 개발 prefix만 받는다. shadow 선정 결과도 관찰 구간 끝까지만 공개한다. 관찰한 데이터를 뒤에 최종 검증으로 바꾸지 않는다.

통계는 5일 circular block, 2,000회 재표집과 고정된 최대 검증 횟수에 대한 Bonferroni 예산을 사용한다. 작은 수익 차이만으로 승격하지 않으며, 짧은 표본의 신뢰구간이 0을 포함하면 유지한다. 이 절차는 정상성·블록 길이 등의 가정에 의존하는 연구용 추정이며 미국 H1~H4 명세의 Holm 검정을 구현했다는 뜻은 아니다. 이 런타임의 고정 세션 일정은 기존 KIS 연구기의 holdout_fraction 대신 사용된다.

shadow는 매 연구 champion에 대해 별도 자본으로 시작하는 전진 가상 평가다. 같은 shadow 블록이 조금씩 늘면 블록의 동일 시작점에서 재현한다. 주기 사이의 실제 포지션·결제·세금을 이어받는 실계좌 원장으로 해석하지 않는다.

## 이력·중단·복구

기존 한투와 같은 DB를 사용하면 그 평가 소비 이력을 즉시 존중한다. 별도 DB에서 가져오려면 실제 연구 실행 전에 다음 명령을 사용한다. 계좌·키·주문 데이터는 복사하지 않으며 원본은 읽기 전용으로 연다.

```bash
uv run --extra agent momentum-agent import-history \
  --db artifacts/live-research/research.sqlite3 \
  --source-db /path/to/previous-research.sqlite3

uv run --extra agent momentum-agent status \
  --db artifacts/live-research/research.sqlite3 --study krx-main

uv run --extra agent momentum-agent recover \
  --db artifacts/live-research/research.sqlite3 --cycle-id CYCLE_ID
```

`recover`는 저장된 완성 평가와 동결 계약·승격 기준을 다시 확인한다. 최종 평가를 재실행하지 않는다. 평가 중 끊겨 완성 결과가 없으면 worker를 정지한 뒤 명시적인 사유로 `abandon --cycle-id CYCLE_ID --reason '...'`을 사용한다. 예약된 구간은 계속 소비 상태로 남고 다음 일정으로 넘어간다.

원래 가격·종목·기업행동·거래일 이력의 prefix를 수정하면 거부한다. 새로 공개된 종목 이력은 공개 시각을 보존해서 추가한다. 빌드가 바뀌면 기존 연구를 조용히 이어가지 않으며, 이력을 보존한 버전 전환이 필요하다. 검증 예산 소진 후 마지막 shadow 관찰이 끝나면 감시 모드가 종료된다. DB 삭제나 이름 변경으로 예산·평가 구간을 초기화하지 않는다.

시장 단위 소비 이력은 종목명·종목군 변경도 차단한다. 기존 KIS 명령도 통합 런타임이 예약한 구간을 존중하도록 연결했다. 서로 다른 DB를 임의로 사용하는 worker 사이에는 자동 동기화가 없으므로 같은 연구 저장소 또는 소비 이력 가져오기가 필요하다.

## 구현 범위와 남은 연결

- 통화별 원장 반올림, 원화 비용 반올림, 배당채권/지급일, 정수 주식 분할, 독립 NAV 대사를 지원한다. 미지급 배당은 NAV에는 포함하되 매수 현금으로 사용하지 않는다.
- 비용은 예제 시나리오다. 실제 상품·날짜·계좌별 세금·수수료·호가·결제 규칙, 실제 달력·데이터 공급자와 주문 연결은 별도 작업이다.
- 신호는 기존 KIS 원시 가격 모멘텀 정의를 유지한다. 기업행동 회계 지원이 분할 보정 신호나 실제 시장 데이터의 완전성을 보증하지 않는다.
- 미국 엔진의 회계 기반을 실제 재사용하지만 미국 H1~H4 연구 판정이나 미국 종목의 자동 개선은 현재 어댑터가 지원하지 않는다. 원래 미국 CLI와 잠긴 검증 계약을 보존한다.
- 연구 전략 갱신은 수익 증가를 보장하지 않는다. 유지·대기·평가 실패가 정상적인 결과로 기록된다.

검증: 루트 **210개 통과**(기존 190개 + 통합 회귀 20개, slow 1개 제외), KIS 앱 **122개 통과**, mypy·ruff 통과. 회귀에는 전일 수량·결측 시가·배당과 분할·미지급 배당의 NAV/현금 구분·미래 정보 격리·중복 소비·복구·동시 worker·이력 가져오기·감시 재시작을 포함한다.

```bash
uv run pytest -q
uv run mypy src
uv run ruff check
uv run --directory apps/kis-agent python -m unittest discover -s tests -q
uv run --extra agent momentum-agent demo --output artifacts/agent-demo-v1
```
