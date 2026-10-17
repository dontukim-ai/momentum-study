---
name: strategy-research
description: 제한된 모멘텀·순위 후보를 제안하고 시점 기반 종목 선정과 독립 연구 평가에 연결한다.
---

입력은 기존 StrategySpec, 개발 구간의 실험·실패 기록, 후보 예산이다. 가설·변경할 축·예상 효과·반증 조건을 먼저 적는다. 현재 허용 변경은 2~60 거래일의 정수 lookback이며 기본 후보 예산은 8개다.

v0.5의 logic 경로는 RankedStrategy의 lookback과 사전 허용된 momentum/risk_adjusted/trend_stability 순위만 바꿀 수 있다. 선정 정책·위험·비용·평가 기준은 study 초기화 때 고정한다. logic run은 통과한 연구 champion만 자동 갱신하며 주문·기존 shadow release를 변경하지 않는다. 원래 research 명령의 lookback-only 경계는 유지한다. 데이터 계약·고정 준비 기간·전진 평가·복구 규칙은 docs/08-selection-and-improvement.md를 따른다.

실험은 uv run kis-agent research --csv <path> --db <persistent-db> --lookbacks 5,10,20 --output <dir>로 실행한다. 같은 연구 이력 DB를 유지한다. 평가 구간을 다시 쓰기 위해 새 DB를 만들거나 위험 비중·비용·합격 기준을 후보가 바꾸게 하지 않는다.

출력은 가설, 후보 파라미터, 연구 run_id, 전체 시도·실패 수, 판정과 제한이다. 기본 생성기는 BoundedLookbacks이고, v0.2는 OpenAI Responses 구조화 제안 또는 --proposal-file을 지원한다. 먼저 개발 구간의 development_context를 만들고, 후보 파일의 context_hash를 검증한다. 실제 API에는 OPENAI_MODEL/--model을 명시한다.

연구는 --proposal-file <file> 또는 --llm --model <model>로 제안을 연결할 수 있다. 기록 응답의 provider와 network_verified를 보존하고 실제 LLM 호출로 표현하지 않는다. 실패한 API 요청도 예산에 포함되며 자동 재시도하지 않는다. 임의 코드 자기 수정은 구현되지 않았다. 후보 제안은 운영 버전 변경을 뜻하지 않는다.
