**v0.2 — 구조화 LLM 후보와 KIS 모의 전용 브로커**

현재 v0.3의 진단·등록·관찰 기능과 검증 결과는 [후속 문서](06-shadow-operations.md)에 기록했다. 아래는 v0.2의 연동 계약과 당시 59개 테스트에 대한 기록이다.

2026-10-16에 v0.1의 로컬 경로를 확장했다. 실제 OpenAI/KIS 호출은 수행하지 않았다. 현재 환경의 자격증명과 api.openai.com, openapivts.koreainvestment.com 네트워크 허용은 구성되지 않은 상태로 확인했다. 아래 테스트 결과는 합성 데이터와 수기로 작성한 API 형태의 fixture에 근거한다.

| 영역 | 구현 | 확인한 범위 |
| --- | --- | --- |
| LLM | OpenAI Responses API, JSON Schema, 모델 명시, 2048 출력 토큰 한도 | 주입한 transport와 기록 형식 응답만 사용. 실제 모델 호출 미검증 |
| 후보 계약 | 가설·반증 조건·2~60 거래일 lookback, 예산·중복·추가 필드 검사 | 코드·배분·비용·평가 기준 변경은 거절 |
| 정보 시점 | 개발 구간만 요약, 입력 크기 한도, context_hash와 전략 구현 버전 연결 | 최종 구간 가격을 바꿔도 LLM 입력이 달라지지 않는 테스트 통과 |
| 요청 기록 | STARTED/COMPLETE/FAILED, 요청 해시, 완료 캐시, 실패도 시도 예산 소비 | 기본 1회, 명시적으로 최대 3회까지. 자동 재시도 없음 |
| 브로커 | 공통 Broker 계약과 고정 KIS 모의 호스트 | 실전 주문 URL·실전 키 fallback 없음 |
| 모의 주문 | 명시된 주문 의도, 고정 정책, 지정가, tick·세션·no-credit 매수 가능 수량 검사 | 수기 fixture의 입력·응답 형식과 OMS 연결 테스트 |
| 원장 | 누적 체결량·금액을 증분 체결로 전환, 정확한 notional 보존 | 반복 조회·부분 체결·수량 역행·개별 증분의 지정가 위반·재시작 테스트 |
| 복구 | UNKNOWN 재전송 금지, 명시된 날짜·조직번호·주문번호로 연결 | 비슷한 주문이 1개여도 자동 매칭하지 않음 |
| 계좌 | 로컬 별칭↔모의 계좌 identity 바인딩, 예상 정산 현금과 매수 가능 현금 구별 | 다른 backend·다른 identity·같은 DB의 중복 별칭 차단 |
| 인증 | 세션 토큰 재사용, CLI 간 0700/0600 비공개 캐시와 POSIX 파일 잠금 | fixture로 프로세스별 세션의 캐시 재사용 검사 |
| HTTP | 기존 proxy·CA 신뢰 유지, 허용 호스트·포트, 인증 요청 redirect 차단 | 리다이렉트/다른 목적지 차단 테스트 |

주요 구현은 src/trading_agent/proposals.py, brokers.py, kis_paper.py, paper_cli.py, token_cache.py, transport.py다. 기본 로컬 모의 명령과 읽기 전용 KIS 일봉 명령도 유지했다. DB는 부가 테이블을 추가하는 스키마 v2로 이동하며 기존 주문·체결·실험을 삭제하지 않는다.

**키 없이 검증하기.** 프로젝트 루트에서 실행한다.

```bash
uv sync --offline
uv run python -m unittest discover -s tests -v
uv run python -m examples.offline_integration --output artifacts/integration-v2
```

setuptools가 로컬 캐시에 없으면 최초 uv sync에는 패키지 다운로드가 필요하다. 런타임 외부 의존성은 없다. 설치 없이 실행하려면 PYTHONPATH=src python -m trading_agent를 사용할 수 있다.

통합 예제는 실제 계정·키를 사용하지 않는다. 합성 데이터와 수기 fixtures/paper_exchange.py, fixtures/llm-response.json을 사용하며 보고서의 provider는 recorded-openai-response, mode는 OFFLINE_FIXTURES_ONLY로 기록한다. 두 주문·세 번의 증분 체결, 정확한 금액 대사, 응답 유실 후 명시적 식별자 복구, 재시작 후 중복 방지를 확인한다. 임시 DB는 이 합성 테스트용이며 실제 연구에서는 최종 구간 소비 기록이 있는 영속 DB를 유지해야 한다.

예제 산출물:

