**v0.3 — 연결 진단·검증 후보 등록·주문 없는 관찰 운용**

2026-10-16. `doctor`, `release`, `shadow` 명령을 추가했다. 연구가 통과한 후보를 고정된 관찰 버전으로 등록하고, 별도 검토 기록을 거쳐 목표 비중과 계좌 상태를 관찰한다. KIS 주문 엔진의 활성 전략을 변경하지 않는다. 실제 서버 호출·실시간 전진 관찰·수익성 검증은 수행하지 않았다.

| 기능 | 구현한 경계 |
| --- | --- |
| 연결 진단 | 키 존재 여부·계좌 형식·현재 관리 환경의 호스트 정책을 표시. 값·토큰·계좌번호는 출력하지 않음 |
| 읽기 전용 probe | 명시한 경우 OAuth 인증, 시세·잔고·당일 주문 GET만 허용. 주문·취소 POST는 transport에서 거절 |
| 후보 등록 | 원래 평가 DB의 통과 판정·수치 기준·최종 구간 소비 기록·CSV 해시·현재 코드 버전을 확인 |
| 버전 묶음 | 전략·원본 평가 해시·데이터 출처·평가 비용·위험·관찰 한도를 고정. 파일 보고서만으로 등록 불가 |
| 관찰 활성화 | 명시한 reviewer·reason과 expected revision을 기록. 중단된 계좌·미체결 주문·계좌 변경·동시 변경 충돌은 거절 |
| 관찰 | 평가 종료 이후 세션만 사용. 확정봉 가용 시각·원래 데이터 보존·비중·원장·미확정 주문을 확인 |
| 중단 | 데이터 지연/변경, 한도 초과, 계좌 정지 등을 STOPPED로 영속 저장. 재시작·다른 release 활성화로 해제 불가 |

주요 코드는 `diagnostics.py`, `releases.py`다. DB 스키마 v3는 releases, shadow_slots, shadow_observations를 추가하며 기존 주문·체결·평가·소비 구간을 보존한다. SQLite 파일에 쓰기 권한이 있는 공격자를 막는 보안 경계는 아니다. 해시는 우발적인 변경·버전 혼용을 검출하며 평가 프로세스의 OS 권한 분리는 아직 필요하다.

**키 없이 재현하기.** 프로젝트 루트에서 실행한다.

```bash
uv sync --offline
uv run python -m unittest discover -s tests -v
uv run python -m examples.offline_operations --output artifacts/operations-v3
uv run kis-agent doctor --output artifacts/readiness.json
```

검증 결과는 **78개 테스트 통과**다. 기존 59개에 후보 탈락·변조·구현 변경·데이터 변경·소비 기록 누락·동시 변경·미체결·계좌 정지·미래 세션·재시작·손실 한도·관찰 중단·진단 비밀 유출 방지 등을 추가했다. 실행 결과는 artifacts/operations-v3/validation.json, release.json, observed.json, stale.json, status.json, doctor.json에 남는다. DB와 CSV도 함께 저장한다.

이 예제는 성공적인 등록 분기를 시험하도록 만든 **400일 반복 주기의 합성 fixture**와 고정된 평가 자본 10,000을 사용한다. 이 시장이나 파라미터는 투자 근거가 아니며 과거 날짜로 진행하는 소프트웨어 시험이다. 기존 기본 합성 데모에서 후보가 탈락하면 KEEP_CHAMPION을 유지하며, 이 예제의 통과 판정으로 대체하지 않는다. 두 예제 모두 실제 서비스 호출과 주문은 0회다.

동일 빌드의 예제 재실행은 원래 평가와 관찰을 재사용한다. 코드가 바뀐 후 기존 평가를 반복해 통과시키려고 DB를 지우지 않는다. 연구 DB를 보존하고 새 전진 데이터를 사용해야 한다. 소프트웨어 테스트용 합성 산출물의 버전별 보관과 실제 연구의 독립 평가 이력은 구별한다.

**실제 연결 진단.** 키는 실행 환경의 비밀 설정에서 구성한다. 기본 doctor는 네트워크를 호출하지 않는다.

```bash
uv run kis-agent doctor
uv run kis-agent doctor --probe-paper --symbol "$KIS_SYMBOL" --output artifacts/paper-read-probe.json
```

`--probe-paper`는 tokenP POST와 quote/balance/orders GET만 허용하며 계좌를 초기화하거나 원장을 덮어쓰지 않는다. 토큰은 기본 artifacts/.secrets의 비공개 캐시에 보관한다. 구성 누락 또는 관리 정책의 명시적 차단이 있으면 인증 요청도 보내지 않는다. 정책 파일이 없는 일반 환경은 UNKNOWN으로 표시하고, 사용자가 명시한 probe에서는 연결을 시도할 수 있다. 정책의 허용은 실제 접속 성공을 뜻하지 않는다.

