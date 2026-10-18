# 실데이터 공급원 결정과 필드표 (DATA-01)

상태: 초안. "확인" 열이 `미확인`인 항목은 PC에서 `tools/data_probe.py`로 실제 응답을 받아 확정한다. 클라우드 세션에서는 아래 공급원에 접속할 수 없다.

## 1. 결정

사용자 위임(2026-10-17): **무료 범위**에서 공급원을 고른다.

| 용도 | 공급원 | 선택 이유 | 대체 불가·한계 |
| --- | --- | --- | --- |
| 일별 시세(주식) | 공공데이터포털 금융위원회 주식시세정보 | 기준일별로 **그날 상장된 전 종목**을 준다. 이후 상장폐지된 종목도 과거 날짜 응답에 남아 생존 편향이 없다 | 이력 시작이 2020년 전후(미확인). 호가(bid/ask) 없음 |
| 일별 시세(ETF) | 공공데이터포털 금융위원회 증권상품시세정보 | 시장 ETF 기준 전략(RES-01)과 ETF 후보 | 위와 같음 |
| 상장폐지·관리종목·거래정지 이력 | KRX KIND 공시·현황 목록 | 지정일·해제일로 시점별 상태를 재구성 | API가 아니라 웹 목록. 자동 수집 조건(이용 약관·요청 간격) 확인 필요 |
| 기업행동(분할·병합·감자·증자·배당) | OpenDART 공시검색·주요사항보고서 | 접수 시각이 있어 공개 시점(`available_at`)을 정할 수 있다 | 권리락일·지급일은 공시 본문에서 추출해야 하는 항목이 있음(미확인) |
| 실시간 운용 | KIS 조회 API | 당일 종가·호가를 제때 받는 유일한 경로 | 과거 상태 이력 없음. 검증 데이터로 쓰지 않음 |

유료 자료(FnGuide 등)와 KRX 정보데이터시스템 스크래핑은 이번 결정에서 제외한다. 이력이 부족해 사이클 수가 모자라면 그때 다시 검토한다.

## 2. 연구 입력 계약과의 대응

연구 입력(`ResearchInput`)은 세 파일이다: 가격 CSV(`session,symbol,open,high,low,close,volume,available_at,source`), 종목 상태 JSONL(`SecurityState`), 기업행동 JSON.

### 2.1 가격 CSV ← 금융위원회 주식·증권상품 시세

| 연구 필드 | 공급원 필드(추정) | 변환 | 확인 |
| --- | --- | --- | --- |
| session | `basDt` (YYYYMMDD) | 날짜 변환 | 미확인 |
| symbol | `srtnCd` (단축코드 6자리) | 그대로. 영구 ID는 `isinCd`로 따로 보관(DATA-02) | 미확인 |
| open/high/low/close | `mkp`/`hipr`/`lopr`/`clpr` | 원 단위 Decimal. 수정주가 아님(원시가) 여부 확인 필요 | 미확인 |
| volume | `trqu` | 정수 | 미확인 |
| available_at | 응답에 없음 | §3 정보 시계 정책 | 정책 |
| source | 고정 문자열 `fsc-stock-price` / `fsc-etf-price` + 수집 manifest 해시 | | 정책 |
| (참고) 시장 구분 | `mrktCtg` (KOSPI/KOSDAQ/KONEX) | 종목 상태의 market | 미확인 |
| (참고) 상장주식수·시가총액 | `lstgStCnt`, `mrktTotAmt` | 진단용. 신호에 쓰지 않음 | 미확인 |

시가가 0이거나 비어 있는 행(거래 없음·정지)은 **채우지 않는다**. 가격 행을 만들지 않고 DATA-05 품질 코드로 격리한다(CLAUDE.md 3번: 결측 시가 → 매수 취소).

### 2.2 종목 상태 JSONL ← KIND + 시세 응답

| 연구 필드 | 원천 | 변환 | 확인 |
| --- | --- | --- | --- |
| symbol | 시세 `srtnCd` | | 미확인 |
| status | KIND 상장폐지일·거래정지 지정/해제 | NORMAL / SUSPENDED / DELISTED 이벤트 | 미확인 |
| asset_type | 공급 API 종류(주식시세=STOCK, ETF시세=ETF) | ETN·기타는 OTHER | 정책 |
| market | 시세 `mrktCtg` | | 미확인 |
| managed | KIND 관리종목 지정/해제 | 지정일~해제일 true | 미확인 |
| caution | KIND 투자주의·경고·위험 지정 | 지정 기간 true | 미확인 |
| leveraged / inverse | ETF 종목명·기초지수 정보 | 이름 규칙만으로 정하지 않는다. 확인 불가 종목은 후보에서 제외 | 미확인 |
| bid / ask | **무료 이력 없음** | §4 | 대체 불가 |
| effective_at / available_at | KIND 지정일·공시 시각 | 공시 시각이 없으면 지정일 장 마감 후로 보수적으로 둔다 | 미확인 |

