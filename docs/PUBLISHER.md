# Kafka 발행 — GraphRAG와 Content

> **책임:** GraphRAG의 완료 이벤트 발행과, 입력을 공급하는 Content의 Outbox 발행 방식을 설명한다.
>
> **확인할 때:** Kafka publisher를 구현하거나 결과 저장·발행·재실행의 경계를 맞출 때.

## 현재 구현과 기준

GraphRAG는 `RefreshCompletionPublisher` 인터페이스와 `RefreshService`의 저장·발행 조율,
완료 payload 변환과 Neptune 완료 Inbox를 구현했다. 실제 Kafka publisher는 아직 연결하지 않았다.
진행 상태·lease를 저장하는 별도 작업 저장소는 두지 않는다.
Content도 현재 `main`에는 Kafka publisher·consumer가 없다. `build.gradle`에 Kafka starter가
없고, `application.yaml`에 Kafka 설정이 없으며, README는 이벤트 발행을 미구현으로 분류한다.

다음 자료는 적용 범위가 다르다.

| 자료 | 이 문서에서 사용하는 범위 |
| --- | --- |
| [메시지 계약 초안](reference/message-contract.md) | 이벤트 이름·헤더·payload·발행 방향의 설계 |
| [GitOps 토픽 선언](../../loresentry-gitops/workload/base/kafka/topics.yaml) | 저장소에 선언된 실제 토픽 이름·파티션·보존 설정. 운영 클러스터 상태는 별도 확인 필요 |
| [Content main](https://github.com/soma-lorekeeper/loresentry-content/tree/83094c1b4804768d4be58fbc085f5e3ea24ff9d8) | 현재 Content 코드와 기본 Outbox 테이블 |
| [Content issue/#17의 SQL](https://github.com/soma-lorekeeper/loresentry-content/blob/c2389bba5da0f117d325b86d70a90171f846b6f6/src/main/resources/db/migration/V12__outbox_relay_and_inbox.sql) | 아직 main에 없는 relay·Inbox 준비 스키마. 실행 중인 publisher의 근거가 아님 |

## 서비스별 발행 대상

| 발행 주체 | 토픽 | 이벤트 | 소비 주체 | Kafka key |
| --- | --- | --- | --- | --- |
| Content | `content.project.changed.v1` | `FileChanged`, `ProjectDeleted` | GraphRAG, search | `project_id` |
| Content | `content.graph-refresh.requested.v1` | `GraphRefreshRequested` | GraphRAG | `project_id` |
| GraphRAG | `graph-rag.graph-refresh.completed.v1` | `GraphRefreshCompleted` | Content | `project_id` |

GraphRAG는 소비 실패를 격리할 때 다음 두 DLQ에도 발행한다. 이는 완료 통지가 아니라
원본 메시지의 실패 기록이다. 조건과 offset 처리 순서는 [소비 문서](CONSUMER.md)를 따른다.

- `content.project.changed.v1.graph-rag.dlq`
- `content.graph-refresh.requested.v1.graph-rag.dlq`

## Content의 Outbox 발행 방식

### 현재 테이블과 별도 브랜치의 준비

Content main의 [V1 스키마](../../loresentry-content/src/main/resources/db/migration/V1__create_content_schema.sql)는
`outbox_events`에 `id`, `event_type`, `aggregate_id`, `payload`, `published_at`, `created_at`을 둔다.
아직 도메인 변경과 함께 이벤트를 INSERT하는 코드나 relay가 없다.

`issue/#17`의 SQL은 다음 컬럼과 인덱스를 추가한다.

| 항목 | 목적 |
| --- | --- |
| `seq BIGINT GENERATED ALWAYS AS IDENTITY` | relay의 INSERT 순서 정렬 값 |
| `event_key NOT NULL` | Kafka key를 payload 파싱 없이 읽기 |
| `traceparent` | 요청이 끝난 뒤 실행하는 relay에 추적 문맥 전달 |
| `attempts`, `last_error` | 발행 실패 횟수와 마지막 원인 기록 |
| `quarantined_at` | 재시도로 해결되지 않는 행 격리 |
| `(seq)` 부분 인덱스 | `published_at IS NULL AND quarantined_at IS NULL`인 발행 대기 행 조회 |

SQL 주석은 실패 횟수만으로 격리하지 않도록 설명한다. broker 장애 중에는 정상 이벤트도
반복 실패하므로, 오류 종류와 행 자체의 문제를 구분해야 한다.
`seq`는 INSERT 순서이며 트랜잭션 커밋 순서를 자동 보장하지 않는다. 같은 프로젝트의
동시 트랜잭션·relay 병렬 실행에서 순서를 지키는 방법은 추가 구현·검증이 필요하다.

이 브랜치의 마이그레이션 번호는 `V12`지만 main에는 이미 `V12__manuscript_folder_first.sql`과
V13이 있다. SQL을 통합할 때 번호 충돌부터 해소해야 하며, 그대로 적용된 것으로 읽지 않는다.

### 예정된 처리 순서

공용 [Outbox 발행 방식 문서](../../docs/OUTBOX_CDC_VS_POLLING.md)는 폴링 relay를 우선하는 설계다.
현재 코드가 이 흐름을 실행하고 있다는 뜻은 아니다.

1. 문서·관계·프로젝트 변경과 Outbox INSERT를 같은 Content PostgreSQL 트랜잭션에서 커밋한다.
2. relay가 미발행·미격리 행을 순서대로 조회한다. 같은 key의 앞 행이 실패하면 뒤 행을 먼저 보내지 않는 처리가 필요하다.
3. `event_type`에 맞는 토픽으로 `event_key`와 JSON payload를 보낸다.
4. Kafka 발행 성공 확인 후에만 `published_at`을 갱신한다.
5. 발행 실패를 기록하고, 일시 장애는 재시도하며 복구 불가능한 행은 격리·알림 대상으로 둔다.

Kafka 발행과 PostgreSQL 갱신은 하나의 트랜잭션이 아니다. 발행 성공 후 `published_at` 기록
전에 중단되면 같은 `ce_id`를 다시 발행할 수 있다. 소비자의 Inbox가 이 중복을 처리해야 한다.
producer idempotence만으로 relay 재시작 뒤의 중복을 제거할 수 있다고 가정하지 않는다.

`GraphRefreshRequested`는 입력 본문을 Kafka에 복제하지 않는다. 계약 초안은 입력 S3 저장을
먼저 끝내고 `refresh_runs`와 Outbox를 함께 커밋하는 흐름을 제시한다. S3와 DB의 원자성,
DB 롤백 뒤 남는 객체 정리, 실제 요청 생성 코드는 아직 구현되지 않았다.

## GraphRAG의 완료 발행

[RefreshService.run](../app/refresh/service.py)은 주입받은 어댑터를 다음 순서로 호출한다.

1. 완료 Inbox를 확인한다. 완료 기록이 있으면 저장된 완료 값을 반환한다.
2. 결과가 없을 때 계산한다. 결과가 있으면 재사용해 모델 재호출을 줄인다.
3. 결과를 S3에 멱등적으로 저장한다.
4. `publisher.publish(completion)`을 호출한다.
5. 발행 성공 확인 후 완료 Inbox를 기록한다. 서비스가 반환한 뒤 consumer가 offset을 커밋한다.

`publish()`의 성공은 Kafka 전달 확인을 받은 시점으로 구현해야 한다. 로컬 producer 큐에
넣기만 하고 반환하면 서비스가 완료로 기록한 뒤 메시지를 잃을 수 있다.
현재 Kafka 어댑터의 ack·타임아웃·재시도 설정은 없다.

발행 후 Inbox 기록 전에 실패하면 재실행에서 결과를 재사용하고 완료 이벤트를 다시
발행할 수 있다. 완료 Inbox가 있으면 S3·모델·발행 없이 완료 값을 반환한다.
요청 식별·동시 실행·복구의 상세 계약은 [완료 Inbox](implementation/REFRESH_INBOX.md)를 따른다.
Content는 `ce_id` 중복 처리와 함께 `request_id` 기준의 업무 중복도 처리해야 한다.
완료 이벤트 ID를 재시도에서 재사용할지, 영속적으로 어디에 기록할지는 연동 전에 결정해야 한다.

### 완료 payload

[completion_to_payload](../app/refresh/serialization.py)는 다음 필드를 만든다.

| 필드 | 의미 |
| --- | --- |
| `project_id`, `request_id` | 프로젝트와 최신화 요청의 식별자 |
| `outcome` | 내부 `PROPOSED`·`NO_CHANGE`는 `SUCCEEDED`, 실패는 `FAILED` |
| `result` | 저장된 결과의 `bucket`·`key`. 성공이면 반드시 존재 |
| `error` | 실패의 `code`·`message`. 성공이면 null |
| `prompt_version` | 결과 생성에 사용한 프롬프트 버전 |

실패 결과도 저장된 경우 `result`가 있을 수 있다. 완료 이벤트에 제안 본문을 넣지 않는다.
`SUCCEEDED`는 승인 전 제안 생성 완료이며 Content 문서나 확정 그래프 적용 완료가 아니다.
결과 JSON은 [S3 입출력 계약](S3_IO_CONTRACT.md)을 따른다.

## 메시지 포장과 미확정 사항

계약 초안은 key를 UTF-8 `project_id`, value를 UTF-8 JSON으로 두고 메타데이터를 헤더에 둔다.
필수 헤더는 `ce_specversion=1.0`, `ce_id`, `ce_type`, `ce_source`, `ce_time`,
`content-type=application/json`이며 `traceparent`는 선택이다.
Content SQL은 `id → ce_id`, `event_type → ce_type`, `created_at → ce_time`, `payload → value`로 설명한다.
GraphRAG publisher는 완료 payload 변환 외에 이 헤더와 key를 별도로 만들어야 한다.

프로젝트 변경 토픽과 그 DLQ의 선언상 메시지 한도는 4 MiB다. Content 발행과 GraphRAG DLQ
발행에서 대응하는 클라이언트 한도를 맞춰야 한다. 본문 토픽의 압축은 계약 초안상 zstd이며
실제 producer 설정·Python 클라이언트 지원은 아직 확인할 구현이 없다.

연동 전에는 이벤트 JSON Schema, relay의 key별 순서·행 잠금·병합 처리, producer 설정,
발행 확인 실패 복구, 완료 이벤트 ID 보존을 확정해야 한다. `reference/outbox.md`의 주문 예시는
구현 참고이며 Content의 실제 실행 코드나 운영 설정이 아니다.
