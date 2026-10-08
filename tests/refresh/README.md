# Refresh 테스트

외부 서버·DB·AWS 자격증명·LLM 키 없이 실제 순수 rules와 fake IO를 실행한다.
실제 LLM의 제안 품질이나 운영 저장소의 내구성 검증과는 구분한다.

```bash
.venv/bin/python -m pytest tests/refresh -q
.venv/bin/python -m pytest tests/refresh/test_real_service.py -q
```

전체 회귀는 `.venv/bin/python -m pytest -q`다. 기존 진단 HTTP 통합 테스트에는
루프백 소켓이 필요하지만, 이 디렉터리의 [conftest.py](conftest.py)는 소켓 생성·연결을 차단한다.

| 파일 | 검증 |
|---|---|
| `test_policy.py`, `test_fixed_fixtures.py`, `test_contracts.py` | 불변 값, 독립 예산, 고정 출처·revision |
| `test_selection.py` | 20/21개·5000/5001자, 요청 manifest, 프로젝트·상태·중복·순환·누락 |
| `test_chunking.py`, `test_context.py` | 전체 원문 보존, Unicode 구간, 반복 문장, 출처 중복 제거, 문맥 예산·버전 |
| `test_document_validation.py` | 허용 대상·필드·revision·근거, 동일 본문 제외, 충돌 실패 |
| `test_relation_validation.py` | 두 방향 분류 키, 기존 연결·역방향 중복, 부분 오류의 전체 실패 |
| `test_real_service.py` | 실제 rules와 여섯 fake, 다중 호출, 관계 전용·변경 없음, 입력 경계·복구 |
| `test_serialization.py` | JSON 왕복·고정 예시, 출처·근거 참조, 완료 상태 매핑·저장 불가 실패 |
| `test_data_fakes.py`, `test_model_fake.py`, `test_job_fakes.py` | fake 자체의 불변성·오류 주입·실행권 계약 |
| `test_service.py`, `test_recovery.py` | 기존 골격의 IO 순서·실패 전파·재개. 대부분 ScriptedRules 사용 |

실제 규칙을 읽거나 실행할 때 다음 예시에서 시작한다.

```python
from tests.refresh.test_real_service import real_scenario

case = real_scenario()
completion = case.service.run(case.request, "example-execution")
assert completion == case.publisher.delivered[0]
```

실패 주입 예시:

```python
from app.refresh.errors import RefreshFailure
from app.refresh.models import Failure

case.calls.failures["publisher.ack"].append(
    RefreshFailure(Failure("PUBLISH_TIMEOUT", "acknowledgment lost", True))
)
```

테스트 통과는 원문 인용이 실제로 제안을 뒷받침하는지, 실제 모델이 기대 제안을 만드는지
증명하지 않는다. [현재 이식 결과와 남은 검증](../../docs/migration/rules-implementation.md)을 참고한다.

OpenAI 후보 계약·오류·예산은 `test_llm_schema.py`, `test_llm.py`에서 검증한다.
평가 기록과 대화 응답 재생은 `test_evaluation_*.py`, `test_response_replay.py`를 본다.
실제 SDK의 로컬 HTTP 검증은 소켓 차단 경계 밖의 `tests/integration/test_openai_http.py`에 둔다.
키 없이 실행할 전체 흐름은 [대체 응답 평가](../../docs/migration/model-response-evaluation.md)를 따른다.

S3 저장소의 엄격 입력·크기·스트림 종료·설정은 `test_s3_artifacts.py`에서 확인한다.
실제 boto3 전송·불변 저장·결과 복구는 `tests/integration/test_s3_http.py`에 둔다.
설정과 검증 경계는 [S3 저장소](../../docs/implementation/S3_ARTIFACTS.md)를 따른다.
