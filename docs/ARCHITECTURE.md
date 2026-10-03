# GraphRAG 애플리케이션 구조

> **책임:** 현재 모듈의 책임·의존 관계·실행 구성과 구현 경계를 설명한다.
>
> **확인할 때:** 구현 범위를 파악하거나 변경할 위치를 정할 때.
>
> **관련 기준:** 실제 함수 위치는 [코드 안내](code-guide.md), HTTP 응답은 [제공 API](API.md)를 본다.

현재 서버는 일반 함수·불변 dataclass·Protocol·생성자 주입으로 판단과 IO를 분리한다.
대표 적용은 `GET /health/db`다. 아직 업무 기능이 없어 운영 진단 정책을 대상으로 했으며,
문서 갱신·인가 같은 업무 규칙을 구현한 사례는 아니다.

## 판단·조율·실행의 경계

| 위치 | 책임 | 의존성 |
|---|---|---|
| [graph.py](../app/graph.py) | `interpret_status(payload)`가 응답 형식을 검증하고 `GraphStatus` 반환 | Python 표준 라이브러리만 사용. 환경변수·HTTP·현재 시각·난수 접근 없음 |
| [application.py](../app/application.py) | `GraphHealthService.check()`가 조회 후 판단을 호출하고 `GraphHealth` 반환. `GraphStatusSource`로 필요한 IO 정의 | 순수 판단과 Protocol. FastAPI·httpx 의존 없음 |
| [neptune.py](../app/neptune.py) | `NeptuneStatusSource`가 HTTP 요청·JSON 해석·클라이언트 종료·통신 예외 변환 실행 | httpx와 애플리케이션의 실패 계약 |
| [main.py](../app/main.py) | `create_app(source)`에서 의존성 연결, 환경변수 읽기, HTTP 응답으로 변환 | FastAPI와 실제 어댑터 |

실행 흐름은 `health_db()` → 서비스의 `fetch_status()` → 순수 함수 `interpret_status()` → HTTP 응답이다.
IO 결과를 받아야 검증할 수 있으므로 서비스가 이 순서를 조율한다. 웹 라우트는 메타데이터 검증을 하지 않고,
어댑터는 누락 필드를 `unknown`으로 정하는 정책을 갖지 않는다.

운영의 `app = create_app()`은 환경변수로 HTTPS 주소를 구성해 실제 어댑터를 연결한다.
설정은 앱 생성 시 한 번 읽는다. 테스트는 `create_app(fake_source)`로 대체 구현을 연결하며,
서비스의 전역 객체를 monkeypatch할 필요가 없다. 앱 인스턴스마다 의존성이 독립적이다.
HTTP 클라이언트는 진단 요청마다 만들고 성공·실패 모두 종료한다. 생성 시 연결 확인은 하지 않는다.

## 이 프로젝트에서 선택한 추상화

현재 필요한 IO가 상태 조회 하나이므로 Protocol도 `fetch_status()`와 대상 주소만 제공한다.
Free Monad·범용 Effect Handler·명령 레지스트리는 사용하지 않는다. 동작 하나를 감싸기 위해
실행 계획 dataclass를 추가하지 않고, 판단 결과인 `GraphStatus`와 응답용 `GraphHealth`를 불변 데이터로 표현한다.
작은 프로젝트에 맞춰 파일 네 개로 경계를 나누며 디렉터리 계층을 미리 늘리지 않는다.

새 유스케이스의 순수 함수, 시간·ID 전달, 작업 데이터 도입 기준은
[판단·IO 분리 원칙](implementation/DECISION_AND_IO.md)에서 관리한다.

## 실패·재시도·중복 실행

| 실패 | 전달 방식 | 웹 응답 |
|---|---|---|
| 네트워크·HTTP 상태 오류, JSON 해석 실패 | 어댑터가 `GraphStatusUnavailable`로 변환 | `503`, 기존 `status`·`service`·`error` 형식 |
| JSON 구조·메타데이터 자료형 오류 | 순수 함수가 `InvalidGraphStatus` 발생 | `503` |
| 예상하지 못한 코드 오류 | 포괄적인 catch로 숨기지 않고 전파 | FastAPI의 기본 `500` 처리 |

기존 정상 필드와 누락 시 `unknown` 규칙은 유지한다. 명시적 null·비문자열 메타데이터는 이제
잘못된 응답으로 판정한다. 예외 문자열을 사용하는 통신 오류 응답에는 내부 주소가 포함될 수 있으며,
오류 비노출 계약은 이 변경에서 새로 구현하지 않았다.

이 유스케이스는 Neptune `/status`를 읽기만 한다. 요청 하나당 조회 한 번이며 자동 재시도는 없다.
중복 요청은 읽기를 다시 실행하고 매번 다른 스냅샷을 볼 수 있다. 업무 데이터 쓰기·트랜잭션·Inbox는 필요하지 않다.
실제 HTTP 테스트로 실패 시 재시도하지 않는 것과 반복 요청이 각각 GET만 수행하는 것을 확인한다.

쓰기 유스케이스의 트랜잭션·동시 변경·재시도 경계는
[판단·IO 분리 원칙](implementation/DECISION_AND_IO.md#6-트랜잭션실패재시도)을 따른다.
이전할 기능과 미결정 사항은 [마이그레이션 문서](migration/overview.md)에 둔다.

## 실행과 배포 구성

[Dockerfile](../Dockerfile)은 Python 3.12 slim 이미지에 런타임 의존성과 `app/`을 복사하고,
UID `10001`의 `appuser`로 `uvicorn app.main:app --host 0.0.0.0 --port 8000`을 실행한다.
의존성 버전은 [requirements.txt](../requirements.txt)와
[requirements-dev.txt](../requirements-dev.txt)에 고정돼 있다.

[CI/CD 설정](../.github/workflows/ci-cd.yaml)은 `main` push 또는 수동 실행 시 테스트를 실행한 뒤
이미지를 빌드해 ECR `graph-rag/api`에 게시하고 `loresentry-update-gitops` Lambda를 호출한다.
이미지 태그는 `build-<run number>-<run attempt>`이며 Lambda 결과가 `updated` 또는 `unchanged`인지 검사한다.
이 워크플로 자체는 Pod 준비 완료나 Neptune 연결 성공을 검증하지 않는다.

[프로젝트 README](../README.md)는 Gateway를 통한 접근과 클러스터 내부 서비스를 전제로 한다.
현재 앱에는 인증·사용자 헤더 검사·CORS 미들웨어가 없으며, 실제 외부 접근 제한과
Kubernetes probe 설정은 이 저장소의 코드만으로 확인할 수 없다.

## 아직 구현하지 않은 기능

| 영역 | 현재 없는 구현 |
|---|---|
| 그래프 저장 | 노드·관계 모델, 그래프 쿼리·쓰기, 스키마·데이터 초기화 |
| 검색 | RAG/GraphRAG 검색 API, 임베딩·벡터 검색, 컨텍스트 구성 |
| 이벤트 동기화 | Content 변경 이벤트 소비, 재처리·순서·중복 처리 |
| 데이터 접근 경계 | 프로젝트 격리, 사용자 접근 권한, 휴지통 제외 조건 |
| 서비스 연동 | AI Chat에 검색 결과를 제공하는 업무 API와 Content 호출 |

Neptune `/status` 조회가 구현됐다는 사실은 위 업무 기능이나 그래프 읽기·쓰기 권한이 검증됐다는 뜻이 아니다.
