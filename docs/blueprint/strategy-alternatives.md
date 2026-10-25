# 전략 대안 조사 (2026-10-25)

작성 2026-10-25. 질문: 지금까지 실험한 후보 공간(가격만 쓰는 20~60일 모멘텀 변형 3종)이 너무 좁은가. 근거는 공개 논문 초록과 2차 요약이다. 원문 전체는 읽지 않았으므로 효과 크기와 강건성은 미확인이다. 이 문서는 후보 목록이며 사전 등록이 아니다.

## 1. 현재 후보 공간과 제약

- 순위 방식은 `momentum`, `risk_adjusted`(모멘텀 ÷ 변동성), `trend_stability`(모멘텀 × 상승일 비율) 세 가지이고, 모두 같은 과거 수익률을 쓴다. kis-agent `RANKINGS` 허용 목록과 `lookback` 2~60 제한(`selection.py`)으로 고정돼 있다. 새 신호를 넣으려면 kis-agent를 바꿔야 한다(사용자 허가 필요, 전략 버전 ID가 모두 바뀜).
- 입력: FSC 시세(2020~2024-06, 약 1,100세션)의 가격·거래량·상장주식수·시가총액(진단용), OpenDART 기업행동. **재무제표는 수집하지 않았다.**
- 표본이 약 4.5년이라 어떤 전략이든 검증 구간 8개의 변동이 크다. 12개월 기간 신호는 첫 1년을 이력으로 쓰므로 검증 구간이 더 줄어든다.

## 2. 한국 시장 근거

- **개별 종목 모멘텀보다 반전이 우세하다.** Kang·Ryu·Webb(2025, Investment Analysts Journal 54(4), 1983~2023): 개별 종목 모멘텀 포트폴리오는 반전 효과를 보였고 산업 모멘텀은 유의하지 않았다. 일부 하위 기간에만 모멘텀이 나타났다. 위험 요인과 산업 모멘텀을 통제해도 반전이 남았다.
- Sim·Kim(2021, Journal of Derivatives and Quantitative Studies): 한국 모멘텀 수익의 기간 구조는 주로 직전 2개월 수익률 반전이 이어진 결과다. 지금 후보(20~60일)가 바로 이 구간이다.
- KAIST 연구: 전통 모멘텀은 부진하고 장기 반전을 겪었으나 고유(잔차)·순위·부호 모멘텀은 안정적이었다(현저성 효과로 설명).
- **저변동성·저베타 이상현상**이 한국에서도 보고된다(KAIST 학위논문, 울산대 연구: 복권 성향 통제 뒤에도 저베타 이상현상 유의). 대부분 학위논문·작업논문이다.
- **가치·수익성:** KOSPI 1994~2014 매출총이익 수익성 프리미엄(가치와 음의 상관, 결합 시 개선). 1987~2023 연구는 최근 장부가/시가 효과가 미국과 달리 강해졌고, 이익잉여금/시가가 더 나은 예측 변수라고 보고했다.
- 해외 일반 근거: 가치와 모멘텀은 음의 상관이라 결합이 유리하다(Asness·Moskowitz·Pedersen 2013; 일본은 모멘텀이 유의하지 않음). 모멘텀 붕괴는 하락장 뒤 반등기에 온다. 변동성 조절(Barroso·Santa-Clara 2015)과 동적 조절(Daniel·Moskowitz 2016)이 붕괴를 줄이지만, 실시간 성과는 줄어든다는 반론도 있다(Cederburg 외 2020). 잔차 모멘텀(Blitz·Huij·Martens 2011)은 낙폭이 얕다. 52주 신고가 근접도(George·Hwang 2004)는 장기 반전 없이 모멘텀 수익을 설명한다.

이 근거로 보면 v2~v3의 손실(단기 급등주 반전, §v3-7)은 우연이라기보다 한국 시장의 알려진 성질과 맞는다.

## 3. 대안 후보

