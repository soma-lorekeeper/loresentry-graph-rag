# Kafka 업무 송수신

Content의 확정 변경을 Neptune에 투영하고 최신화 요청의 승인 전 갱신안을 생성한다.
업무 consumer는 FastAPI 수명주기로 실행하며 기본 비활성이다. 기존 fake polling은 sandbox 전용으로 유지한다.
두 업무 토픽은 별도 Consumer·PollingWorker·작업 스레드·Producer로 처리한다.
최신화 활성화 시 별도 Neptune 클라이언트·Producer를 가진 Outbox relay 스레드도 시작한다.

## 코드와 원자성

| 코드 | 책임 |
| --- | --- |
| [messages.py](../../app/kafka/messages.py) | 공통 헤더·UTF-8 JSON·key·subject·sequence 검증과 완료/DLQ 포장 |
| [handlers.py](../../app/kafka/handlers.py) | Content payload를 ProjectionChange·RefreshRequest로 변환하고 서비스 호출 |
| [ordered_projection.py](../../app/adapters/ordered_projection.py) | 전체 관계 스냅샷 교체·순서 fence·문서/프로젝트 삭제 tombstone |
| [projection_ready.py](../../app/adapters/projection_ready.py) | 요청이 의존하는 Content 이벤트의 Inbox와 프로젝트 삭제 여부 확인 |
| [business_runtime.py](../../app/kafka/business_runtime.py) | 실제 IO 조립·토픽별 실행·종료·health·모델 시간 예산 |
| [outbox.py](../../app/refresh/outbox.py) | 완료 Inbox의 발행 대기를 독립적으로 재시도하는 relay |
| [publisher.py](../../app/kafka/publisher.py) | 레코드별 전송 확인·큐 포화·전달 기한, 완료/DLQ 발행 |
| [consumer.py](../../app/kafka/consumer.py) | 명시적 업무 처리기·제한 재시도·DLQ와 기존 polling 연결 |

그래프 변경·Inbox·순서/삭제 기록은 Gremlin 요청 하나로 확정한다.
문서별 순서 fence와 관계 양쪽 문서의 마지막 스냅샷 순서를 검사해 오래된 관계를 재생성하지 않는다.
삭제 tombstone은 UUID를 영구 폐기하는 계약이며 이후 더 큰 seq의 이벤트도 문서를 부활시키지 않는다.
아직 도착하지 않은 관계의 상대 문서는 숨겨진 placeholder로 생성하고 자기 스냅샷이 도착하면 활성화한다.
기존 fixture의 sequence 없는 변경 모델은 호환 경로이며 운영 Kafka handler는 항상 sequence를 제공한다.

갱신안은 입력·실행 문맥·결과를 불변 S3 객체로 저장한다. S3 결과 저장 → Neptune Inbox·Outbox 원자 기록 → 요청 offset 순서다.
별도 relay가 발행 대기를 읽어 Kafka ack 후 발행 완료로 표시한다. 요청 소비와 전송은 독립적이다.
완료 ID는 전용 고정 UUIDv5 namespace와 project_id/request_id로 결정한다.
완료 전 중복 실행·모델 비용은 가능하다. Kafka ack 뒤 발행 완료 표시 전 장애는 중복 발행을 허용한다.
relay는 요청 재수신 없이 미발행 건을 재시도하며, exactly-once 실행·발행을 보장하지 않는다.

## Content와 합친 계약

Content의 [ContentEvents](../../../loresentry-content/src/main/java/com/loresentry/content/outbox/ContentEvents.java)는
사용자별 트랜잭션 advisory lock을 쓰고 문서·관계 변경과 Outbox를 함께 commit한다.
같은 프로젝트의 seq는 이 경로에서 commit 순서와 일치한다. DB 직접 수정은 이 경로를 우회하므로
운영 원본 변경에 사용하지 않는다. relay의 비동기 실패나 DLQ 재투입은 여전히 순서를 바꿀 수 있다.

`FileChanged`는 `project_id`, `file_id`, `revision_no`, `change_type`, `changed_fields`,
`file`, `relations`를 보낸다. file에는 title·folder_code·description·body_text,
관계에는 무방향 저장 행의 relation_id·target_document_id·description이 있다.
휴지통·영구 삭제는 file/relations=null이며 상태 변경·tombstone으로 처리한다.
기존 `version` payload 초안은 사용하지 않고 ce_sequence를 순서 기준으로 쓴다.

`GraphRefreshRequested`는 다음을 포함한다.

- project_id·request_id·신뢰한 user_id와 `files[].file_id/revision_no/state`
- 입력 S3 bucket/key 및 **그 바이트 전체**의 SHA-256 input_fingerprint
- 고정 model_settings의 model·prompt_version·schema_version
- `required_events`: 이 프로젝트 각 문서의 최신 투영 이벤트 ID 목록. 삭제 문서도 포함

Content는 새 요청 시 투영 head가 없는 기존 문서도 Outbox에 기록한다.
GraphRAG는 required_events가 모두 Neptune Inbox에 있을 때 생성한다. 다른 토픽의 순서나
단순 프로젝트 최대 seq만으로 준비 완료를 판정하지 않는다. 입력 manifest·설정은 같은 요청에서 바꾸지 않는다.

Content의 활성화된 요청 API는 다음과 같다. 호출자는 기존 X-User-Id 규칙을 따른다.

