# Kafka 발행 — GraphRAG와 Content

> **책임:** Content의 실제 relay를 참고해 GraphRAG 완료·DLQ publisher의 구현 계약을 정한다.
>
> **확인할 때:** Kafka 발행 어댑터, 전달 확인, 재발행과 완료 Inbox를 구현할 때.

## 적용 기준과 구현 상태

Content의 Outbox 기반은 `44e074e`에서 통합됐으며 현재 `ContentEvents`와 요청·완료 업무 연결을 추가했다. [Outbox 패키지](../../loresentry-content/src/main/java/com/loresentry/content/outbox/),
[Inbox 패키지](../../loresentry-content/src/main/java/com/loresentry/content/inbox/),
[Kafka 설정](../../loresentry-content/src/main/resources/application.yaml),
[V14 마이그레이션](../../loresentry-content/src/main/resources/db/migration/V14__outbox_relay_and_inbox.sql)을 기준으로 한다.
README의 이벤트 발행 미구현 표기는 이 코드보다 뒤처져 있다.

Content에는 relay·메시지 변환·발행 확인·실패 기록·정리 작업과 도메인 변경의 Outbox INSERT가 있다.
완료·사용자 삭제 업무 처리도 연결했으며 listener 활성화는 별도 설정이다.
요청 API는 입력 S3 저장 뒤 refresh_runs와 GraphRefreshRequested를 함께 commit한다.

GraphRAG는 `RefreshCompletionPublisher`, `completion_to_payload`, 결과 저장·Inbox/Outbox·relay
조율과 실제 `KafkaPublisher`의 완료·DLQ 전송을 구현했다.
[송수신 기반](implementation/KAFKA_TRANSPORT.md)에 조립 방법과 검증 범위를 정리했다.
기존 `FakeProducer`는 별도의 sandbox이며 완료 이벤트를 발행하지 않는다.
업무 runtime은 완료 Outbox relay를 별도 스레드로 실행한다. 업무별 enabled는 기본 false이며
의존성 설정과 배포 조건은 [운영 안내](../deploy/README.md)를 따른다.

## 발행 대상

| 발행자 | 토픽 | 이벤트 | key | subject |
| --- | --- | --- | --- | --- |
| Content | `content.project.changed.v1` | `FileChanged` | `project_id` | `file_id` |
| Content | `content.project.changed.v1` | `ProjectDeleted` | `project_id` | `project_id` |
| Content | `content.graph-refresh.requested.v1` | `GraphRefreshRequested` | `project_id` | `request_id` |
| GraphRAG | `graph-rag.graph-refresh.completed.v1` | `GraphRefreshCompleted` | `project_id` | `request_id` |

Content는 `aggregate_id`를 `ce_subject`로 옮긴다. 위 subject 의미대로 Outbox를 생성하는
책임은 후속 도메인 생산 코드에 있다. 완료 subject는 Content의 `InboxMessages`가
`request_id`와 대조하므로 프로젝트 ID를 넣으면 거절된다.