| # | 후보 | 아이디어 | 한국 근거 | 필요한 변경 | 데이터 |
| --- | --- | --- | --- | --- | --- |
| A1 | 단기 반전 | 최근 20일 하락 상위(패자)를 산다 | 강함(반전 우세, 2개월 반전) | kis-agent 순위 추가 | 있음 |
| A2 | 저변동성 | 60일 변동성 하위를 산다 | 중간(학위논문 다수) | kis-agent 순위 추가 | 있음 |
| A3 | 시장 추세 필터 | KOSPI 200 ETF가 이동평균 아래면 현금(듀얼·절대 모멘텀, Faber·Antonacci) | 해외 근거, 한국 미확인 | 정책·평가기 변경 | 있음(069500) |
| A4 | 잔차 모멘텀 | 시장 수익을 뺀 수익률로 순위 | KAIST 연구(고유 모멘텀 안정) | kis-agent 순위 추가 | 있음 |
| A5 | 52주 신고가 근접도·12-1 모멘텀 | 장기 가격 신호 | 한국 약함(12-1), 미확인(52주) | lookback 상한 해제 | 있음, 표본 1년 손실 |
| B1 | 가치(PBR, 이익잉여금/시가) | 싼 종목 | 최근 강해짐 | 새 데이터·순위 | **없음**: OpenDART 재무제표 시점 기준 수집 필요 |
| B2 | 수익성·퀄리티 | 매출총이익/자산 | 있음(KOSPI 1994~2014) | 새 데이터·순위 | 없음(B1과 같음) |
| B3 | 가치+모멘텀 결합 | 음의 상관을 이용한 분산 | 해외 강함 | B1 이후 | 없음 |

## 4. 권장 순서 (제안, 미승인)

1. v4(10종목) 결과를 사전 등록 기준으로 판정한다.
2. 가격만으로 되는 A1(단기 반전)과 A2(저변동성)를 순위 방식으로 추가하는 kis-agent 변경을 사용자 허가로 진행한다. 한국 근거가 가장 직접적이고 데이터가 이미 있다. 같은 연구 안에서 모멘텀과 경쟁하는 후보로 두면 기존 승격 절차를 그대로 쓴다.
3. 2022년 −49%와 같은 하락장 손실에는 A3(시장 추세 필터)를 따로 검토한다.
4. B1·B2는 재무제표 시점 기준 수집(공시 접수 시각 = `available_at`)이 먼저라 별도 데이터 과제로 둔다.

## 5. 미국 시장 기술적 분석 연구와 스킬 (2026-10-25 추가)

질문: 미국 시장 기반 기술적 분석 이론·연구·설치된 스킬 중 후보로 넣을 만한 것이 있는가. 근거는 초록과 2차 요약이다(원문 미확인). 평가 기준은 (a) 입력(일별 OHLCV, 2020~2024)으로 계산 가능한가, (b) 데이터 스누핑 뒤에도 근거가 남는가, (c) 한국 근거가 있는가.

### 5.1 연구

