# 01 자기 가격 시계열·신호처리 (요약)
- TSMOM(MOP 2012, Hurst-Ooi-Pedersen 2017 100년): 분산 선물 Sharpe 0.5~1.0, 2010년대 부진, 2022 반등; 빠른 신호는 2010년 이후 0 근처(arXiv 2607.01550 프리프린트). Huang et al. 2020 JFE: 자산별 예측력 약함, 수익은 평균수익 차이일 수 있음. 크립토 TSMOM 혼재(CME 선물 도입 후 감쇠 주장); Shen-Urquhart-Wang 2022 BTC 장중 TSMOM.
- 평균회귀: 지수 일간 자기상관 0 근처; 개별 주간 반전은 미시구조·비용에 소멸; Nagel 2012 유동성 공급 보상.
- 기술적 규칙: BLL 1992 → STW 1999 Reality Check 표본외 소멸 → Bajgrowicz-Scaillet 2012 FDR 1897~2011 사전선택 불가·저비용에도 상쇄. Hudson-Urquhart 2021 크립토 14,919 규칙 유의하나 BTC 표본외 무예측. Detzel et al. 2021 가격/MA가 BTC 예측(가치평가 어려운 자산).
- 캘린더: 월말·월초, 할로윈 비교적 지속(검정력 낮음); Lou-Polk-Skouras 2019 모멘텀은 야간; Heston-Korajczyk-Sadka 2010 30분 주기 지속(체결 타이밍 용도); Gao et al 2018 장중 모멘텀; 크립토 시간대 효과 불안정(Baur 2019).
- 변동성: GARCH/HAR 예측 강함. Moreira-Muir 2017 → Cederburg 2020 표본외 실패, Barroso-Detzel 2021 비용 후 시장 포트폴리오만 유지 → 위험관리 도구.
- 신호처리: 대부분 누수·스누핑(Physica A 등). 예외 Faria-Verona 2018 웨이블릿으로 예측변수 분해 OOS R² 2.6%. 허스트: 수익률 장기기억 기각(Lo 1991), 변동성 장기기억 강함.
- 가격-거래량: CGW 1993 반전, Llorente 2002 정보비대칭에 따라 지속/반전, GKM 2001 고거래량 프리미엄(1개월).
- 견고한 축: 분산된 느린 추세 + 변동성 스케일링(위험관리).
