# GraphRAG 검증 계획

> **책임:** 기존 테스트가 검증하는 범위와 추가로 필요한 코드·운영 검증을 구분한다.
>
> **확인할 때:** 변경 후 검증할 시나리오를 고르거나 테스트 통과의 의미를 판단할 때.
>
> **관련 기준:** 응답은 [제공 API](../API.md), 호출 설정은 [호출 API](../API_CALLS.md), 실행 방법은 [프로젝트 README](../../README.md#test)를 본다.

검증은 순수 판단, 대체 구현으로 연결한 앱, 실제 HTTP IO를 나눈다.
로컬 통합 테스트는 임시 포트의 HTTP 서버를 사용하며 외부 네트워크·AWS 자격 증명·실제 Neptune은 필요하지 않다.

## 자동화 테스트

| 파일 | 확인 기준 |
|---|---|
| [test_graph.py](../../tests/test_graph.py) | 같은 입력은 같은 결과, 입력 불변, 누락은 unknown, 잘못된 구조·null·비문자열은 거절. FastAPI나 DB 없이 실행 |
| [test_main.py](../../tests/test_main.py) | 기본 라우트는 IO 없음, 성공 JSON 유지, 알려진 실패는 503, 코드 오류는 전파, 앱별 주입 의존성 독립 |
| [test_neptune_integration.py](../../tests/test_neptune_integration.py) | 실제 어댑터와 라우트 연결, GET /status 경로, JSON 정상·비정상, HTTP 실패, 읽기 타임아웃, 자동 재시도 없음, 반복 읽기 |

```bash
# 프로젝트의 Python 3.12 가상환경 사용
.venv/bin/python -m pytest tests/test_graph.py -q
.venv/bin/python -m pytest -m 'not integration' -q
.venv/bin/python -m pytest -m integration -q
```

전체 검증은 `.venv/bin/python -m pytest -q`로 실행한다.
[CI/CD](../../.github/workflows/ci-cd.yaml)의 기존 `pytest` 명령도 모든 테스트를 수집한다.
순수 테스트에는 서버가 없으며, 통합 테스트 실행 환경에는 루프백 소켓 사용 권한이 필요하다.

## 검증 한계

위 세 파일은 읽기 전용 운영 진단 검증이다. 갱신안 rules의 검증은 아래 별도 범위를 따른다.
DB 쓰기 트랜잭션·Kafka 전달 보장을 검증한 것은 아니다.
통합 테스트는 HTTP 어댑터의 실제 전송을 검증하지만 Neptune의 TLS·IAM 인증·그래프 연산은 검증하지 않는다.
라이브 서비스에 도달하지 않았다는 사실과 테스트용 HTTP 서버에서 IO가 실행됐다는 사실을 구분한다.

## 운영 환경에서 별도 확인할 항목

| 항목 | 확인 기준 |
|---|---|
| 실제 배포 | 배포 이미지가 의도한 커밋인지, Pod 준비 상태와 실제 probe 경로가 무엇인지 확인 |
| Neptune 접근 | 실행 환경의 DNS·네트워크·TLS·포트와 실제 `/health/db` 응답 확인 |
| 인증 요구 | 대상 Neptune의 인증 요구와 현재 서명 없는 HTTP 요청의 호환 여부 확인 |
| 접근 제한 | 진단 API와 FastAPI 문서 경로가 의도한 네트워크 범위에서만 접근 가능한지 확인 |

이 항목들은 이 저장소의 테스트로 검증되지 않는다. 실제 운영 상태는 배포·인프라 증거와
실행 결과를 별도로 확인해야 한다.

## 업무 기능 구현 시 추가할 범위

그래프 저장·검색·이벤트 소비가 추가되면 프로젝트 격리, 접근 권한, 휴지통 제외,
명시적 문서 참조만의 관계 생성, 이벤트 중복·순서·재처리와 AI Chat 연동을 검증해야 한다.
현재 갱신안 내부 판단은 구현했으며 실제 그래프 저장·검색·이벤트 소비는 미구현이다.


## 갱신안 rules와 fake IO

[Refresh 테스트 안내](../../tests/refresh/README.md)에 파일별 검증을 정리했다.
기본 RefreshRules로 입력 한도·manifest·프로젝트·상태·근거·후보 충돌과 다중 대상 호출을
검증한다. 소켓을 차단하고 JSON fake 저장소로 출처·결과 왕복·복구를 확인한다.
기존 ScriptedRules 테스트는 실행 순서·오류 전달 검증으로 별도 유지한다.
실제 LLM 품질·S3 내구성·Content 인가·Neptune 반영·Kafka 전달 보장은 후속 검증이다.

## 모델 호출과 응답 대체

`tests/refresh/test_llm_schema.py`·`test_llm.py`는 파서·엄격 타입·오류 분류와 호출 예산을,
`tests/integration/test_openai_http.py`는 실제 SDK의 로컬 HTTP 요청·무재시도·종료를 확인한다.
`test_evaluation_*.py`·`test_response_replay.py`는 시도 기록·실패·사용량 불명·응답 재생·
버전 불일치·잘못된 원문 위치 거절을 검증한다. 기본 pytest는 원격 API를 호출하지 않는다.

대화 작성 응답 6사례의 실행 결과는 [평가 기록](../migration/model-response-evaluation.md)에 있다.
실제 API 인증 성공·모델 생성 품질·사용량은 후속 검증으로 남긴다.

## 새 설정 생성과 원고 보존

[test_new_settings.py](../../tests/refresh/test_new_settings.py)는 기존 설정 없는 원고 한 편,
새 인물과 기존 인물 연결, 새 후보끼리 연결, 생성만 있는 PROPOSED와 JSON 왕복·결과 재사용을
검증한다. 첫 호출만 생성을 허용하는 범위, 잘못된 후보 ID·분류·revision·근거,
중복 이름·충돌 본문의 전체 실패도 확인한다. 원고 수정은
[test_document_validation.py](../../tests/refresh/test_document_validation.py)에서
원고 ID를 허용 대상으로 주입해도 거절하는지 확인한다.

평가의 `manuscript_content_preserved`는 최초 입력·저장된 실행 스냅샷·입력 저장소의 원고를
비교한다. 공백·줄바꿈·본문 변경 또는 원고 수정 제안이 있으면 false이고 평가 명령은 실패한다.
`expected_new_settings`는 생성 후보의 ID·분류·이름을 고정 기대와 비교한다.
이는 설정 내용의 의미적 정확성이나 프로젝트 전체 중복 부재를 보증하지 않는다.
조회하지 않은 설정의 중복·별칭·동명이인 판단과 Content 승인 시 ID 발급·관계 반영은 후속 검증이다.

저장소 루트에서 실행한다. 출력 디렉터리는 새 경로를 사용하며 원격 요청은 없다.

```bash
.venv/bin/python -m scripts.evaluate_refresh \
  --suite new-settings --env-file .env.example \
  --trial new-settings-v2 --output .evaluation/new-settings-v2 --repeat-saved
```

현재 생성 후보는 `refresh-candidate-v2`, 프롬프트는 `refresh-prompt-v3`, 결과는
`refresh-result-v2`다. 과거 v1 결과 읽기와 v1 후보/v2 프롬프트 응답 재생은 호환 경로이며
새 생성 기능의 모델 품질 검증과 구분한다. [새 생성·연결 예시](../examples/refresh/result-new-settings.json)를 참고한다.

현재 구현 확인: 전체 pytest 300개와 Ruff lint·format 검사가 통과했다.
실제 OpenAI 생성·품질은 인증 실패로 미검증이며 운영 S3·Content·Neptune·Kafka 연동도 미연결이다.