| # | 후보 | 근거 요약 | 판정 |
| --- | --- | --- | --- |
| T1 | 일간 최대 수익률(MAX) 회피 | Bali·Cakici·Whitelaw(2011): 직전 1개월 최대 일간 수익률이 높은 종목이 이후 부진(상하위 십분위 차 월 1% 이상). 한국에서도 확인되나 저심리 국면·고평가 종목에서만 유의하다는 연구가 있다. 과잉반응 해석도 있다(Gorman 외 2022). | **후보.** v3b 상위 종목은 상한가 연속 종목이었다. 모멘텀 순위에 MAX 상한 필터를 더하면 그 이익과 −58% 같은 손실이 함께 빠지는지 시험할 가치가 있다. |
| T2 | 변동성 그룹 이동평균 타이밍 | Han·Yang·Zhou(2013, JFQA): 변동성 십분위 포트폴리오에 이동평균(예: 10일) 타이밍을 적용하면 고변동성 그룹에서 모멘텀보다 큰 초과수익. 복제 연구는 일부 기간 길이에서만 유의. | **후보(A3과 결합).** 시장 전체가 아니라 고변동성 그룹의 지수가 이동평균 아래면 그 그룹 매수를 멈추는 형태. |
| T3 | 야간·장중 수익 분해 | Lou·Polk·Skouras(2019, JFE): 모멘텀 이익은 야간(전일 종가→시가)에, 다른 이상현상 이익은 장중에 쌓이고 두 구간이 서로 반대로 움직인다. 대형·고가 종목에서 강하다. | **후보(탐색).** 시가·종가가 있어 계산 가능. 우리 체결은 시가라 야간 수익을 놓치는 구조인지 먼저 진단할 가치가 있다. |
| T4 | 고거래량 프리미엄 | Gervais·Kaniel·Mingelgrin(2001): 거래량이 비정상적으로 높았던 종목이 다음 달 상승(관심 효과). 핀란드·중국 복제는 실패. | 보류. 해외 복제가 엇갈리고 우리 모멘텀 급등주와 겹친다. |
| T5 | 52주 신고가 근접도 | George·Hwang(2004), §3 A5와 같음. | A5로 유지(lookback 상한 해제 필요). |
| T6 | 이동평균·범위 돌파 규칙 | Brock·Lakonishok·LeBaron(1992) 다우지수 초과수익. Sullivan·Timmermann·White(1999)의 데이터 스누핑 보정 뒤 이후 10년 표본에서 사라짐. 개관(Park·Irwin)도 1990년대 초 이후 주식 일간 규칙 수익이 크게 줄었다고 정리. | 종목 순위 신호로는 제외. 시장 필터(A3)로만 검토. |
| T7 | 차트 패턴(머리어깨형 등) | Lo·Mamaysky·Wang(2000): 일부 패턴이 정보를 더하지만 수익성은 약하다. 재검증 결과는 엇갈림. | 제외. 구현 복잡도 대비 근거가 약하다. |
| T8 | RSI·볼린저·MACD 단일 지표 | 표본 외·비용 반영 학술 근거를 찾지 못했다. 업계 백테스트는 무작위 수준. RSI 과매도 매수는 사실상 단기 반전(A1)과 같다. | 제외(A1로 대체). |
| T9 | 시계열 모멘텀 | Moskowitz·Ooi·Pedersen(2012)은 선물 대상. 개별 주식에서는 횡단면 모멘텀과 순노출 차이뿐이라는 반론(Goyal·Jegadeesh 2017). | 개별 신호로는 제외. 시장 필터(A3)에 포함. |
| T10 | 같은 달 계절성 | Heston·Sadka(2008): 같은 달 상대 성과가 해마다 반복. 신흥시장에서는 경제적 유의성이 약하다는 연구가 있다. | 제외. 4.5년 표본으로는 연간 지연 신호를 만들 수 없다. |

### 5.2 설치된 스킬

| 스킬 | 내용 | 판정 |
| --- | --- | --- |
| us-swing-screener | 3개월 −25% 이상 하락 + 1주 상승(반등 초입) + 목표가 상승여력 + 재무 6항목 | 가격 부분(장기 패자 중 단기 반등 확인)은 A1(단기 반전)의 변형으로 넣을 수 있다. 목표가·재무는 데이터가 없다. |
| kronos-research | 사전학습 캔들 모델(Kronos)로 종목 순위, RankIC 검증 | 보류. 외부 모델 가중치·torch 의존과 네트워크가 필요해 규칙 코어 밖이다. 별도 진단 도구로 RankIC만 비교할 수는 있다. |
| tenbagger-screener | 메가트렌드·고성장 재무 채점 | 제외. 시점 기준 백테스트가 불가능한 서술형 평가다. |

### 5.3 데이터 스누핑 주의

