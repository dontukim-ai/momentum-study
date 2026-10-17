**조사 근거와 채택 판단**

확인일: 2026-10-16. GitHub 공식 문서·일부 구현 파일을 직접 읽었다. 아래 기능 설명은 문서 또는 정적 코드 확인에 근거하며, 서비스 동작·수익률·실거래 적합성을 실행으로 검증했다는 뜻은 아니다. 고정 버전과 확인 파일은 source-manifest.json에 기록했다.

**스킬 조사.** 현재 세션의 Public Equity Investing은 종목 리서치·가설 추적·포트폴리오 리스크 분석용이다. 브로커 실행이나 자기 개선 전략 엔진 전용 스킬은 아니다. 이번에는 분류를 위한 라우터만 검토했고 실제 투자 분석 워크플로는 적용하지 않았다. plugin-management를 이용한 “한국투자 Korea Investment KIS trading” 검색은 결과가 없었다. 이는 해당 검색 범위의 결과이며 외부 MCP의 부재를 의미하지 않는다.

| 구분 | 확인한 대상 | 채택 판단 |
| --- | --- | --- |
| 설치된 스킬 | Public Equity Investing | 이후 공시 기반 리서치·thesis tracker의 보조 절차로 검토. 주문 권한은 부여하지 않음 |
| 설치된 스킬 | plugin-management | 연동 탐색에 사용. 한투 플러그인은 이번 검색에서 발견되지 않음 |
| 설치된 스킬 | cloud-environment-runtime | 조사 환경과 네트워크 정책 확인에 사용 |
| 공식 외부 MCP | KIS Code Assistant MCP | API 탐색·샘플 조회용 개발 보조 도구로 우선 검토 |
| 공식 외부 MCP | KIS Trading MCP | 조회·모의투자 프로토타입 참고. 운영 주문 경로는 버전 고정 어댑터로 구현 |
| 커뮤니티 스킬 | agiprolabs/claude-trading-skills | 일부 절차만 참고. 암호화폐 가정·수식·스크립트 검토 후 국내시장에 맞춰 작성 |

