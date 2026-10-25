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

## 5. 출처

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
