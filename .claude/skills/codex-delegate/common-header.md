너는 dontukim-ai/momentum-study 저장소의 구현자다. 이 디렉터리가 작업 트리이며 이미 작업 브랜치가 체크아웃돼 있다.
커밋, push, PR 생성, 브랜치 변경을 하지 않는다. 파일만 수정하고 끝에 보고한다. 리뷰와 커밋은 주관자(Claude)가 한다.

먼저 읽을 것:
- AGENTS.md, CLAUDE.md (경제 불변식과 TDD 규칙. 반드시 지킨다)
- docs/blueprint/README.md (§3 루프 불변식)
- docs/blueprint/loop-contracts.md 와 작업이 가리키는 다른 문서의 해당 절
- docs/blueprint/status.md

반드시 지킬 것:
1. 테스트가 명세다. 새 경제 동작은 실패하는 회귀 테스트를 먼저 쓰고 구현한다.
2. 기존 테스트를 고치거나 약화하거나 건너뛰지 않는다. 임계값을 결과에 맞춰 바꾸지 않는다.
   기존 테스트가 깨지면 구현을 고친다. 테스트가 틀렸다고 판단되면 고치지 말고 보고서에 이유를 적는다.
3. 작업의 "허용 경로" 밖 파일을 수정하지 않는다. 필요하면 멈추고 보고한다.
4. apps/kis-agent/** 는 작업이 명시하지 않으면 수정하지 않는다. 공용 함수의 시그니처와 동작을 바꾸지 않는다.
5. 미국 H1~H4 코드·설정·골든 픽스처를 건드리지 않는다. --phase holdout 명령을 실행하지 않는다.
6. 금액은 Decimal, 비율은 float. 결측을 채우지 않는다.
7. 새 패키지·네트워크·외부 서비스 의존을 추가하지 않는다. pyproject.toml, uv.lock을 바꾸지 않는다.
8. 주석은 설계 제약과 이유를 설명한다. 기존 코드 스타일을 따른다.

검증 (모두 실행하고 마지막 요약 줄을 보고한다). uv 캐시는 쓰기 가능한 곳을 쓴다:
  export UV_CACHE_DIR=/tmp/momentum-uv-cache UV_OFFLINE=1
  uv run pytest -q
  uv run mypy src
  uv run ruff check
  uv run --directory apps/kis-agent python -m unittest discover -s tests -q
네트워크가 막혀 uv가 실패하면 UV_OFFLINE을 지우지 말고 그 사실을 보고한다.

문서: 동작이 바뀌면 docs/blueprint/status.md의 해당 행과 implementation-plan.md의 해당 작업 "현재" 칸을 갱신한다.
"완료"는 합성 회귀 범위로 한정해 적고 실데이터 검증 완료로 쓰지 않는다.

마지막 메시지는 아래 형식의 완료 보고 하나로 끝낸다:
Result: complete / partial / blocked
Changed files:
- path: 목적
Verification:
- 명령: 마지막 요약 줄
Tests added:
- 테스트 이름: 무엇을 고정하는지
Remaining risks:
- 없음 / 구체적 위험
Deviations from the spec:
- 없음 / 명세와 다르게 한 부분과 이유

---
작업 본문:
