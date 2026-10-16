---
name: execution-reconcile
description: 로컬 모의 주문·체결·현금·수량의 영속 상태와 대사 결과를 점검한다.
---

uv run kis-agent status --db <path> --account <id>로 상태를 확인한다. 로컬 모의 브로커 대사는 uv run kis-agent reconcile --db <path> --account <id>로 수행한다. 현재 이 명령은 실제 한투 계좌에 연결하지 않는다.

KIS_PAPER에 바인딩된 계좌는 uv run kis-agent kis-paper reconcile --db <path> --account <alias>를 사용한다. 잘못된 backend를 대신 연결하지 않는다. cash_basis의 예상 정산 현금과 실제 매수 가능 현금을 구분한다. 실제 수수료가 빠진 원장이나 누적 관측의 역행·현금 단독 수정은 보류한다.

UNKNOWN/SUBMITTING을 미접수로 해석하거나 즉시 재전송하지 않는다. 부분 체결과 취소 잔량을 구별하고 CANCEL_PENDING 중의 추가 체결을 확인한다. 반복 통보는 fill_id로 중복 반영을 방지한다.

KIS는 로컬 client key를 전달하지 않으므로 후보가 하나여도 자동 매칭하지 않는다. 운영자가 조회에서 확인한 주문일·조직번호·주문번호로만 kis-paper adopt를 수행한다. 실제 취소 가능 계약은 아직 미확인이라 취소 POST가 기본 차단된 상태다.

출력은 상태·미확정 주문·현금/보유 차이·halt_reason·복구 근거다. 계좌 차이나 대사 오류로 정지한 상태를 DB 직접 편집으로 해제하지 않는다. 새 주문 제안과 실제 주문 집행은 이 스킬의 대사 결과에서 자동으로 발생하지 않는다.