GraphRAG는 `content.project.changed.v1.graph-rag.dlq`와
`content.graph-refresh.requested.v1.graph-rag.dlq`에도 실패 레코드를 발행한다.
원본 보존·실패 헤더·offset 규칙은 [소비 문서](CONSUMER.md#재시도와-dlq)를 따른다.
토픽·파티션·보존 한도는 [GitOps 선언](../../loresentry-gitops/workload/base/kafka/topics.yaml)이 기준이며
실제 운영 적용 상태는 별도 확인한다.

## Content에서 참고할 동작

| 구성 | 실제 코드의 동작 | GraphRAG에 적용할 원칙 |
| --- | --- | --- |
| `OutboxMessages` | 종류별 topic, key, UTF-8 payload·헤더 변환 | wire 변환을 업무 서비스와 분리 |
| `OutboxRepository` | 트랜잭션 advisory lock, `seq` 정렬, `FOR UPDATE SKIP LOCKED` | 저장소별 원자성 경계를 명시 |
| `OutboxPublisher` | 최대 50행, payload 합계가 16 MiB에 닿는 행까지 읽음. 전송 후 ack 확인한 행만 발행 표시 | 큐에 넣은 시점과 전달 완료 구분 |
| 실패 처리 | 알 수 없는 종류·크기 초과 격리. 그 밖의 실패는 미발행 유지, 5회 이상 실패 경고 | 인프라 장애를 메시지 문제로 단정하지 않음 |
| `OutboxRelay` | 1초 fixed delay, 잔여 작업 연속 처리, 실패 backoff 1·2·4·8·10초 | 제한된 실행·대기, 종료 시 새 회차 중단 |
| `OutboxPurge` | 발행 후 3일 지난 행을 매일 04:00 Asia/Seoul에 삭제 | 복구에 필요한 영속 자료의 보존 관리 |

Kafka ack 뒤 DB commit 전에 중단되면 같은 `ce_id`가 재발행될 수 있다.
`enable.idempotence`가 프로세스 재시작이나 DB와 Kafka 사이의 원자성까지 제공하지는 않는다.

relay 자체는 조회된 행의 `seq` 순서로 전송한다. ContentEvents를 사용하는 업무 쓰기는
사용자별 transaction lock으로 INSERT와 commit 순서를 맞춘다. 직접 SQL 쓰기는 이 계약을 우회한다.
이미 비동기로 보낸 뒤 앞 행이 실패하고 뒷 행이 성공할 수도 있다. key별 실패 장벽·최신 행 병합은
현재 코드에 없다. 같은 key만으로 확정 상태의 전역 순서를 보장한다고 해석하지 않는다.

## GraphRAG 완료 발행 순서

[RefreshService.run](../app/refresh/service.py)의 기존 복구 흐름을 유지한다.

1. 완료 Inbox가 있으면 완료 값을 반환한다. S3·모델·publisher를 호출하지 않는다.
2. 저장 결과가 있으면 재사용하고, 없으면 계산한다. 실행 문맥과 결과는 불변 저장한다.
3. 결과 S3 저장을 확인한다.
4. Neptune의 같은 Inbox vertex에 완료 값과 발행 대기를 한 트랜잭션으로 저장한다.
5. 서비스 반환 후 consumer가 해당 요청 offset을 commit한다.
6. 별도 `RefreshOutboxRelay`가 대기 이벤트를 읽고 Kafka 전달 확인 후 발행 완료로 표시한다.
   요청 offset과 전송은 독립적으로 진행한다.

Neptune Inbox가 Outbox 역할도 맡으며 PostgreSQL을 추가하지 않는다.
이벤트 ID·payload·발생 시각은 재시도에도 유지한다. 전송 실패는 대기를 유지하고
최대 30초 backoff로 계속 재시도한다. Kafka ack 뒤 DB 표시 전 중단은 중복 발행을 허용한다.
Inbox가 있어도 relay는 미발행 건을 처리한다. 중복 요청은 발행 완료를 대기로 되돌리지 않는다.
기존 pending 속성이 없는 Inbox는 이미 발행한 기록으로 유지한다.
S3·Neptune·Kafka·offset 전체는 별도 트랜잭션이며 실행 lease는 없다.
[완료 Inbox·Outbox](implementation/REFRESH_INBOX.md)의 복구·동시 실행 한계를 따른다.

### 완료 JSON과 헤더

value는 [completion_to_payload](../app/refresh/serialization.py)를 사용한다.
`project_id`, `request_id`, `outcome`, `result`, `error`, `prompt_version`을 보내며
제안 본문은 넣지 않는다. `PROPOSED`·`NO_CHANGE`는 `SUCCEEDED`다.
성공이면 `result.bucket`·`result.key`가 있어야 하고 실패면 안전한 `error.code`·`error.message`가
있어야 한다. 저장된 실패 결과는 result 위치도 보낼 수 있다.
`SUCCEEDED`는 승인 전 제안 생성 완료이며 Content 원본·확정 그래프 반영 완료가 아니다.

| 헤더 | 완료 이벤트 값 |
| --- | --- |
| `ce_specversion` | `1.0` |
| `ce_id` | 완료 이벤트 UUID. 아래 안정적 ID 정책 사용 |
| `ce_type` | `GraphRefreshCompleted` |
| `ce_source` | `/graph-rag` |
| `ce_subject` | payload의 `request_id` |
| `ce_time` | UTC RFC 3339, 밀리초 3자리 |
| `content-type` | `application/json` |
| `traceparent` | 유효한 추적 문맥이 있을 때만 전달 |

key와 헤더 값은 UTF-8, value는 snake_case UTF-8 JSON이다. 헤더 이름은 한 번만 넣는다.
Content는 모든 Outbox 이벤트에 `ce_sequence=seq`를 붙이지만 완료 이벤트에는 필요하지 않다.

**구현된 완료 ID:** namespace를
`uuid5(NAMESPACE_URL, "urn:loresentry:graph-refresh-completed:v1")`로 고정하고,
그 namespace의 UUIDv5로 `{project_id}:{request_id}`에서 결정적으로 만든다.
같은 요청은 저장된 동일 결과만 재발행한다. namespace를 재배포마다 바꾸지 않는다.
`ce_time`은 각 전송 시각으로 두고 순서·중복 키로 쓰지 않는다.
이 방식은 기존 `Completion` 저장 형식을 바꾸지 않으면서 재시작 뒤에도 ID를 유지한다.
Content는 `request_id`의 GENERATING 상태를 조건으로 결과를 수용해 업무 중복도 차단한다.
같은 요청 ID로 결과를 새로 계산하는 정책 변경은 허용하지 않고 새 요청을 만든다.

## Python publisher 구현안

`app/kafka/messages.py`에 순수 메시지 변환을, `app/kafka/publisher.py`에
전달 확인을 수행하는 IO 어댑터를 구현했다. 완료 publisher와 DLQ publisher는 같은 전송 도구를
사용하되 메시지 생성 규칙을 분리한다. Kafka 객체를 refresh rules나 내부 모델에 전달하지 않는다.

`produce()`는 비동기다. 레코드별 `on_delivery` 결과를 보관하고 producer의 `poll()`을
호출하며 해당 레코드의 성공까지 기다린다. 큐 포화 `BufferError`는 기한 안에서 poll 후 재시도한다.
실패 callback·대기 기한 초과는 예외로 전파한다. `flush()` 반환만으로 개별 성공을 판정하지 않는다.
이 API 동작은 [Confluent Python 공식 API](https://docs.confluent.io/platform/current/clients/confluent-kafka-python/html/index.html#producer)를 기준으로 한다.

초기에는 업무 worker별 producer 하나를 소유해 작업 스레드에서 발행·poll을 수행한다.
consumer poll과 producer poll은 별개다. callback에서 consumer commit을 호출하지 않는다.
공유 producer를 도입한다면 레코드별 완료 판정과 스레드 소유권 테스트를 먼저 추가한다.

| 항목 | Content 현재 값 | GraphRAG 초기 구현안 |
| --- | --- | --- |
| 전달 확인 | `acks=all`, idempotence true | `acks=all`, `enable.idempotence=true` |
| 압축 | zstd | `compression.type=zstd` |
| 크기 | `max.request.size=4194304` | `message.max.bytes=4194304` — 큰 원본의 DLQ 포함 |
| 요청 대기 | `request.timeout.ms=10000` | 같은 이름·값 |
| 총 전달 기한 | `delivery.timeout.ms=20000` | `message.timeout.ms=20000` |
| 애플리케이션 대기 | 25초 | 큐 진입 대기 포함 단조 시계 기준 25초 |

Python 설정명과 전달 기한·압축 옵션은 [librdkafka 설정 문서](https://docs.confluent.io/platform/current/clients/librdkafka/html/md_CONFIGURATION.html)를 따른다.
Java의 `max.block.ms`·`max.request.size`를 Python에 그대로 넘기지 않는다.
타임아웃은 미전달의 증명이 아니므로 같은 ID 재발행을 허용한다.
DLQ 헤더가 추가되면 원본 크기 한도 근처에서 broker가 거절할 수 있다.
이를 테스트하고, 한도 초과 시 원본 offset을 유지하며 경고한다. 본문을 잘라서 보내지 않는다.

종료 시 새 작업을 막고 진행 중 작업·전송을 정해진 종료 예산 안에서 기다린다.
업무 runtime 종료 대기는 기본 360초다. Pod grace period는 이보다 길게 설정한다.
완료 발행이 확인되지 않으면 Outbox 대기를 유지한다. DLQ는 전달 확인 전 원본 offset을 진행하지 않는다.

## 구현 검증

[검증 계획](implementation/TEST_PLAN.md#kafka-업무-연동-추가-검증)의 전송 성공·실패·중복·
재시작 시나리오를 적용한다. 실제 Content listener와 결과 수용까지 통과하는 로컬 왕복 테스트를 포함한다.
운영 활성화 조건은 [배포 안내](../deploy/README.md)를 따른다.