**한투 공식 레퍼런스.** [open-trading-api](https://github.com/koreainvestment/open-trading-api/tree/2077773a69a428daf63e148182511f068cc45de3)는 2026-10-14 커밋을 확인했다. 단일 API 예제인 examples_llm, 상품별 통합 예제인 examples_user, llms.txt, MCP, strategy_builder, backtester가 함께 있다.

| 자료 | 직접 확인한 내용 | 설계에 반영할 점 |
| --- | --- | --- |
| [Code Assistant MCP](https://github.com/koreainvestment/open-trading-api/blob/2077773a69a428daf63e148182511f068cc45de3/MCP/KIS%20Code%20Assistant%20MCP/README.md) | API 검색·코드 탐색, 데이터 목록에 의존, 실제 API 호출은 별도 | 개발·문서 탐색에 사용. 운영 체결 엔진과 분리 |
| [Trading MCP](https://github.com/koreainvestment/open-trading-api/blob/2077773a69a428daf63e148182511f068cc45de3/MCP/Kis%20Trading%20MCP/Readme.md) | 시세·잔고·주문, 실전/모의 환경, 동적 코드 다운로드·실행 | 샌드박스에 적합한 참고 구현. 실거래 프로세스에 최신 원격 코드를 즉시 반영하지 않음 |
| [Strategy Builder](https://github.com/koreainvestment/open-trading-api/blob/2077773a69a428daf63e148182511f068cc45de3/strategy_builder/README.md) | 지표·조건 기반 전략, BUY/SELL/HOLD, .kis.yaml 공유 형식 | 초기 후보는 제한된 DSL로 작성. 시그널 외에 포트폴리오 비중·실험 메타데이터 추가 필요 |
| [Backtester](https://github.com/koreainvestment/open-trading-api/blob/2077773a69a428daf63e148182511f068cc45de3/backtester/README.md) | Docker 기반 Lean, .kis.yaml, Grid/Random Search, 비동기 MCP 도구 | 재사용을 검토할 첫 평가 엔진. 한국시장 체결·비용 모델 검증은 별도 필요 |
| [현금주문 예제](https://github.com/koreainvestment/open-trading-api/blob/2077773a69a428daf63e148182511f068cc45de3/examples_llm/domestic_stock/order_cash/order_cash.py) | 환경별 TR ID, 거래소 구분, 주문 수량·가격 필드 | API 계약을 한 곳에서 관리하고 모의/실전 환경 혼용 차단 |
| [일봉 조회 예제](https://github.com/koreainvestment/open-trading-api/blob/2077773a69a428daf63e148182511f068cc45de3/examples_llm/domestic_stock/inquire_daily_itemchartprice/inquire_daily_itemchartprice.py) | 1회 최대 100건, 수정주가/원주가 구분 | 날짜 구간 분할·중복 제거·결측 검사·원주가/조정주가 분리 |

공식 샘플도 통합 검증이 필요한 이유가 구체적으로 있다.

- 현금주문 예제의 매수/매도 TR ID는 TTTC0012U/TTTC0011U이고, 같은 커밋의 backtester/kis_backtest/providers/kis/constants.py에는 TTTC0802U/TTTC0801U가 있다. 어느 쪽이 지원되지 않는다고 단정할 근거는 없다. 현재 API 계약·환경별 지원 여부를 확인하고 어댑터의 단일 매핑으로 관리해야 한다.
- backtester/kis_backtest/codegen/generator.py의 기본값은 commission_rate=0.00015, tax_rate=0.002, slippage=0.0이다. 이 수치는 샘플 설정이며 현재 사용자 계좌·상품·시점의 비용이 아니다. 주식/ETF별 세금, 실제 수수료, 스프레드와 미체결을 별도로 반영해야 한다. 코드 생성의 수수료 통화도 계좌 기준 통화와 함께 점검한다.
- Trading MCP의 tools/base.py는 main 브랜치의 인증 코드를 다운로드하고 subprocess로 샘플을 실행한다. 운영 계좌에서는 검토한 코드·의존성을 고정하고 배포 변경을 추적하는 편이 재현성과 장애 분석에 유리하다.
- 인증 샘플은 토큰 재사용·만료일 저장·실전/모의 전환을 제공한다. 샘플의 sleep 값은 보장된 호출 한도가 아니다. 계정·API별 현재 호출 한도와 WebSocket 제한은 공식 포털에서 구현 시점에 확인해야 한다.

**자기 개선·퀀트 연구 레퍼런스.**

| 자료 | 채택할 아이디어 | 그대로 도입하지 않을 부분 |
| --- | --- | --- |
| [Microsoft RD-Agent](https://github.com/microsoft/RD-Agent/tree/484776c211e4fbbeef03e0ec00d6bbee7362a4f4) | 가설→실험 생성→코딩→실행→피드백→trace 축적. 가장 직접적인 자기 개선 연구 루프 참고 대상 | 전체 프레임워크를 MVP의 필수 런타임으로 만들지 않음. 기존 Qlib 중국시장 데이터·기간 설정을 국내시장에 그대로 쓰지 않음 |
| [Qlib](https://github.com/microsoft/qlib/tree/54355232463878d2eebb91fe0ee5fa7fa1f5976c) | 데이터·특징·모델·백테스트 분리, 연구 워크플로와 실험 기록 | KIS 브로커 어댑터와 국내시장 데이터 준비가 자동 해결된다고 가정하지 않음 |
| [Qlib PIT 문서](https://github.com/microsoft/qlib/blob/54355232463878d2eebb91fe0ee5fa7fa1f5976c/docs/advanced/PIT.rst) | 재무자료의 기간·공표일·수정 이력을 함께 보존 | 최신 재무제표로 과거 시점의 판단을 재구성하지 않음 |
| [TradingAgents](https://github.com/TauricResearch/TradingAgents/tree/1394a3f72aa4393e1a98f51b382434c4b4c2d972) | 리서치·트레이더·리스크 역할의 입출력 분리, 날짜별 근거·결정 기록 | LLM 간 토론·합의를 통계적 검증이나 주문 통제로 사용하지 않음 |
| [NautilusTrader](https://github.com/nautechsystems/nautilus_trader/tree/b6d6f1da5610a429f8a08e418cf149c1d6cd327d) | 이벤트 기반 시뮬레이션, 백테스트와 실거래 간 로직 공유 | 분봉·호가 단계의 대안으로 보류. 이번 조사에서 KIS 기본 어댑터를 확인하지 않았으므로 있다고 가정하지 않음 |

RD-Agent의 docs/scens/quant_agent_fin.rst와 components/workflow/rd_loop.py를 확인했다. 문서의 금융 연구 루프와 코드의 hypothesis generation, experiment generation, coding, running, feedback, trace 구조가 연결된다. 이 구조가 특정 국내 전략의 수익성을 보장한다는 근거는 아니다.

Qlib의 PIT 문서는 공표 시점과 재무 대상 기간을 구분한다. 이 설계에서는 이를 가격·공시·뉴스·에이전트 기억까지 확장해, 의사결정 시점에 실제 이용 가능했던 정보만 사용하도록 한다.

**커뮤니티 스킬 평가.** [claude-trading-skills](https://github.com/agiprolabs/claude-trading-skills/tree/981e1d736cdc02bdc1c55c74ec9224e956414706)는 README상 Crypto/DeFi-first 컬렉션이다. 아래 4개 SKILL.md 원문을 읽었으며 설치·스크립트 실행은 하지 않았다.

| 스킬 | 참고할 내용 | 수정·검증할 내용 |
| --- | --- | --- |
| [strategy-framework](https://github.com/agiprolabs/claude-trading-skills/blob/981e1d736cdc02bdc1c55c74ec9224e956414706/skills/strategy-framework/SKILL.md) | 반증 가능한 가설, 진입·청산·사이징·성과 기준을 미리 정의 | 거래 세션·호가 단위·KRW 원장·한국 ETF 특성 반영 |
| [walk-forward-validation](https://github.com/agiprolabs/claude-trading-skills/blob/981e1d736cdc02bdc1c55c74ec9224e956414706/skills/walk-forward-validation/SKILL.md) | 시간순 검증, 라벨 중첩 제거, 다중 실험 기록 | 암호화폐용 기간 예시·통계 코드를 그대로 채택하지 않음 |
| [risk-management](https://github.com/agiprolabs/claude-trading-skills/blob/981e1d736cdc02bdc1c55c74ec9224e956414706/skills/risk-management/SKILL.md) | 노출·손실·드로다운·서킷브레이커의 계층 | 예시 손실률은 사용자 한도로 사용하지 않음. UTC 자정 리셋을 KRX 거래일 기준으로 변경 |
| [trade-journal](https://github.com/agiprolabs/claude-trading-skills/blob/981e1d736cdc02bdc1c55c74ec9224e956414706/skills/trade-journal/SKILL.md) | 거래 근거·버전·귀속 손익·리뷰 기록 | 한 번의 거래에서 나온 설명을 검증된 인과관계로 취급하지 않음 |

특히 walk-forward-validation의 DSR 예제는 연율화 Sharpe 입력과 표본 수의 시간 단위, kurtosis 설명·기본값의 일관성을 검토해야 한다. num_trials=1에서는 norm.ppf(0)이 등장한다. 또한 CPCV는 조합에 따라 테스트 블록 뒤의 관측치를 훈련에 포함할 수 있으므로, 미래 자료를 사용할 수 없는 실제 walk-forward 배포 평가와 구분해야 한다. 해당 예제를 운영 게이트에 복사하지 않고 원식 대조와 별도 검증을 거친 구현을 사용한다.

**스택 채택 결론.** MVP는 한투 API 계약·.kis.yaml·Lean 어댑터의 재사용 가능성을 먼저 검증하고, 얇은 연구 오케스트레이터와 자체 승격 정책을 추가한다. RD-Agent/Qlib는 연구 흐름의 우선 레퍼런스로 사용한다. 여러 백테스트 엔진·에이전트 프레임워크를 동시에 결합하지 않는다. 분봉·호가 매매가 목표로 바뀌면 NautilusTrader 같은 이벤트 엔진의 도입 가치를 다시 판단한다.

**조사 범위의 남은 항목.** 공식 포털 https://apiportal.koreainvestment.com/ 은 공식 저장소에서 연결한 원천 문서 위치로 확인했으나 포털 FAQ·최신 약관·호출 한도 본문은 이번에 직접 열람하지 않았다. 계정 자격증명 없이 공개 GitHub 자료만 조사했으며 모의/실전 API 응답, 시세 품질, 과거 데이터 범위, 체결 모델, 수익률은 미검증이다. 구현 전 아래 사항을 확정한다.

- 계정·상품·환경별 REST/WS 한도, 지원 API·TR ID, KRX/NXT/SOR 라우팅, 주문·취소·조회 계약.
- 일봉·분봉의 과거 조회 범위와 수정주가 기준, 상장폐지·과거 구성종목·공시의 시점 데이터 공급원.
- 상품·거래일·계좌별 수수료·세금, 데이터 저장·활용 범위.
- 재사용할 코드의 경로별 라이선스. RD-Agent/Qlib의 LICENSE는 MIT, TradingAgents는 Apache-2.0을 직접 확인했다. 한투 저장소의 개별 컴포넌트 라이선스를 저장소 전체로 확대 해석하지 않는다.