- artifacts/integration-v2/report.md: 검증 범위 요약.
- artifacts/integration-v2/validation.json: 구현 해시, 네트워크 호출 0회, 대사·재시작 결과.
- artifacts/integration-v2/proposal.json: 개발 입력 해시·가설·반증 조건·후보·fixture 출처.
- artifacts/integration-v2/research.json: 후보 평가와 기존 전략 유지/모의 관찰 검토 판정.
- artifacts/integration-v2/broker.json: 브로커 상태·현금 기준·대사·이벤트 기록.

**구조화 제안과 연구.** 기록 형식 응답으로도 실제 제안 파서·컨텍스트 검사를 실행할 수 있다.

```bash
uv run kis-agent propose --csv artifacts/integration-v2/synthetic.csv --db artifacts/research-v2.sqlite3 --recorded-response fixtures/llm-response.json --output artifacts/proposal-v2.json
uv run kis-agent research --csv artifacts/integration-v2/synthetic.csv --db artifacts/research-v2.sqlite3 --proposal-file artifacts/proposal-v2.json --output artifacts/research-v2
```

실제 호출은 OPENAI_API_KEY와 OPENAI_MODEL을 실행 환경에 구성한 뒤 --recorded-response를 빼고 --model "$OPENAI_MODEL"을 명시한다. research --llm --model "$OPENAI_MODEL"로 한 단계에서 연결할 수도 있다. 어떤 모델이 사용 가능한지는 계정에서 확인해야 하며 기본 모델명을 추정하지 않는다.

LLM 입력은 개발 구간의 종목별 최근 수익률과 모멘텀 요약이다. 전체 데이터의 해시·최종 구간 가격·최종 평가 보고서는 보내지 않는다. 모델이 과거 시장을 학습하며 얻은 미래 지식까지 제거할 수 있다는 뜻은 아니므로 실제 기여는 이후 전진 검증이 필요하다. 모델 출력은 정수 파라미터와 설명 문자열로만 소비하며 코드·쉘·거래 도구로 실행하지 않는다.

후보 파일은 가설·반증 조건·lookbacks를 포함하고 개발 데이터·전략의 context_hash가 일치해야 한다. 평가기는 자신의 실제 구간 분할로 컨텍스트를 다시 검증한다. 거절·불완전 출력·비정상 JSON·예산 초과는 평가 전에 실패한다. API 실패를 임의 후보로 대체하지 않는다. 실패 후 다시 요청하려면 proposal_requests의 기록을 확인하고 propose --max-attempts를 명시적으로 늘린다. 이 옵션은 호출 비용의 시도 수 한도이며 반복 실험을 위한 최종 구간 재사용을 허용하지 않는다. 사용량이 응답에 없으면 null로 남기고 0으로 확정하지 않는다.

**KIS 모의 연결 설정.** 다음 환경변수는 실제 값 출력 없이 애플리케이션이 읽는다.

| 변수 | 용도 |
| --- | --- |
| KIS_PAPER_APP_KEY | 모의 App Key |
| KIS_PAPER_APP_SECRET | 모의 App Secret |
| KIS_PAPER_ACCOUNT | 8자리-01 형식 모의 종합계좌 |

키·계정은 소스나 주문 의도 파일에 넣지 않는다. 기본 로컬 데모의 설정과 모의 전용 정책을 구별한다. configs/kis-paper.example.json은 검토용 예시이며 계정·종목·호가 단위·한도를 확인한 복사본으로 초기화해야 한다.

```bash
uv run kis-agent kis-paper init --db artifacts/kis-paper.sqlite3 --account kis-paper --policy configs/kis-paper.example.json
uv run kis-agent kis-paper status --db artifacts/kis-paper.sqlite3 --account kis-paper
uv run kis-agent kis-paper quote --db artifacts/kis-paper.sqlite3 --account kis-paper --symbol "$KIS_SYMBOL"
```

init은 현재 보유 수량과 예상 정산 현금을 가져온다. 추적하지 않는 미체결이 있거나 보유 종목이 선언된 계약 범위를 벗어나면 초기화하지 않는다. 이미 초기화된 계좌를 다시 호출해도 로컬 원장을 덮어쓰지 않는다. 동일 물리 계좌는 같은 DB에서 한 별칭만 사용한다. 여러 DB를 만들어 계좌별 위험·예약을 우회하는 행위를 막는 전역 운영 레지스트리는 아직 없다.

