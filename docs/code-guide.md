# GraphRAG 서버 코드 읽기

> **책임:** 현재 요청 흐름·시작 구성·테스트와 배포 파일 위치를 안내한다.
>
> **확인할 때:** 변경할 함수나 관련 검증 파일을 찾을 때.
>
> **관련 기준:** 모듈 책임과 구현 경계는 [서버 구조](ARCHITECTURE.md)를 본다.

[main.py](../app/main.py)의 `create_app()`이 환경변수 또는 전달된 대체 구현으로 서비스를 조립한다.
현재 요청 흐름은 여기서 등록한 라우트에서 시작한다.

## 기본 진단 요청

`root()`는 서비스 이름을, `health()`는 고정 상태를 반환한다. 두 함수는 상태 조회 서비스를 호출하지 않는다.

## Neptune 연결 확인

1. [main.py](../app/main.py)의 `health_db()`가 `GraphHealthService.check()`를 호출한다.
2. [application.py](../app/application.py)의 서비스가 주입된 `GraphStatusSource.fetch_status()`로 스냅샷을 조회한다.
3. 운영에서는 [neptune.py](../app/neptune.py)의 `NeptuneStatusSource`가 HTTP와 JSON 해석을 실행한다.
4. 서비스가 [graph.py](../app/graph.py)의 `interpret_status()`로 응답을 검증·해석한다. 이 함수는 IO를 하지 않는다.
5. 라우트가 성공 결과를 기존 JSON 필드로 변환한다. 알려진 조회·검증 실패는 `503`, 예상하지 못한 오류는 기본 `500` 처리로 전달한다.

판단과 실행의 경계·실패 정책은 [서버 구조](ARCHITECTURE.md), HTTP 계약은 [제공 API](API.md)를 따른다.

## 서버 시작과 실행 파일

| 파일 | 읽을 내용 |
|---|---|
| [app/main.py](../app/main.py) | `create_app(source)`에서 의존성과 라우트 구성. `app`은 기본 운영 구성 |
| [Dockerfile](../Dockerfile) | Python 이미지, 비루트 사용자, uvicorn 실행 명령 |
| [requirements.txt](../requirements.txt) | FastAPI·uvicorn·httpx 런타임 버전 |
| [requirements-dev.txt](../requirements-dev.txt) | pytest·httpx 테스트 의존성 |
| [pyproject.toml](../pyproject.toml) | pytest의 모듈 경로와 `tests/` 수집 설정 |
| [ci-cd.yaml](../.github/workflows/ci-cd.yaml) | 테스트·이미지 게시·GitOps 업데이트 호출 |

## 검증 위치

| 파일 | 검증 범위 |
|---|---|
| [test_graph.py](../tests/test_graph.py) | 서버·DB 없는 순수 검증, 누락·잘못된 자료형, 입력 불변성 |
| [test_main.py](../tests/test_main.py) | 대체 구현 주입, 응답 계약, 실패·예상 밖 오류 구분, 앱별 의존성 격리 |
| [test_neptune_integration.py](../tests/test_neptune_integration.py) | 실제 로컬 HTTP 요청, JSON 해석·오류·타임아웃·재시도 없음·반복 조회 |

실행 방법과 운영 확인 범위는 [검증 계획](implementation/TEST_PLAN.md)을 따른다.

## 갱신안 내부 코드

`app/refresh/models.py`에서 값의 의미, `rules.py`에서 판단 단계, `service.py`에서
조회·판단·실행 순서를 읽는다. [Refresh 코드 안내](../app/refresh/README.md)와
[실제 rules 실행 테스트](../tests/refresh/test_real_service.py)가 진입점이다.

모델 연결은 [llm.py](../app/adapters/llm.py), 후보 계약은
[llm_schema.py](../app/adapters/llm_schema.py)를 읽는다.
[evaluate_refresh.py](../scripts/evaluate_refresh.py)가 키·클라이언트와 다섯 fake를 조립한다.
[대체 응답 평가](migration/model-response-evaluation.md)의 명령으로 실제 rules 흐름을 실행할 수 있다.