### 2.3 기업행동 JSON ← OpenDART

PC 탐침 결과(2026-10-17, 기준일 2024-06-03, `pblntf_ty=B` 주요사항보고 31건)로 공시목록(`list.json`) 필드를 확인했다.

| 공시목록 필드 | 확인된 형태 | 쓰임 | 확인 |
| --- | --- | --- | --- |
| `rcept_no` | 14자리 숫자 문자열, 앞 8자리가 접수일 | 공시 고유 ID. 정정공시도 별도 번호 | 확인 |
| `rcept_dt` | YYYYMMDD, **시각 없음** | `available_at`의 날짜(§3) | 확인 |
| `stock_code` | 6자리. 31건 중 4건 빈 값(비상장) | 단축코드 연결. 빈 값은 연구 대상 아님 | 확인 |
| `corp_code` | 8자리 DART 고유번호 | `corpCode.xml`(약 3.6MB)로 종목코드와 연결 | 확인 |
| `corp_cls` | Y/K/N/E | 유가증권/코스닥/코넥스/기타 | 확인 |
| `report_nm` | 예: `주요사항보고서(영업양수결정)`, `[기재정정]…` | 행동 종류 분류, 정정 여부 | 확인 |
| `rm` | 비고 문자 조합(예: 공, 정, 공정). 31건 중 9건 빈 값 | 진단용 | 확인 |

공시목록에는 비율·권리락일·지급일 같은 **세부 값이 없다**. 세부 값은 주요사항보고서 종류별 상세 API에서 받아야 한다(DATA-01b 탐침으로 필드 확인).

| 연구 필드 | 원천 | 확인 |
| --- | --- | --- |
| security_id | 공시목록 `stock_code` | 확인 |
| action_type | `report_nm`의 보고서 종류(무상증자·유상증자·감자·주식분할/병합 등) | 분류 규칙은 DATA-02 |
| ex_date / pay_date | 종류별 상세 API(권리락일·신주배정기준일 등) | 미확인 |
| split_ratio / cash_amount | 종류별 상세 API(배정비율·감자비율 등) | 미확인 |
| available_at | `rcept_dt`에 시각이 없으므로 **접수일 18:00 KST**로 둔다. 장중 공시였더라도 그날 결정에는 쓰지 않는다(보수적) | 정책 |

정정공시는 원공시를 덮어쓰지 않고 새 이벤트로 쌓는다(DATA-02). 현금배당은 주요사항보고가 아니라 거래소 공시·사업보고서 항목이라 이번 공시목록에 나오지 않는다. 배당 원천은 DATA-02에서 따로 정한다.

공시 이벤트 원장(DATA-02a): `momentum.krx.dart_events`와 `tools/dart_ledger.py`는 탐침이 저장한 공시목록 JSON 페이지를 네트워크 없이 검증·분류하여 추가 전용 JSONL로 기록한다. 접수일 18:00 Asia/Seoul부터 `as_of`에 노출하며 원본 파일 SHA-256을 보존한다. 같은 접수번호의 내용 변경은 배치 전체를 거부하고, 정정공시는 별도 번호로 유지한다. 빈 종목코드는 보존하되 `listed_only()`에서는 제외한다. `uv run python tools/dart_ledger.py --pages <탐침 out>/dart/<기간>/page-*.json --ledger data/krx/dart_events.jsonl`로 실행한다. 완료 근거는 가짜 페이지를 사용한 합성 회귀 범위이며 실데이터 검증은 미완료다. 비율·권리락일 추출과 연구용 CorporateAction 생성은 후속 작업이다. 원장 경로 `data/krx/`는 Git에서 제외한다.

### 2.4 공공데이터포털 탐침 상태

첫 탐침(2026-10-17)에서 주식·ETF 시세 모두 HTTP 403이 났다. 활용신청 미승인 또는 승인 직후 반영 지연이 흔한 원인이다. DATA-01b 탐침은 오류 응답 본문도 저장해 원인 메시지를 확인한다.

재탐침 명령(PC): `uv run python tools/data_probe.py --date 2024-06-03 --out artifacts/data-probe --dart-bgn 2024-04-01 --dart-end 2024-06-30 --with-dart-detail`. 오류 응답은 `error-*.xml/json`으로 저장되고 summary 메시지에 앞 200자가 남는다. 상세 엔드포인트(`pifricDecsn`·`fricDecsn`·`piicDecsn`·`crDecsn`)는 추정이며 결과로 확인한다.

## 3. 정보 시계(available_at) 정책

공공데이터 시세는 기준일 다음 영업일 이후에 갱신된다(정확한 시각 미확인). 그대로 쓰면 t일 종가가 t+1일 장 시작 전에 보이지 않아, 전일 종가로 수량을 정하는 현재 체결 규칙과 맞지 않는다.