잔고의 prvs_rcdl_excc_amt를 예상 정산 현금 기준으로 명시하며 dnca_tot_amt로 대체하지 않는다. 이는 실제 매수 가능 현금과 다르다. 매수 직전에는 ord_psbl_cash와 nrcvb_buy_qty를 별도로 확인하며, 매도는 ord_psbl_qty를 확인한다. 이 필드들의 계정별 실제 응답·정산 의미는 아직 직접 검증하지 않았다.

**명시적 주문 의도.** KIS_SYMBOL, KIS_QUANTITY, KIS_LIMIT_PRICE는 사용자가 검토한 모의 주문 계획의 값으로 구성한다. prepare는 키 없이 실행되며 주문을 보내지 않는다. 기존 의도 파일을 덮어쓰지 않으므로 같은 의사결정을 다시 보낼 때는 원래 파일과 key를 재사용한다.

```bash
uv run kis-agent kis-paper prepare --account kis-paper --symbol "$KIS_SYMBOL" --side BUY --quantity "$KIS_QUANTITY" --price "$KIS_LIMIT_PRICE" --ttl 120 --output artifacts/reviewed-paper-intent.json
uv run kis-agent kis-paper submit --db artifacts/kis-paper.sqlite3 --account kis-paper --intent artifacts/reviewed-paper-intent.json
uv run kis-agent kis-paper reconcile --db artifacts/kis-paper.sqlite3 --account kis-paper
```

submit은 실제 모의 서버에 주문할 수 있는 명령이다. 이번 작업에서는 실행하지 않았다. 키가 없거나 계좌·정책이 초기화되지 않으면 전송하지 않는다. KRX 지정가·선언된 tick·평일 09:00~15:20 KST 창만 지원하며 실제 휴장·VI·거래정지 캘린더와 실시간 WS는 아직 통합하지 않았다. HTTP 시세의 as_of는 수신 시각이며 거래소 체결 시각으로 확정하지 않는다. 연구 결과가 이 명령을 자동으로 실행하거나 후보를 활성화하지 않는다.

명시된 주문을 보낸 뒤 응답이 유실되면 UNKNOWN으로 유지한다. KIS에 로컬 client key가 전달되지 않아 비슷한 주문을 자동 채택하지 않는다. 운영자는 orders --day에서 확인한 주문일·조직번호·주문번호를 대조한 뒤 adopt --order-key --day --orgno --odno로 연결할 수 있다. 계좌·종목·방향·수량·가격·날짜를 검증하고 다른 의도에 이미 연결된 주문을 거절한다.

**실제 계약이 확인될 때까지 남기는 제한.**

- 공식 정정취소 샘플은 취소 전 psbl_qty 확인을 요구하지만 확인한 취소 가능 조회 예제에는 실전 TTTC0084R만 있다. 모의용 ID를 만들어 쓰거나 rmn_qty를 취소 가능 수량으로 간주하지 않는다. 현재 CLI의 실제 취소 POST는 기본 차단이며, 허용된 fixture 계약에서만 요청·부분 체결 경합 경로를 테스트했다.
- 실제 수수료를 응답에서 제공하지 않으면 0을 잠정 원장값으로만 기록하고 broker_issues·계좌 정지로 표시한다. tot_fee도 현재 fixture 계약의 기대 필드이며 실제 응답에서 확인된 값이 아니다. 수수료·세금·정산 현금을 대조한 뒤에야 다음 단계로 진행할 수 있다. 수량 변화 없이 금액만 바뀐 정정은 자동으로 장부에 흡수하지 않는다.
- resume는 대사 일치·미확정 주문 없음·미확인 계약 없음이 확인된 경우 운영자가 정지를 해제하는 명령이다. 차이나 계약 결함을 무시하는 기능이 아니다. 정책 변경·현금 보정은 별도의 검토 가능한 마이그레이션이 필요하다.
- GET 페이지는 응답의 tr_cont와 연속조회 키를 함께 확인한다. 헤더가 없거나 커서가 반복되거나 페이지 예산을 소진하면 일부 결과를 완전한 조회로 사용하지 않는다.
- 모의 TR ID·입력은 공식 고정 커밋을 확인했지만 출력 스키마와 계정 자격은 수기 fixture로만 검사했다. 실제 호출 전에 docs/integration-sources.json의 미확인 출력 계약을 대조해야 한다.

59개 테스트가 통과했다. 정식 다음 단계는 읽기 전용 실제 인증·시세·잔고·조회 출력 캡처를 검증하고, 취소 가능 조회·수수료·정산 계약을 확인한 뒤 모의 주문 통합 시험을 진행하는 것이다. 실전 주문, 코드 자기 수정, WS·시장 상태 브레이커·실거래 배포 레지스트리는 이번 범위에 넣지 않았다.
