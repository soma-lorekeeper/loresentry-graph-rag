# refresh의 Content HTTP 조회

`ContentDocumentSource`는 Content의 문서 목록 API로 입력에 없는 추가 문서를 읽는다.
Neptune은 연결된 문서 ID를 찾고, 본문·속성·제목·분류·`revision_no`는 Content에서 얻는다.

## 호출 계약

| 요청 | 목적 |
|---|---|
| `GET /projects/{project_id}/files/content?ids=…` | 선택된 문서를 한 번에 조회. `ids`는 반복 파라미터이며 서로 다른 ID 최대 100개 |

추가 ID는 중복을 합친 뒤 요청 순서대로 100개씩 나눠 보낸다. 추가 ID가 없으면 HTTP 요청을
보내지 않는다. 모든 요청에 신뢰한 호출자의 `X-User-Id`와 `Accept: application/json`을 전달한다.
사용자·프로젝트·문서 ID는 정규 UUID 문자열이어야 한다. 사용자 식별은 생성자에서
명시적으로 받아 한 실행에 바인딩하며, S3 본문이나 공통 환경변수에서 추측하지 않는다.
호출 측이 인증된 사용자 정보를 제공해야 하며 이 어댑터가 토큰을 검증하지는 않는다.

Content는 호출자 소유의 활성(휴지통이 아닌) 프로젝트만 응답하고, 아니면 `404 PROJECT_NOT_FOUND`다.
응답은 `{project_id, documents, missing}`이다. `missing`은 휴지통·없음·다른 프로젝트 문서다.
응답의 `project_id`가 요청과 같아야 하고, `documents`의 ID와 `missing`은 겹치지 않으면서
합쳐서 요청한 ID와 정확히 같아야 한다. 한 요청은 Content의 한 트랜잭션에서 읽힌다.

## 연결과 설정

| 코드 | 책임 |
|---|---|
| [content_documents.py](../../app/adapters/content_documents.py) | HTTP 전송, 신원 전달, 범위 검사, 누락·오류 구분 |
| [content_schema.py](../../app/adapters/content_schema.py) | Content 문서 검증과 `DocumentSnapshot` 변환. S3 `refresh-input-v2` 문서와 같은 모델 |
| [neptune_refresh.py](../../app/adapters/neptune_refresh.py) | 실제 Neptune·Content 어댑터를 주입한 refresh 서비스 생성·종료 |

`neptune_refresh_service(...)`에서 `documents`를 생략하면 실제 Content 어댑터를 사용한다.
서비스를 구성하는 호출 측에서는 아래처럼 인증된 사용자 정보와 나머지 포트를 전달한다.
변수들은 호출 측에서 준비한 값이다.

```python
with neptune_refresh_service(
    user_id=trusted_user_id,
    artifacts=artifacts,
    model=model,
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

목록 원소는 `{id, revision_no, folder_code, title, body_text, properties, relations}`이고
S3 `refresh-input-v2`의 문서와 같은 모양이다. `body_text`는 Content가 서식을 빼고 만든 본문이며
공백·유니코드·제목·속성 값·관계 설명은 정규화하지 않는다. 속성은 `{key, value}` 배열이고
같은 key가 반복될 수 있다. 관계는 `{target_document_id, relation_key, description}`이며
`relation_key`는 상대 문서 분류로 정해진다.

Content의 최초 문서는 `revision_no = 0`이다. 어댑터와 refresh 규칙은 0 이상의 정수를
그대로 보존하고, 음수·문자열·boolean을 거절한다. 추가 응답 필드는 허용하며 실제 사용하는
필드와 속성·관계는 엄격히 검증한다. 원고 등 최초 S3 입력을 최신 Content 응답으로
덮어쓰지 않으며, 부족한 문서만 실행 스냅샷에 추가한다.

## 실패와 재시도

| 상황 | 처리 |
|---|---|
| `404 PROJECT_NOT_FOUND` | `CONTENT_PROJECT_UNAVAILABLE`, 생성 중단 |
| 응답의 `missing` | `DocumentBatch.missing_ids`에 명시. refresh는 `DOCUMENT_MISSING`으로 생성 중단 |
| `401`·`403` | `CONTENT_ACCESS_DENIED`, 생성 중단 |
| 네트워크 실패·`408`·`429`·`5xx` | 재시도 가능한 `CONTENT_UNAVAILABLE` |
| 잘못된 JSON·자료형·문서 스키마 | `CONTENT_RESPONSE_INVALID` |
| 프로젝트 불일치, 받은 문서·missing이 요청 ID와 다름 | `PROJECT_MISMATCH` 또는 `FETCH_MISMATCH` |
| `400`, 그 외 `404`·리다이렉트·비정상 상태 | `CONTENT_REQUEST_REJECTED`. 문서 없음으로 대체하지 않음 |

실패 메시지에 Content 원문 응답이나 사용자 정보를 포함하지 않는다.
필요한 문서를 모두 얻고 실행 스냅샷을 저장한 뒤에만 모델을 호출한다.
모델 실패 후 같은 요청을 재시도하면 저장된 실행 스냅샷을 재사용해 Content를 다시 읽지 않는다.
추가 문서가 100개를 넘어 요청이 나뉘면 요청 사이의 동시점 읽기는 보장하지 않는다.

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
Kafka 수신과 완료 Outbox relay의 연결은 [업무 송수신](KAFKA_TRANSPORT.md)을 따른다.