실패한 첫 조회에서 멈추며 자동 재시도하지 않는다. 결과에는 분류된 상태만 남기고 원시 응답·에러·보유 수량·잔고·주문번호를 넣지 않는다. 필요할 경우 별도의 비공개 수집 절차로 실제 계약을 점검해야 한다. 세 조회가 실제 transport에서 성공해야 read_probe_verified가 true가 된다. 수기 fixture는 PASSED여도 해당 값은 false다. 주문 조회가 비어 있을 수도 있으므로 full_order_contract_verified는 항상 false다. 취소 가능 수량·수수료·정산·실제 주문·응답 유실 복구는 별도 검증 대상이다.

이번 환경의 실행 결과는 키 미구성, 한투·OpenAI 호스트 정책 차단, HTTP 요청 0회다. 따라서 읽기 probe와 모델 호출은 실서버에서 검증되지 않았다.

**검토 가능한 등록·관찰 절차.** 등록은 원래 평가 DB와 동일한 CSV를 사용한다. configs/shadow.example.json의 종목·비용·손실 한도는 합성 시험 예시다. 실제 평가 유니버스와 관찰 정책을 명시한 파일로 대체해야 한다.

```bash
uv run kis-agent release --db "$RESEARCH_DB" --run-id "$RUN_ID" --csv "$EVALUATION_CSV" --policy "$SHADOW_POLICY" --output artifacts/review-release.json
uv run kis-agent shadow activate --db "$RESEARCH_DB" --slot reviewed-shadow --release-id "$RELEASE_ID" --account "$JOURNAL_ACCOUNT" --expected-revision 0 --reviewer "$REVIEWER" --reason "$REVIEW_REASON"
uv run kis-agent shadow observe --db "$RESEARCH_DB" --slot reviewed-shadow --csv "$FORWARD_CSV" --session "$KST_SESSION" --output artifacts/shadow-observation.json
uv run kis-agent shadow status --db "$RESEARCH_DB" --slot reviewed-shadow
```

release는 활성화하지 않는다. activate는 기존에 초기화된 계좌의 관찰용 포인터만 바꾸고 변경 이력을 남긴다. 최초 expected revision은 0이며 이후 현재 revision을 사용한다. 해당 계좌의 open order가 있으면 먼저 대사·해결해야 한다. `--as-of`는 역사적 재생·테스트용으로만 명시하고, 기본 관찰은 현재 UTC 시각을 사용한다. 세션 날짜는 KST 기준이다. Forward CSV에는 고정된 원래 평가 구간과 이후 데이터를 함께 넣는다. 기업행위로 과거 가격을 다시 쓰는 데이터는 현재 지원하지 않는다.

관찰은 로컬 원장의 현금·보유 수량을 종가로 평가하고 목표 비중을 기록한다. **브로커 잔고 새로고침·주문 계획·가상 체결·자동 주문은 수행하지 않는다.** KIS 계좌는 별도로 kis-paper reconcile을 실행해 원장의 정확성을 확인해야 한다. 관찰 보고서에는 LOCAL_JOURNAL_NOT_REFRESHED_FROM_BROKER를 명시한다. 별도의 KIS 수동 주문 명령을 이 관찰 STOPPED 상태가 차단하지는 않는다. 현재 손실 점검을 실거래용 OMS 브레이커로 사용할 수 없다.

일일 손실 기준은 당일 첫 관찰에서는 직전 관찰의 자산, 최초 관찰에서는 당시 자산이다. 이후 당일에는 기준을 유지한다. 고점은 관찰 기록 전체에서 유지하며 release를 교체해도 초기화하지 않는다. 외부 입출금·배당·분할의 성과 조정은 없으며, 관찰 간 발생한 최대 손실이나 최초 관찰 이전 손실은 측정하지 못한다. 매일 장 마감 관찰로 장중 위험을 제어할 수 있다는 뜻도 아니다.

동일 슬롯·revision·시각의 동일 입력은 저장 결과를 반환한다. 입력이 다르면 충돌로 거절한다. 과거 시각의 새 기록도 거절한다. 기존 결과를 재조회한 보고서의 시각은 현재 상태를 뜻하지 않으므로 최신 상태는 shadow status로 확인한다. STOPPED에서는 새로운 관찰의 신호를 제공하지 않는다. 현재 중단 해제 명령은 없으며, 원인과 재개 정책을 검토한 별도 구현이 필요하다.

다음 연결 과제는 실제 읽기 계약 확인, 수수료·취소·정산 출력 검증, 휴장·시장 상태와 원장 현금 흐름 처리, 그 이후의 KIS 모의 주문·실시간 관찰이다.
