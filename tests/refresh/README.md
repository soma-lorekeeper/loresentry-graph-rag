# Refresh 골격 테스트

외부 서버·DB·AWS 자격증명·LLM 키 없이 내부 골격과 fake를 실행한다.
실제 갱신안의 품질이나 운영 저장소의 내구성을 검증하는 테스트는 아니다.

저장소 루트에서 프로젝트 Python 환경으로 실행한다.

```bash
.venv/bin/python -m pytest tests/refresh -q
.venv/bin/python -m pytest tests/refresh/test_service.py tests/refresh/test_recovery.py -q
```

전체 회귀 검증은 `.venv/bin/python -m pytest -q`로 실행한다.
다른 기존 테스트가 HTTP 루프백 소켓을 사용할 수 있지만, 이 디렉터리의
[conftest.py](conftest.py)는 socket 생성과 연결을 차단한다.
새 의존성 설치는 필요하지 않다.

| 파일 | 검증 |
|---|---|
| `test_contracts.py` | 내부 스냅샷·revision·근거 위치와 요청 식별 |
| `test_rules_skeleton.py` | 모든 미구현 판단의 명시적 오류, 테스트 stub의 독립성 |
| `test_data_fakes.py` | 자료 저장·조회·불변성·오류 주입·범위별 fixture |
| `test_model_fake.py` | 고정 후보·변경 없음·잘못된 후보·timeout |
| `test_job_fakes.py` | 접수·입력 충돌·실행권·checkpoint·발행 응답 유실 |
| `test_service.py` | 정상 흐름·조회 생략·변경 없음·기본 미구현 중단 |
| `test_recovery.py` | 오류별 후속 호출 중단, 자료 고정, 저장 결과 재사용과 재발행 |

간단한 실행 예시는 테스트 전용 scenario를 사용한다.

```python
from tests.fakes.scenario import scenario

case = scenario()
completion = case.service().run(case.request, "example-execution")
assert completion == case.publisher.delivered[0]
```

오류 주입은 각 호출의 이름을 지정한다.

```python
from app.refresh.errors import RefreshFailure
from app.refresh.models import Failure

case.calls.failures["publisher.ack"].append(
    RefreshFailure(Failure("PUBLISH_TIMEOUT", "acknowledgment lost", True))
)
```

새 사례는 기존 fixture를 수정하지 말고 `scenario()`로 독립 상태를 만든다.
내부 stub이 후보를 수락한다고 해서 실제 업무 규칙이 통과한 것은 아니다.
실제 구현·미구현 목록, 포트 교체 위치와 후속 검증은
[마이그레이션 현황](../../docs/migration/fake-baseline.md)을 참고한다.
