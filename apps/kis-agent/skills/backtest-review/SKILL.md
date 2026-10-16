---
name: backtest-review
description: 백테스트와 최종 평가 보고서의 시점·비용·선택 편향을 검토한다.
---

backtest.json과 research.json의 snapshot_id, strategy_version, evaluation_id/run_id, 구간, 비용·리스크 설정을 대조한다. 종가 신호가 다음 거래 세션에서 실행되는지, 개발 구간과 최종 구간이 분리됐는지, 평가 구간 소비 기록이 있는지 확인한다.

수익·낙폭·회전율·표본 수·비용 스트레스·불확실성을 함께 설명한다. JSON의 수치를 LLM 추정으로 바꾸지 않는다. KEEP_CHAMPION이나 판단 보류는 정상 결과다.

현재 엔진은 익일 시가에 고정 슬리피지와 전일 거래량 한도를 적용하는 선별용 모델이다. 전량 체결 가정·기업행위 미지원·예시 비용·같은 프로세스 안의 논리적 평가 경계를 명시한다. ELIGIBLE_FOR_PAPER_REVIEW는 실거래 승인이나 수익성 확정을 뜻하지 않는다.
