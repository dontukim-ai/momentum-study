# momentum-study

모멘텀 가설 검증과 주문 없는 자동 개선 연구를 관리하는 저장소다. 현재 두 Python 프로젝트를 별도 패키지·설정·검증 경로로 유지한다.

**현재 작업 범위:** 사용자 지시로 미국 H1~H4 개발·연구 실행을 보류한다. 기존 코드는 보존하며, 국내 주식·ETF 후보 선정과 자동 개선 연구에 집중한다.

| 경로 | 범위 | 실행 명령 |
| --- | --- | --- |
| 루트 `src/momentum` | 미국 보통주 H1~H4, 공통 체결·회계 엔진, 손계산 골든 테스트 | `uv run momentum --help` |
| [`apps/kis-agent`](apps/kis-agent/README.md) | 국내 주식·ETF의 시점 기반 후보 선정, 100만 원 연구 예산, 제한된 자동 개선, KIS 읽기 어댑터 | `uv run --directory apps/kis-agent kis-agent --help` |

루트의 H1~H4 가설·원화/달러·비용·최종 평가 정책과 KIS 연구 프로토타입의 정책을 혼용하지 않는다. KIS 연구 결과를 H1~H4의 투자 판정으로 사용하지 않는다. 실제 데이터·실제 서비스·수익성은 합성 테스트와 별도 검증 대상이다. 현재 개발 우선순위는 종목 선정·판단·자동 개선이며 실전 자동 주문은 구현하지 않았다.

## 자동 자기 개선 에이전트

`momentum-agent`는 미국 엔진의 원장과 한투의 시점 기반 후보 선정을 연결한다. 후보 생성 → 공통 체결·회계 평가 → 예약한 독립 검증 → 연구 champion 갱신·전진 관찰을 실행하며, 새 입력 파일을 감시해 반복할 수 있다. 현재 전략 연결기는 KRX/KRW·100만 원 이하 연구 예산이다. 미국 H1~H4의 최종 평가 잠금은 보존한다.

```bash
uv sync --extra agent --frozen
uv run --extra agent momentum-agent demo --output artifacts/agent-demo-v1
```

합성 예제는 승격·유지·재실행·전진 관찰을 검증하고 주문을 만들지 않는다. 실제 데이터 공급자는 별도 연결 대상이다. 실제 입력·감시 실행·과거 소비 이력 가져오기·중단 복구는 [자동 연구 에이전트 사용법](docs/automatic-research-agent.md)에 설명했다.

## 실행과 검증

```bash
uv sync --frozen
uv run pytest -q
uv run mypy src
uv run ruff check

uv sync --directory apps/kis-agent
uv run --directory apps/kis-agent python -m unittest discover -s tests -v
uv run --directory apps/kis-agent python -m examples.offline_logic
```

루트 pytest는 `tests/`만 수집한다. KIS 앱은 독립 가상 환경과 unittest를 사용하며 런타임 외부 의존성이 없다. 루트 lint는 두 프로젝트를 각자의 설정에 따라 검사한다. 네트워크 없는 환경에서는 캐시에 의존성이 있는 경우에만 `--offline`을 붙인다. 읽기 전용 홈 환경에서는 `UV_CACHE_DIR=/workspace/.cache/uv`처럼 쓰기 가능한 캐시를 지정할 수 있다.

KIS 전체 예제는 `apps/kis-agent/artifacts/logic-v5`에 합성 입력·선정 사유·연구 결과·SQLite 기록을 생성한다. 같은 빌드에서 반복하면 완료 결과를 재사용한다. 과거 개발 산출물·가상 환경·캐시·DB·토큰·실제 자격증명은 Git에 올리지 않는다. 실제 연구 DB는 평가 구간 소비 이력이므로 따로 보존한다.

## 문서

- [미국 모멘텀 검증 명세](docs/spec-v2.0.md)
- [기존 개발 불변 규칙](CLAUDE.md)
- [KIS 종목 선정·자동 개선](apps/kis-agent/docs/08-selection-and-improvement.md)
- [저장소 통합 범위와 검증](docs/kis-agent-integration.md)
- [미국 검증·KIS 자동 연구의 차이와 통합 설계](docs/unified-research-agent-design.md)
- [자동 자기 개선 연구 에이전트 실행·운영](docs/automatic-research-agent.md)

개발 기준 저장소는 `dontukim-ai/momentum-study`다. 이후 작업은 이 checkout 안에서 진행하며 형제 폴더에 별도 프로젝트를 만들지 않는다.