후보를 많이 넣을수록 우연히 좋은 규칙을 고를 확률이 커진다(Sullivan·Timmermann·White). 연구당 새 순위는 2~3개로 제한하고, 사전 등록·검증 예산·독립 검증 구간 구조를 그대로 쓴다.

### 5.4 갱신된 권장 순서 (제안, 미승인)

1. v4 판정.
2. 첫 신호 확장 연구: A1 단기 반전, A2 저변동성, T1 MAX 필터(모멘텀 + 최근 1개월 최대 일간 수익률 상한). 모두 기존 입력으로 계산된다. kis-agent 순위 추가 승인이 필요하다.
3. 하락장 대책: A3 시장 추세 필터, T2 변동성 그룹 타이밍.
4. 진단: T3 야간·장중 분해(읽기 전용 분석으로 먼저).
5. 데이터 과제: B1·B2 재무제표.

## 6. 출처

- Kang·Ryu·Webb(2025) 초록: https://www.nisc.co.za/products/abstracts/38749/momentum-and-reversal-effects-in-the-korean-stock-market
- Sim·Kim(2021): https://emerald.com/insight/content/doi/10.1108/JDQS-02-2021-0005/full/html
- KAIST 모멘텀·현저성: https://koasas.kaist.ac.kr/handle/10203/297600
- 저변동성: https://dspace.kaist.ac.kr/handle/10203/220708?mode=full , https://oak.ulsan.ac.kr/handle/2021.oak/14813
- 한국 수익성 프리미엄: https://koreascience.or.kr/article/JAKO201516863230113.do
- Value and Momentum Everywhere: https://pages.stern.nyu.edu/~lpederse/papers/ValMomEverywhere.pdf
- 모멘텀 붕괴·변동성 조절: https://alphaarchitect.com/risk-of-momentum-crashes/ , https://pureadmin.qub.ac.uk/ws/files/139425062/volatility_scaling_in_momentum_Dec_17.pdf
- 잔차 모멘텀: https://www.robeco.com/en-uk/insights/2023/02/quant-chart-taming-momentum-crashes
- 52주 신고가: https://alphaarchitect.com/the-secret-to-momentum-is-the-52-week-high/
- 단기 반전과 건너뛰는 달: https://alphaarchitect.com/quantitative-momentum-research-short-term-return-reversal/
- 추세 추종·듀얼 모멘텀: https://alphaarchitect.com/asset-allocation-horserace-robust-asset-allocation-raa-vs-dual-momentum/ , https://quantpedia.com/active-dual-momentum-gtaa-strategy/
- MAX 효과: https://alphaarchitect.com/hot-off-the-jfe-press-maxing-out-your-returns/ , 한국 https://dspace.kaist.ac.kr/handle/10203/322414?mode=full , https://koasas.kaist.ac.kr/handle/10203/265636 , 과잉반응 해석 https://cfr.ivo-welch.info/published/papers/gorman2021could.pdf
- 변동성 그룹 이동평균: https://ideas.repec.org/a/cup/jfinqa/v48y2013i05p1433-1461_00.html , https://cxoadvisory.com/?p=7901
- 야간·장중: https://personal.lse.ac.uk/polk/research/TugOfWar.pdf
- 고거래량 프리미엄: https://rodneywhitecenter.wharton.upenn.edu/wp-content/uploads/2014/04/9901.pdf , https://aaltodoc.aalto.fi/server/api/core/bitstreams/2d5b4975-b572-424c-baa4-679597a6704f/content
- 기술적 규칙과 데이터 스누핑: https://researchonline.lse.ac.uk/id/eprint/119144 , https://ideas.repec.org/a/bla/jecsur/v21y2007i4p786-826.html
- 차트 패턴: https://www.nber.org/papers/w7613
- 단일 지표 백테스트: https://elite.finviz.com/help/technical-analysis/backtests_report.ashx
- 시계열 모멘텀: https://papers.ssrn.com/abstract=2089463
- 계절성: https://www.nber.org/papers/w20815.pdf