결정(사전 등록, 과거 검증용):
- 과거 구간의 t일 시세 `available_at`은 **t일 18:00 KST**로 둔다. 같은 값이 그날 거래소 종가로 이미 공개됐다는 가정이다. 이 가정은 입력 묶음의 `basis`에 명시한다.
- 이 가정은 E1(예약 과거 구간) 증거에만 쓴다. E2·E3 실시간 증거는 실제 수신 시각을 `available_at`으로 쓰며, 당일 종가는 KIS로 받는다.
- 정정된 시세가 나중에 들어오면 원래 값을 바꾸지 않고 정정 이벤트로 남긴다(DATA-05).

## 4. 호가(spread) 공백

현재 선정 규칙은 bid/ask가 없으면 `SPREAD_UNAVAILABLE`로 후보에서 뺀다. 무료 과거 호가 자료가 없으므로 실데이터를 그대로 넣으면 모든 종목이 빠진다. 과거 호가를 현재 호가로 채우지 않는다(implementation-plan의 원칙).

2026-10-18 사용자 결정으로 **선택지 1을 채택**했다(FIN-01a, 합성 회귀 범위 구현). `SelectionPolicy.spread_mode`의 기본값 `quoted`는 기존 호가 필터를 유지한다. 사전 등록한 `historical_unavailable`은 bid/ask가 없을 때만 호가 제외를 생략하고 `spread_source=unavailable_preregistered`를 표시한다. 실제 호가가 있으면 `spread_source=quoted`로 표시하고 기존 spread 계산과 넓은 호가 제외를 적용한다. 유동성·변동성·가격·이력·시장·상태 필터는 유지하며 호가를 생성하거나 결측을 채우지 않는다.

이 모드의 `AgentPolicy`는 편도 slippage **최소 25bp**를 강제한다. 결과를 보기 전에 정한 보수값으로, 최소 가격 1,000원대의 1원 호가 단위 반 틱 이상과 기본 5bp의 차이를 덮는다. 선정 정책 digest가 평가 계약 ID에 반영되므로 다른 모드의 결과를 동일 계약으로 비교할 수 없다. status·tick의 기존 policy 요약에서 모드를 확인한다.

현재 주문 경로는 `SelectionPolicy`를 받지 않으며 공통 연구 수량 계획에는 주문 거부 검사를 넣지 않는다(주관자 결정). 대신 관찰 release에 근거 사이클의 `spread_mode`와 `execution_eligible`을 동결한다. `historical_unavailable`은 실행 자격 false이며 주문 권한은 여전히 없다. 후속 ExecutionRelease는 실행 자격 false를 거부해야 한다([루프 계약 §11](loop-contracts.md#11-release--s4--운용-loop-06)).

**선택지 2(전진 수집)**는 병행 과제로 남긴다. KIS로 실제 수신 시각의 호가를 수집하고, 충분한 이력이 쌓인 뒤 실제 호가 조건의 E2/E3 검증에 사용한다. 25bp 하한은 실제 비용 적합성·실데이터 검증 완료를 뜻하지 않는다.

## 5. 키와 실행 위치

- 공공데이터포털 서비스 키, OpenDART 인증키는 무료 발급이다. PC 환경변수 `DATA_GO_KR_SERVICE_KEY`, `OPENDART_API_KEY`에만 둔다. 저장소·대화·로그에 남기지 않는다.
- 공공데이터포털 키는 마이페이지의 **일반 인증키(Decoding)** 값을 넣는다. 도구가 URL 인코딩을 하므로 Encoding 값을 넣으면 이중 인코딩으로 거부된다.
- 탐침 실행(PC, 읽기 전용): `uv run python tools/data_probe.py --date 2024-06-03 --out artifacts/data-probe`. 결과 `artifacts/data-probe/summary.json`(필드 목록·빈 값 수·표본값·형태)과 `manifest.json`(원본 해시)을 근거로 이 문서의 `미확인` 칸을 채운다. 원본 파일은 저장소에 커밋하지 않는다. KIND는 목록 주소와 폼을 `--kind-url`·`--kind-form`으로 직접 지정해야 하며, 지정하지 않으면 건너뛴다.
- 수집은 PC에서 실행한다. 클라우드 세션은 수집 결과(원본 해시가 붙은 묶음)를 검증·변환하는 코드만 다룬다.

## 6. 다음 작업

1. DATA-01a: `tools/data_probe.py`로 PC에서 표본을 받아 이 문서의 `미확인` 칸을 채운다.
2. DATA-02: 원본 → 가격 CSV·종목 상태 JSONL·기업행동 JSON 변환기, 영구 ID(ISIN), 정정 이벤트.
3. DATA-03: 거래소 달력(시세 응답의 기준일 집합 + 공개 휴장일).
4. DATA-05·FIN-01: 품질 코드, §4 호가 공백 정책 등록.
