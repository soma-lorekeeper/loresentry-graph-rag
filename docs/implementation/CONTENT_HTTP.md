# refresh의 Content HTTP 조회

`ContentDocumentSource`는 Content의 기존 API로 입력에 없는 추가 문서를 읽는다.
Neptune은 연결된 문서 ID를 찾고, 본문·속성·제목·분류·`revision_no`는 Content에서 얻는다.
Content에 새 엔드포인트를 추가하지 않는다.

## 호출 계약

| 요청 | 목적 |
|---|---|
| `GET /projects/{project_id}` | 호출자 소유의 활성 프로젝트인지 확인. 추가 문서 조회마다 한 번 호출 |
| `GET /files/{document_id}/content` | 선택된 문서를 ID별로 순차 조회. 중복 ID는 한 번만 요청 |

모든 요청에 신뢰한 호출자의 `X-User-Id`와 `Accept: application/json`을 전달한다.
사용자·프로젝트·문서 ID는 정규 UUID 문자열이어야 한다. 사용자 식별은 생성자에서
명시적으로 받아 한 실행에 바인딩하며, S3 본문이나 공통 환경변수에서 추측하지 않는다.
호출 측이 인증된 사용자 정보를 제공해야 하며 이 어댑터가 토큰을 검증하지는 않는다.

프로젝트 조회는 응답의 `id`와 `trashed_at = null`을 검사한다. Content의 문서 조회는
소유권과 문서 휴지통 상태를 확인하지만 프로젝트 휴지통 상태는 확인하지 않으므로,
활성 프로젝트를 먼저 확인한다. 문서 응답의 `project_id`와 `id`도 요청과 대조한다.
추가 ID가 없으면 HTTP 요청을 보내지 않는다.

## 연결과 설정

| 코드 | 책임 |
|---|---|
| [content_documents.py](../../app/adapters/content_documents.py) | HTTP 전송, 신원 전달, 범위 검사, 누락·오류 구분 |
| [content_schema.py](../../app/adapters/content_schema.py) | Content 응답 검증과 `DocumentSnapshot` 변환 |
| [neptune_refresh.py](../../app/adapters/neptune_refresh.py) | 실제 Neptune·Content 어댑터를 주입한 refresh 서비스 생성·종료 |

`neptune_refresh_service(...)`에서 `documents`를 생략하면 실제 Content 어댑터를 사용한다.
서비스를 구성하는 호출 측에서는 아래처럼 인증된 사용자 정보와 나머지 포트를 전달한다.
변수들은 호출 측에서 준비한 값이다.

```python
with neptune_refresh_service(
    user_id=trusted_user_id,
    artifacts=artifacts,
    model=model,
    publisher=publisher,
) as refresh:
    completion = refresh.run(request)
```

| 설정 | 값 |
|---|---|
| `CONTENT_BASE_URL` | 필수 HTTP(S) 서비스 origin. 클러스터 예: `http://content-api`. 경로·query·인증정보 제외 |
| `CONTENT_TIMEOUT_SECONDS` | 기본 `10`. 양의 유한 초 단위 값. connect는 최대 3초 |
| `content_base_url=` | 조립 함수에서 환경변수 주소를 대체할 수 있는 인자 |
| `user_id=` | 호출별 필수 신원. `documents`를 별도로 주입하지 않을 때 생략하면 구성 실패 |

환경 파일은 실행 측에서 로드해야 한다. 구성만으로 FastAPI나 평가 CLI가 refresh를 실행하지 않는다.
평가에서는 기존처럼 `FakeDocuments`를 명시적으로 주입할 수 있다.
Content와 Neptune의 HTTP 클라이언트는 연결을 재사용하고 context 종료·예외 시 닫힌다.
외부에서 주입한 `httpx.Client`는 주입한 측이 닫는다. 리다이렉트와 자동 재시도는 하지 않는다.

## 본문과 revision

현재 Content `DocumentResponses.Content`의 구조화 본문은
`body = {"schema_version": 1, "doc": {...}}`다. Content의 `BodyText.extract`와 같은
규칙으로 인라인 텍스트를 이어 붙이고 블록·목록·`hardBreak`를 줄바꿈으로 변환한다.
공백·유니코드·제목·속성 값·관계 설명은 정규화하지 않는다. 아직 변환 전인 문서는
`body = null`일 때 `legacy_body_md` 원문을 그대로 보존한다. 본문 표현이 모두 없거나
둘 다 채워졌으면 실패한다. 모르는 본문 버전·노드도 조용히 생략하지 않는다.

Content의 최초 문서는 `revision_no = 0`이다. 어댑터와 refresh 규칙은 0 이상의 정수를
그대로 보존하고, 음수·문자열·boolean을 거절한다. 추가 응답 필드는 허용하며 실제 사용하는
필드와 속성·관계는 엄격히 검증한다. 원고 등 최초 S3 입력을 최신 Content 응답으로
덮어쓰지 않으며, 부족한 문서만 실행 스냅샷에 추가한다.

## 실패와 재시도

| 상황 | 처리 |
|---|---|
| 프로젝트 `404 PROJECT_NOT_FOUND` | `CONTENT_PROJECT_UNAVAILABLE`, 본문 요청 중단 |
| 문서 `404 FILE_NOT_FOUND` | `DocumentBatch.missing_ids`에 명시. refresh는 `DOCUMENT_MISSING`으로 생성 중단 |
| `401`·`403` | `CONTENT_ACCESS_DENIED`, 생성 중단 |
| 네트워크 실패·`408`·`429`·`5xx` | 재시도 가능한 `CONTENT_UNAVAILABLE` |
| 잘못된 JSON·자료형·본문 스키마 | `CONTENT_RESPONSE_INVALID` |
| 프로젝트·문서 ID 불일치 | `PROJECT_MISMATCH` 또는 `FETCH_MISMATCH` |
| 그 외 `404`·리다이렉트·비정상 상태 | `CONTENT_REQUEST_REJECTED`. 문서 없음으로 대체하지 않음 |

실패 메시지에 Content 원문 응답이나 사용자 정보를 포함하지 않는다.
필요한 문서를 모두 얻고 실행 스냅샷을 저장한 뒤에만 모델을 호출한다.
모델 실패 후 같은 요청을 재시도하면 저장된 실행 스냅샷을 재사용해 Content를 다시 읽지 않는다.
여러 Content 요청은 하나의 DB 트랜잭션이 아니므로 문서 사이의 동시점 읽기는 보장하지 않는다.

## 검증 범위

저장소 루트의 Linux 호스트에서 다음 명령을 실행한다. 통합 테스트는 로컬 소켓을 연다.

```bash
.venv/bin/python -m pytest tests/test_content_documents.py tests/integration/test_content_http.py -q
```

로컬 HTTP 대체 서버로 실제 GET·헤더·실패·스냅샷 복구를 확인한다.
[Neptune 통합 테스트](../../tests/integration/test_refresh_neptune.py)는
[전용 Gremlin 서버](GRAPH_PROJECTION.md#검증)에 저장한 관계를 읽어 실제 Content HTTP
어댑터까지 연결하며, 선택한 원홉 문서만 요청하는지 검사한다.
Content의 Spring 애플리케이션·운영 클러스터·실제 AWS Neptune 검증과는 구분한다.
완료 기록과 복구는 [Neptune Inbox](REFRESH_INBOX.md)를 사용한다.
refresh HTTP/Kafka 수신 진입점과 실제 Kafka 완료 발행은 별도 구현 범위다.
