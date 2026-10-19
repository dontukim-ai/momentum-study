# 연구 DB 백업과 소비 witness (OPS-02a)

이 절차는 합성 회귀로 검증한 복구 기능이다. 실데이터 운용 검증, 자동 스케줄러 배포, 원본 데이터·환경 전체 보관은 완료하지 않았다. 연구 champion 갱신은 주문을 만들지 않는다.

예를 들어 매일 연구 작업 종료 후, 그리고 코드·정책·환경 전환 전에 백업한다. 실행 환경에서 다음 명령을 사용한다.

```sh
uv run python -m momentum.agent.cli backup --db /research/research.sqlite3 --to /backups/research
uv run python -m momentum.agent.cli witness-status --db /research/research.sqlite3
```

SQLite backup API가 WAL의 커밋된 내용까지 일관 사본으로 만든다. 원본은 이후에도 쓸 수 있다. 백업 파일과 같은 stem의 manifest JSON을 함께 보관한다. manifest는 SHA256·바이트 수·UTC 시각·build ID·agent 스키마 버전·agent 테이블별 행 수·witness 경로와 마지막 seq/hash를 담는다. 키·계좌 정보는 manifest에 넣지 않는다. **DB 사본 자체는 기존 DB 내용을 포함하므로 접근을 제한한다.** 같은 시각의 백업 이름은 덮어쓰지 않는다.

파일 DB의 기본 witness는 `<DB 디렉터리>/witness/<DB 파일명>.witness.jsonl`이다. 메모리 DB는 기본 비활성이다. 프로그램에서 `WitnessLog(path)`를 주입하면 별도 경로를 쓸 수 있다. DB와 다른 디스크/폴더에 witness 사본을 권장한다. 백업 후에도 witness는 계속 누적하므로, DB 복원 시점에 맞춰 과거 witness로 되돌리지 않는다. 읽기 중인 로그 복사는 마지막 개행까지 완전한지 검증한다. 임의로 잘린 끝 줄을 제거하여 복구하지 않는다.

복원 절차:

1. 연구 worker와 DB에 접근하는 모든 프로세스를 중지한다. 열린 SQLite 연결이 있는 상태에서 파일을 교체하지 않는다.
2. DB·manifest·최신 witness를 확보한다. 원본 자료, 정책, 코드 build와 의존성 환경도 별도 보관한다. OPS-02a 백업은 이들 전체를 패키징하지 않는다.
3. 아래 명령을 실행한다. 대상이 존재하면 `--replace`가 필요하다. 기존 DB와 WAL/SHM은 `<DB 이름>.replaced-<UTC시각>` 및 그 `-wal`/`-shm` 이름으로 이동하여 보존한다. witness는 이동·교체하지 않는다.
4. 출력의 `missing`이 있으면 운영자 이름으로 reconcile하고 다시 status를 확인한다.
5. `missing=[]`를 확인한 뒤 worker를 재개한다. 보존 파일과 witness 사본은 복구 증거로 유지한다.

```sh
uv run python -m momentum.agent.cli restore --from /backups/research/research-TIMESTAMP.sqlite3 --db /research/research.sqlite3 --replace
uv run python -m momentum.agent.cli reconcile-witness --db /research/research.sqlite3 --operator OPERATOR
uv run python -m momentum.agent.cli witness-status --db /research/research.sqlite3
```

reconcile은 사라진 최종·shadow 구간을 배타적으로 격리하고 관찰 노출을 원래 stream에 합친다. 사라진 commitment는 holdout을 막고 기존 예산 원장에 reserve/consume을 각각 한 번 기록한다. 추가 전용 quarantine에는 witness seq/hash·payload·운영자·시각이 남는다. 한 트랜잭션이며 재실행은 멱등이다. 잃어버린 평가·사이클·수익률은 복원하거나 만들어내지 않는다. COMMIT 실패로 witness에만 남은 예약도 같은 보수적 격리 대상이다. 연구 자체가 없는 백업은 정책/예산을 추정하지 않고 reconcile을 거부한다. 이 경우 해당 연구 정책·예산이 보존된 복구 자료가 필요하다.

witness를 삭제·편집하지 않는다. 대체 DB를 witness 없이 사용하지 않는다. witness와 DB를 함께 과거로 되돌리지 않는다. 손상된 해시 사슬이나 잘린 끝 줄을 추정하여 고치지 않는다. 해시 사슬은 변경·순서 오류 검출용이며, 로그 전체 삭제·유효한 접미부 제거·악의적 전체 재해시에 대한 외부 서명/앵커는 아니다. 별도 사본의 보관과 운영 접근 제어가 필요하다. witness 도입 이전 소비는 자동 역채움하지 않으므로 기존 DB도 계속 보존한다.

## 연구 worker 운영(OPS-01a)

`momentum-agent run [--watch]`은 실행하는 동안 연구별 **단일 worker lease**를 연구 DB의 `agent_worker_leases`에 잡고 `<output>/worker.json`을 갱신한다.

- **lease:** 같은 연구에 살아 있는 worker가 있으면 두 번째 실행은 "Another worker holds this study"로 거부된다. heartbeat가 `max(3 × interval, 120)`초 동안 없으면 죽은 것으로 보고 새 worker가 넘겨받는다(`took_over_from`에 기록). 넘겨받힌 쪽은 다음 heartbeat에서 스스로 멈춘다. PID 확인은 쓰지 않는다(Windows에서 `os.kill(pid, 0)`은 Ctrl+C 신호를 보낸다).
- **상태:** `STARTING` → `RUNNING`/`WAITING_FOR_DATA`(대기 이유: 다음 최종 구간까지 필요한 세션 수) → `STOPPED`(tick 수 도달)·`FINISHED`(예산 소진·관찰 완결)·`STOPPED_BY_OPERATOR`(Ctrl+C)·`STOPPED_ERROR`·`NEEDS_OPERATOR`.
- **재시도 없음:** 어떤 오류도 자동 재시도하지 않는다. 중단된 최종 평가(`Cycle interrupted`)는 `NEEDS_OPERATOR`로 남기고 `operator_action`에 `recover` 또는 `abandon` 절차를 적는다. 그 밖의 오류는 `STOPPED_ERROR`와 `last_error`로 남는다. 다시 시작은 사람이 원인을 본 뒤 하는 의도적 행동이다.
- **확인:** `momentum-agent worker-status --db DB --output DIR --study NAME`은 lease·상태 파일·heartbeat 경과 초·stale 여부를 출력한다. 주문은 항상 0건이다.
