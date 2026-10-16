**v0.4 — 실전 키 연결과 100만 원 운용 상한**

사용자는 모의 키 대신 실전 계좌 키를 사용하고 가용 자본을 100만 원으로 명시했다. 이에 실전 키 전용 인증·현재가·잔고·최근 주문 조회 경로를 추가했다. 모의 키 발급은 이 경로의 선행 조건이 아니다. 실제 매매 주문은 아직 구현·실행하지 않았다.

실전 키는 실행 환경의 비밀 변수로 등록한다. 채팅·소스·JSON 설정에 실제 값을 넣지 않는다.

| 변수 | 값 |
| --- | --- |
| KIS_APP_KEY | 실전 App Key |
| KIS_APP_SECRET | 실전 App Secret |
| KIS_ACCOUNT | 실전 종합계좌 8자리와 상품코드 01을 합친 `8자리-01` |

화면의 계좌 앞 8자리만으로 상품코드를 추정해 실행하지 않는다. 계좌에서 확인한 전체 번호를 등록한다. 현재 현금 주식 계좌의 상품코드 01만 지원한다. KIS_PAPER_* 변수는 이 실전 경로에서 읽지 않는다. OpenAI 키는 한투 연결과 별도이며 구조화 LLM 후보 생성 시 필요하다.

```bash
uv run kis-agent doctor
uv run kis-agent doctor --probe-real --symbol "$KIS_SYMBOL" --output artifacts/real-readiness.json
```

KIS_SYMBOL은 조회할 국내 종목의 6자리 코드다. 이는 조회 대상이며 매매 대상 확정이나 주문 의도가 아니다. doctor 기본 실행은 네트워크를 호출하지 않는다. `--probe-real`을 지정하면 인증·시세·잔고·당일 주문 조회만 시도한다. 모의 probe와 동시에 선택할 수 없다. 사용자 지정 호스트나 실전/모의 자동 대체도 없다.

실전 호스트는 `https://openapi.koreainvestment.com:9443`로 고정했다. 관리 환경의 네트워크 허용에는 `openapi.koreainvestment.com`이 필요하다. 키 존재와 호스트 허용은 접속 성공을 보장하지 않으며 실제 응답으로 확인한다. 현재 환경에서는 키와 호스트 허용이 없어서 HTTP 요청 0회로 BLOCKED_CONFIGURATION_OR_POLICY를 기록했다.

| 경로 | API ID | 허용 메서드 |
| --- | --- | --- |
| /oauth2/tokenP | OAuth | POST |
| /uapi/domestic-stock/v1/quotations/inquire-price | FHKST01010100 | GET |
| /uapi/domestic-stock/v1/trading/inquire-balance | TTTC8434R | GET |
| /uapi/domestic-stock/v1/trading/inquire-daily-ccld | TTTC0081R, 최근 80일 범위만 지원 | GET |

잔고·최근 주문 API ID와 요청 필드는 공식 저장소 고정 커밋 [잔고 샘플](https://github.com/koreainvestment/open-trading-api/blob/2077773a69a428daf63e148182511f068cc45de3/examples_llm/domestic_stock/inquire_balance/inquire_balance.py), [주문 조회 샘플](https://github.com/koreainvestment/open-trading-api/blob/2077773a69a428daf63e148182511f068cc45de3/examples_llm/domestic_stock/inquire_daily_ccld/inquire_daily_ccld.py)을 재확인했다. 실제 계좌 응답 계약은 아직 미검증이다. 빈 당일 주문 응답으로 전체 주문·체결·취소 계약을 검증했다고 간주하지 않는다.

새 KisRealReadOnly는 OMS Broker가 아니며 submit·cancel 메서드를 제공하지 않는다. 허용 작업 목록은 위 세 GET뿐이고 OAuth 이외의 POST를 만들지 않는다. 진단 transport에서도 메서드·호스트·경로를 다시 확인한다. 키·토큰·계좌번호·잔고·주문번호·원시 응답·원시 오류는 진단 보고서에 저장하지 않는다. 토큰은 기존 비공개 캐시를 사용하며 실전/모의의 캐시 키를 구별한다.

현금 필드는 예수금 dnca_tot_amt와 예상 정산 현금 prvs_rcdl_excc_amt를 구별한다. 둘 중 하나가 없으면 대체하지 않는다. 어느 값도 검증된 매수 가능 금액으로 표시하지 않는다. 연속조회 누락·반복·예산 소진·페이지 간 충돌이 발생하면 일부 결과를 완전한 잔고로 사용하지 않는다. 계좌 초기화나 원장 갱신도 수행하지 않는다.

**100만 원 상한의 의미.** REAL_CAPITAL_LIMIT와 configs/real-readonly.json에 요청한 1,000,000원 한도를 기록했다. 계좌에 더 많은 금액이 조회돼도 이 설정은 자동으로 늘어나지 않는다. 현재는 조회만 있으므로 주문 예약·보유 노출을 포함해 이 한도를 집행하는 실전 주문 엔진은 없다. 이는 현재 주문 기능을 활성화하는 설정 파일도 아니다. 실전 주문을 구현할 때 기존 보유·미체결·수수료·신용 차단과 함께 총자본 한도를 집행해야 한다. 종목·수량·가격·실행 시점과 구체적 손실 한도는 아직 정하지 않았다.

**검증.** 88개 테스트 통과. 기존 78개에 실전 키/모의 키 분리, 실전 호스트·API ID, 주문 API 사전 거절, 다른 계좌 조회 차단, 현금 의미 구분, fixture 출처 표시, 정책 차단·오류 비밀 보호, 연속조회 실패, 토큰 캐시 분리, 키 없는 CLI 실행을 추가했다. 수기 fixture 성공을 실서버 연결이나 실거래 준비 완료로 표현하지 않는다.
