---
name: codex-delegate
description: 작업 하나를 Codex CLI(`codex exec`)에 맡겨 별도 worktree에서 구현시키고, 허용 범위·diff·검증을 직접 확인한 뒤 커밋·push·PR까지 한다. "Codex에 맡겨", "위임", "codex로 구현", 블루프린트 작업 ID 구현 요청에 사용한다.
---

# Codex 위임

Claude가 주관자다. Codex는 구현만 하고, 커밋·push·PR·수용 판단은 Claude가 한다.

## 0. 사전 확인 (세션마다 한 번)

```bash
codex --version                                   # 없으면: npm install -g @openai/codex
curl -s -o /dev/null -w "%{http_code}\n" https://api.openai.com/v1/models   # 200이어야 함
```

- 인증은 클라우드 환경의 네트워크 시크릿(`api.openai.com`)이 요청마다 키를 붙인다. Codex에는 가짜 키로 로그인만 해 둔다: `echo placeholder | codex login --with-api-key`.
- 401이면 시크릿 값이 틀린 것이다(예: 앞의 `s`가 빠진 `k-proj-…`). 사람에게 시크릿 수정을 요청하고 멈춘다. 키를 채팅으로 받지 않는다.
- 403/연결 실패면 네트워크 허용 도메인에 `api.openai.com`이 없는 것이다.

## 1. 작업 파일 작성

`.codex-tasks/<ID>.md`(Git에 넣지 않음)에 작업 본문을 쓴다. 형식은 `task-template.md`를 따른다. 반드시 포함:

- 작업 ID와 블루프린트 명세 위치(파일·절)
- 배경: 현재 코드가 어떻게 동작하는지, 실제 함수·파일 이름으로
- 구현할 것: 번호 목록, 필드 이름까지 구체적으로
- **허용 경로**: 수정해도 되는 파일·디렉터리 목록
- 먼저 쓸 실패 회귀: 손계산 숫자가 들어간 시나리오
- 문서 갱신 범위

작업 하나는 한 번의 리뷰로 읽을 수 있는 크기로 자른다(대략 diff 500줄 이하).

## 2. 실행

```bash
.claude/skills/codex-delegate/run.sh <ID> <기준 브랜치> <작업 브랜치>
```

스크립트가 하는 일:
1. `../momentum-study-wt/<ID>`에 기준 브랜치로 worktree를 만들고 작업 브랜치를 판다.
2. `common-header.md` + 작업 파일을 프롬프트로 `codex exec --sandbox workspace-write`를 실행한다.
3. Codex 마지막 메시지를 `.codex-tasks/<ID>.report.md`, 전체 로그를 `.codex-tasks/<ID>.log`에 남긴다.
4. 변경 파일 목록과, 작업 파일의 `허용 경로` 밖 변경 목록을 출력한다.

실행은 수 분~수십 분 걸린다. Bash `run_in_background`로 돌리고 완료 알림을 기다린다.

## 3. 수용 검사 (Claude가 직접)

worktree에서 순서대로:

1. **범위:** 허용 경로 밖 변경은 `git checkout -- <path>`로 되돌린다. `apps/kis-agent/**`, 미국 H1~H4 코드·`tests/fixtures/`·골든, 기존 테스트의 단언, `pyproject.toml`/`uv.lock` 변경은 작업 파일이 명시하지 않았다면 항상 되돌린다.
2. **diff 전체를 읽는다.** 특히: 기존 테스트 약화, 임계값 변경, 결측 채우기, 다음 시가로 수량 계산, 엔진 복제(CLAUDE.md 절대 금지 1~7).
3. **검증을 직접 실행**하고 마지막 요약 줄만 본다:
   ```bash
   export UV_CACHE_DIR=/tmp/momentum-uv-cache
   uv run pytest -q
   uv run mypy src
   uv run ruff check
   uv run --directory apps/kis-agent python -m unittest discover -s tests -q
   ```
4. Codex 보고서의 "Deviations" 항목을 코드에서 확인한다.

## 4. 실패 처리

- 검증 실패·범위 위반·명세 불일치가 있으면, 무엇이 틀렸는지와 기대 동작을 적은 수정 지시로 다시 실행한다:
  ```bash
  .claude/skills/codex-delegate/run.sh <ID> --resume "<수정 지시>"
  ```
- **같은 실패로 2회 재시도해도 안 되면 멈춘다.** 명세를 고치거나 Claude가 직접 고치고, 그 사실을 PR 본문에 적는다.
- 사소한 수정(오타, import 정리)은 Claude가 직접 고쳐도 된다. 경제 계산 변경은 Codex에 돌려보내거나 실패 회귀부터 쓴다.

## 5. 마무리

1. 커밋은 Claude가 한다. 메시지는 한국어, 작업 ID 포함, 세션 지침의 attribution 줄을 붙인다.
2. `git push -u origin <작업 브랜치>` 후 draft PR을 만든다. 기준 브랜치가 아직 머지 전인 PR 브랜치면 그 브랜치를 base로 한다.
3. PR 본문: 작업 ID, 변경 요약, 직접 실행한 검증 결과(요약 줄), 범위 확인 결과, Codex가 명세와 다르게 한 부분과 판단, 사람이 확인할 항목. "구현: Codex / 리뷰·검증: Claude"를 적는다.
4. worktree는 PR이 머지되거나 닫힌 뒤 `git worktree remove`로 지운다.

## 하지 않는 것

- Codex에게 커밋·push·PR·네트워크 호출·holdout 실행을 시키지 않는다.
- 같은 worktree에서 Codex와 Claude가 동시에 파일을 쓰지 않는다.
- Codex 보고서의 "통과"를 검증 결과로 옮겨 적지 않는다. 직접 실행한 결과만 적는다.
