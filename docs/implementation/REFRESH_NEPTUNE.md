# refresh의 Neptune 원홉 조회

refresh의 `RelatedDocumentSource`를 실제 Neptune의 `File` 노드와 `REFERENCES` 엣지에
연결한다. 관계 조회 결과는 기존 선택 규칙에 전달하고, 입력에 없는 문서 ID만
`DocumentSource.fetch_documents(project_id, document_ids)`로 요청한다.
본문과 Content의 `revision_no`는 Content 조회 결과에서 얻는다.

## 구현과 조립

| 코드 | 책임 |
|---|---|
| [neptune_relations.py](../../app/adapters/neptune_relations.py) | 실제 원홉 쿼리와 응답 검증, refresh의 `RelatedDocuments` 변환 |
| [neptune_refresh.py](../../app/adapters/neptune_refresh.py) | Neptune 관계 조회를 주입한 `RefreshService` 생성·클라이언트 종료 |
| [content_documents.py](../../app/adapters/content_documents.py) | 선택된 문서 ID를 기존 Content API로 조회하고 범위·본문 응답 검사 |
| [neptune_client.py](../../app/adapters/neptune_client.py) | 투영 쓰기와 원홉 읽기의 공통 HTTP·설정·SigV4·응답 해석 |
| [service.py](../../app/refresh/service.py) | 활성 seed 조회, 준비 상태 검사, 부족한 본문 요청, 실행 스냅샷 저장·재사용 |
| [selection.py](../../app/refresh/selection.py) | 기존 문서 선택·추가 조회 예산·Content 응답 검증 |

실제 관계 조회를 사용하는 조립 진입점은 `neptune_refresh_service(...)` context manager다.
호출자는 `artifacts`, `model`, `publisher`를 기존 포트로 주입하고,
신뢰한 호출자의 `user_id`를 전달한다. `documents`를 생략하면 실제 Content API를 사용한다.
반환된 서비스의 `run(request)`를 호출하고, 종료나 예외 시 소유한 HTTP
클라이언트는 닫힌다. writer 주소·IAM 인증·timeout은
[그래프 저장 설정](GRAPH_PROJECTION.md#실행과-연결-설정)을 공유한다.

Content API·설정·신원 전달·본문 변환은 [Content HTTP 조회](CONTENT_HTTP.md)를 따른다.
`inbox`를 생략하면 [완료 Inbox](REFRESH_INBOX.md)도 같은 Neptune writer에 연결한다.
프로덕션 refresh HTTP/Kafka 수신 진입점은 아직 없다.
평가 도구는 외부 IO 없이 실행되는 기존 `FakeRelations`·`FakeDocuments`를 계속 명시적으로 주입한다.

## 원홉의 의미

- seed는 입력 S3 스냅샷에 존재하는 활성 문서다. 휴지통·삭제 문서는 그래프 조회에서 제외한다.
- seed와 상대 노드는 `File`, 엣지는 `REFERENCES`여야 한다.
- seed·엣지·상대 노드의 `project_id`를 모두 요청 프로젝트로 제한한다.
- 양 끝 노드의 `trashed = false`를 확인한다.
- 저장 방향과 관계없이 `bothE('REFERENCES').otherV()`로 한 번만 이동한다.
- `relation_key`는 상대 노드의 `folder_code`에서 계산하며, 요청한 키 필터도 상대 분류에 적용한다.
- 문서 ID의 `file:` 접두사는 제거하고 Content의 원래 UUID를 반환한다.
- 관계 설명은 실제 엣지의 현재 `description`을 사용한다.

예를 들어 원고 A와 설정 B, 설정 B와 설정 C가 연결되어 있고 seed가 A라면 관계 조회는
A–B만 반환한다. 기존 선택 규칙은 입력에 B가 없을 때 B의 본문을 Content에 요청한다.
C까지 확장하지 않는다. 여러 seed가 같은 상대 문서에 연결되면 문서 조회 ID는 중복 제거한다.
본문은 Neptune의 제목·설명에서 만들어 대체하지 않는다.

노드·관계 읽기는 요청 하나에서 수행하고 결과를 정렬해 같은 그래프 상태가 같은 내부
표현을 만들게 한다. 현재 속성이 없거나 다중 값인 경우, 잘못된 ID·자기 연결·같은 쌍의
서로 다른 관계가 관측된 경우에는 손상된 응답으로 처리한다.

## 준비 상태와 실패

| 상황 | 처리 |
|---|---|
| seed가 활성 노드로 있고 원홉 관계가 없음 | `READY`, 빈 관계 |
| 필요한 활성 seed가 아직 없거나 그래프에서 비활성 상태 | `WAITING`. 서비스는 `GRAPH_NOT_READY`로 중단 |
| `required_graph_version`이 지정됨 | 현재 투영본에 버전이 없으므로 `UNKNOWN`. 서비스는 중단 |
| seed가 없거나 `max_depth = 0` | 관계 IO 없이 빈 결과. 명시 대상 등의 기존 선택은 유지 |
| `max_depth > 1` | `UNSUPPORTED_GRAPH_DEPTH`. 원홉으로 조용히 축소하지 않음 |
| HTTP·네트워크·응답 형식 실패 | 재시도 가능한 `RefreshFailure`. Content·모델 호출을 진행하지 않음 |

이웃 수로 결과를 자르거나 거절하지 않는다. 모델 입력 크기는 호출마다의 토큰 예산과 묶음 실행이 다룬다.

`READY`는 현재 조회 스냅샷에서 seed·관계를 확인했다는 뜻이다. Kafka backlog가 없거나
특정 Content revision까지 모두 반영됐다는 보장은 아니다. 현재 모델에는 그래프 버전이
없으므로 `observed_version`은 `None`이다.

추가 Content 응답의 프로젝트·활성 상태·누락·중복·분류별 관계 키는 기존 `assemble`에서
검증한다. 조회 결과는 모델 호출 전에 실행 스냅샷으로 저장한다. 같은 요청을 재시도할 때
저장된 스냅샷이 있으면 Neptune과 Content를 다시 읽지 않아 생성 문맥을 보존한다.

## 검증과 남은 범위

기본 검증은 다음과 같이 실행한다.

```bash
.venv/bin/python -m pytest tests/test_neptune_relations.py tests/refresh/test_recovery.py -q
```

실제 Gremlin 검증은 [전용 서버 실행 안내](GRAPH_PROJECTION.md#검증)에 따라 서버를 실행한
뒤 저장소 루트에서 수행한다.

```bash
GREMLIN_TEST_ENDPOINT=http://127.0.0.1:18182 .venv/bin/python -m pytest tests/integration/test_refresh_neptune.py tests/integration/test_projection_gremlin.py -q
```

통합 테스트는 실제 저장 어댑터로 만든 그래프를 조회한다. 양방향·한 홉 제한·분류 필터,
휴지통·프로젝트·label 경계, 실제 `RefreshService`가 선택된 ID만 Content 포트에 전달하는지,
본문·revision 보존, 반영 대기와 저장 스냅샷 재사용을 확인한다.
Content HTTP 어댑터는 로컬 API 대체 서버를 사용하고 기본 조립의 완료 Inbox는 실제 Gremlin에
저장한다. S3·모델·완료 발행은 fake다. 유료 모델을 호출하지 않는다.

AWS Neptune의 실제 네트워크·TLS·IAM·쿼리 실행과 운영 Content HTTP 연동은 별도 검증 대상이다.
사용자용 그래프 조회 API와 다중 홉 검색은 이번 범위에 포함하지 않는다.