| 경로 | 동작 |
| --- | --- |
| POST `/projects/{projectId}/graph-refresh` | `{ "files": ["문서 UUID"] }`, 202와 request_id·GENERATING. 최대 20개·활성 본문 5,000 코드 포인트 |
| GET `/projects/{projectId}/graph-refresh/{requestId}` | status와 저장된 v3 result. 소유자·활성 프로젝트만 접근 |

입력 S3 업로드가 실패하면 요청·Outbox DB 변경은 commit되지 않는다. DB rollback 뒤 남은 입력은 lifecycle로 정리한다.
Content 완료 handler는 요청·프로젝트·입력 fingerprint·v3 outcome을 검사해 후보 JSON과 READY/FAILED 상태를
Inbox 트랜잭션에서 함께 저장한다. 같은 request_id의 다른 ce_id도 중복 후보를 만들지 않는다.
S3 읽기는 현재 최대 15초로 제한한 DB 트랜잭션 안에서 수행한다. Content poll batch 10건과
max.poll.interval을 조정할 때 이 비용을 포함한다.
문서·새 설정·관계 제안은 자동 적용하지 않는다. 승인 UI·실제 적용 API는 이 Kafka 연동의 범위가 아니다.
한 시간 이상 미완료인 요청은 분당 정리 작업으로 FAILED 처리한다. 늦은 결과가 이를 READY로 되돌리지 않는다.
프로젝트/사용자 삭제 뒤 S3 prefix를 best effort 정리하고, 삭제/만료된 요청의 늦은 결과도 정리한다.
정리 장애의 잔여 객체는 3일 lifecycle에 의존한다.

## 실행 설정

| GraphRAG 환경변수 | 기본값·역할 |
| --- | --- |
| KAFKA_PROJECT_CHANGED_ENABLED | false. FileChanged·ProjectDeleted 처리 |
| KAFKA_REFRESH_REQUESTED_ENABLED | false. 갱신안 생성 처리 |
| KAFKA_BOOTSTRAP_SERVERS | 활성화 시 필수 |
| KAFKA_SHUTDOWN_SECONDS | 360. 전체 업무 worker 종료 대기 예산 |
| KAFKA_REFRESH_TASK_SECONDS | 600. 요청별 모델 작업 예산 |
| KAFKA_POLLING_ENABLED | false. sandbox와 업무 실행 동시 활성화 금지 |
| NEPTUNE_ENDPOINT·NEPTUNE_PORT | writer hostname·8182. 테스트는 명시적 endpoint 주입 |
| GRAPHRAG_S3_BUCKET·GRAPHRAG_S3_INPUT_BUCKET | refresh 활성화 시 필수. 비공개 버킷 |
| CONTENT_BASE_URL·OPENAI_API_KEY | refresh 활성화 시 필수 |
| OPENAI_TIMEOUT_SECONDS | 240. 작업 잔여 예산보다 길게 호출하지 않음 |

고정 group은 `graph-rag.project-changed`, `graph-rag.graph-refresh-requested`다.
현재 projection Inbox 논리 이름과 refresh Inbox 이름은 유지한다. 운영자가 group을 바꿔 중복 제어를 초기화하지 않는다.
실행 중 worker 실패는 /health를 503으로 만든다. 연결·할당·lag의 실시간 증명은 아니다.

SIGTERM 때 새 업무를 중단하고 poll을 유지한다. 모델 호출 중이면 현재 호출의 timeout을 기다리며
다음 호출은 시작하지 않는다. 중단된 요청은 offset을 진행하지 않고 저장 문맥으로 재실행한다.
GitOps의 Pod grace period는 420초다. 강제 종료의 재전달·모델 비용 중복은 남는다.

## 실패와 DLQ

InvalidEvent는 즉시 DLQ, 명시적인 RetryableProcessingError는 최초 처리 뒤 1·2·4·8·10초로
최대 5회 재시도 후 DLQ다. DeliveryError와 분류하지 않은 설정/프로그래밍 오류는 예외를 전파한다.
DLQ ack 이전에는 offset을 진행하지 않는다.
완료 Outbox의 전송 실패는 원본 요청을 DLQ로 보내지 않는다. 발행 대기를 유지하고
1·2·4·8·16·30초(이후 30초) 간격으로 재시도한다. 빈 배치는 1초 대기한다. 모델 업무 실패를 FAILED 결과로 저장·발행한 경우는 정상 완료다.

DLQ는 `<원본 topic>.graph-rag.dlq`다. 원본 key·value·ce_*를 보존하고 기존 kafka_dlt-* 진단을 교체한다.
원본 partition/offset은 signed int32/int64 big-endian, 나머지 핵심 진단 헤더는 UTF-8이다.
전송 partition은 원본 번호로 강제하지 않는다. 원문·자격증명·외부 예외 메시지는 로그·진단에 넣지 않는다.

## 검증과 한계

기본 전체 검증은 프로젝트 가상환경의 `.venv/bin/python -m pytest -q`다.
실제 Kafka·Gremlin은 일회용 환경을 지정한다. 전체 Content 왕복은 [전체 연동 실행](../../tests/fullstack/README.md)을 따른다.
이 테스트는 실제 Java Content HTTP·PostgreSQL·Outbox relay·Kafka·GraphRAG·Gremlin·S3 호환 서버·
Content 완료 listener를 통과한다. 모델은 NoChangeModel로 대체한다.
로컬 Gremlin/S3 호환 검증은 AWS Neptune 동시성·S3 IAM·실제 원격 모델 품질·운영 rollout의 증명이 아니다.
배포는 [운영 설정](../../deploy/README.md)을 따른다.
