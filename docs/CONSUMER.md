# Kafka 소비 — GraphRAG와 Content

> **책임:** GraphRAG가 소비할 토픽, Content가 완료를 받는 방식, 중복·실패·offset 처리 경계를 설명한다.
>
> **확인할 때:** Kafka consumer를 구현하거나 Content와 요청·결과 계약을 맞출 때.

## GraphRAG의 구독 대상

GraphRAG가 구독할 업무 토픽은 다음 두 개다. 이름과 파티션 수는
[GitOps 토픽 선언](../../loresentry-gitops/workload/base/kafka/topics.yaml)에 있고,
이벤트·payload 의미는 [메시지 계약 초안](reference/message-contract.md)에 있다.
Git 선언은 운영 중인 이벤트 발행이나 consumer 실행을 보증하지 않는다.

| 소비 토픽 | 이벤트 | 처리 목적 | key | 파티션 |
| --- | --- | --- | --- | --- |
| `content.project.changed.v1` | `FileChanged`, `ProjectDeleted` | Content 확정 상태를 그래프 투영본에 반영 | `project_id` | 6 |
| `content.graph-refresh.requested.v1` | `GraphRefreshRequested` | S3 입력을 읽어 승인 전 갱신안 생성 | `project_id` | 3 |

`graph-rag.graph-refresh.completed.v1`은 GraphRAG의 발행 대상이며 Content가 소비한다.
`auth.user.deleted.v1`도 Content가 소비하는 토픽이다. GraphRAG는 사용자 삭제를
Content의 `ProjectDeleted`로 전달받는 설계이므로 auth 토픽을 직접 구독할 필요가 없다.
옛 문서의 `content.file.changed.v1`은 잠정 이름이며 현재 GitOps 선언의 소비 대상으로 사용하지 않는다.
DLQ는 평상시 업무 구독과 구분하고, 재처리 절차를 정한 별도 소비 경로로 다뤄야 한다.

## 현재 구현 상태

GraphRAG에는 `confluent-kafka` 기반 [polling worker](implementation/KAFKA_POLLING.md)가 있다.
실제 Kafka 수신·수동 commit에 fake consumer handler·producer를 연결한 sandbox 실행 경로다.
Content 업무 이벤트 변환과 그래프·refresh 서비스 연결은 아직 없다. 공통 그래프 처리기·Neptune 그래프/Inbox 저장
어댑터와 fake consumer는 [그래프 투영 구현](implementation/GRAPH_PROJECTION.md)에 있다.
fake의 수신·offset 상태는 메모리에만 있으며 실제 Kafka 연동은 아니다.
갱신안 생성 서비스·S3 저장·완료 payload 변환과 Neptune 완료 Inbox는 구현됐지만 Kafka
진입점은 미연결이다. 별도 작업 상태 저장소 없이 [완료 Inbox](implementation/REFRESH_INBOX.md)와
S3 재사용으로 복구한다. 완료 발행의 구현 범위는 [발행 문서](PUBLISHER.md)를 따른다.

Content [main 코드](https://github.com/soma-lorekeeper/loresentry-content/tree/83094c1b4804768d4be58fbc085f5e3ea24ff9d8)에도
Kafka listener·Kafka starter·consumer 설정이 없다.
[issue/#17의 준비 SQL](https://github.com/soma-lorekeeper/loresentry-content/blob/c2389bba5da0f117d325b86d70a90171f846b6f6/src/main/resources/db/migration/V12__outbox_relay_and_inbox.sql)은
`inbox_events`를 추가하지만 아직 main에 통합되지 않았다. 다음 Content 처리 흐름은
테이블·계약·참고 예시에 근거한 설계이며 실행 중인 consumer 설명이 아니다.

## 변경 이벤트 소비

`FileChanged`는 변경 diff가 아닌 문서의 현재 상태 스냅샷을 전달하는 초안이다.
생성·수정·휴지통 이동·복원·영구 삭제를 구분하고 `changed_fields`를 처리 판단의 힌트로 둔다.
현재 초안에는 `file_id`, `version`, `change_type`, `file`, `relations` 등이 있다.

consumer는 헤더·payload·프로젝트 key를 검증하고 이벤트 ID 중복을 확인한 뒤,
새 이벤트에 대해서만 문서 노드·관계 또는 삭제를 반영한다. 휴지통 문서는 조회·탐색에서
제외하고 복원 시 상태를 되돌리는 설계다. `ProjectDeleted`는 프로젝트의 투영 데이터를 지운다.
처리 성공이나 안전한 중복 판정 뒤에만 offset을 진행해야 한다.

같은 프로젝트의 문서 변경과 프로젝트 삭제는 같은 토픽·key에 두어 순서 역전을 줄인다.
이 순서는 publisher가 올바른 순서로 보내고 consumer가 파티션 내부의 처리 순서를 지킬 때
성립한다. 다른 토픽 사이의 순서나 DB 커밋 순서는 Kafka key만으로 보장되지 않는다.

다음 차이는 구현 전에 확정해야 한다.

- **순서 값:** 초안의 `version`은 아직 결정되지 않았다. `revision_no`는 조건부 문서 저장 번호이며 이름·폴더·휴지통 등 모든 이벤트의 순서를 표현한다고 가정할 수 없다. relay의 `seq`도 문서별 version과 같은 값이 아니다.
- **관계 구조:** 초안은 양방향 두 행을 설명하지만 Content main의 [V10](../../loresentry-content/src/main/resources/db/migration/V10__one_record_per_relation.sql)은 무방향 문서 쌍당 한 행이다. 이벤트의 관계 ID·상대 분류별 key·양쪽 문서 이벤트 발행 방식은 현재 저장 구조에 맞춰야 한다.
- **본문만 변경:** `changed_fields=["BODY"]`를 건너뛰어도 되는지는 그래프의 담당 데이터에 달린다. 본문에서 생성하는 투영 데이터가 생기면 일괄 skip 규칙을 사용할 수 없다.
- **중복과 그래프 원자성:** 공용 [Inbox 계약](../../docs/GRAPH_INBOX_PATTERN.md)에 따라 Neptune 내 기록·변경을 요청 하나로 묶는 어댑터가 있다. 로컬 Gremlin에서 확인한 범위와 AWS Neptune의 추가 검증은 [구현 문서](implementation/GRAPH_PROJECTION.md)를 따른다.

## 갱신안 요청 소비

`GraphRefreshRequested`는 `request_id`, `project_id`, 변경 문서 목록 `files`, 입력 위치
`input.bucket`·`input.key`를 전달하는 초안이다. 입력 본문은 S3에 있고 Kafka에는 넣지 않는다.
원고와 확정 Content 문서를 직접 변경하는 요청이 아니라 갱신안을 생성하는 요청이다.

consumer를 연결할 때 필요한 순서는 다음과 같다.

1. 헤더·payload와 프로젝트 key를 검증하고 요청 ID를 식별한다.
2. 이벤트를 내부 `RefreshRequest`로 변환하고 완료 Inbox를 확인한다. 이미 완료됐으면 재실행하지 않는다.
3. 입력 S3 객체와 프로젝트·문서·revision·상태를 검증한다.
4. 저장된 실행 스냅샷·결과를 재사용하거나 새 갱신안을 생성한다.
5. 결과 저장·완료 이벤트 발행 확인·완료 Inbox 기록을 끝낸 뒤 요청 offset을 진행한다.

현재 이벤트 초안만으로 내부 요청을 완전히 조립할 수는 없다.
[S3 계약](S3_IO_CONTRACT.md)은 입력 바이트 전체의 SHA-256인 `input_fingerprint`를 요구하고,
내부 요청에는 대상 설정·시드·선택 설정 등이 필요하다. fingerprint 전달 위치와
요청 설정의 공급·기본값 정책과 Content 조회에 필요한 신뢰한 `user_id` 전달을 Content와 확정해야 한다.
`files[].file_id → changed_documents[].document_id`, `input → input_ref`의 필드 변환도 필요하다.
입력 한도와 지원 스키마는 S3 계약을 기준으로 검증한다.

모델 처리는 길어질 수 있다. 현재 sandbox runtime은 `confluent-kafka`, 작업 스레드 하나,
처리 중 전체 할당 pause와 지속 poll, 재할당 세대 확인, 완료 레코드만 수동 commit을 사용한다.
운영 consumer group과 파티션별 병렬 처리·재시도 정책은 업무 연결 때 확정해야 한다.
일부 작업을 뒤에서 먼저 완료해도 미완료 offset을 넘어 commit해서는 안 된다.
결과를 저장하기 전 단순 수신 확인만으로 offset을 commit하는 구성은 작업 유실을 만들 수 있다.
영속 작업 큐에 안전하게 넘긴 뒤 commit하는 대안은 별도의 복구·멱등성 계약이 필요하다.

## Content가 소비하는 이벤트와 Inbox 방식

| Content 소비 토픽 | 이벤트 | 계약상 업무 |
| --- | --- | --- |
| `graph-rag.graph-refresh.completed.v1` | `GraphRefreshCompleted` | 요청 상태 갱신, S3 결과를 승인 전 제안으로 수용 |
| `auth.user.deleted.v1` | `UserDeleted` | 사용자 소유 데이터 삭제, 프로젝트별 `ProjectDeleted` 발행 |

계정 삭제의 현재 실행 경로는 Gateway의 동기 HTTP 호출이다.
`auth.user.deleted.v1` 선언만으로 auth가 이미 이벤트를 발행한다고 해석하지 않는다.

준비 SQL의 Inbox는 다음 구조다.

| 컬럼 | 의미 |
| --- | --- |
| `consumer_name` | 논리 처리기 이름. 서로 다른 처리기의 중복 판정 구분 |
| `event_id UUID` | Kafka 헤더 `ce_id` |
| `processed_at` | 처리 시각과 정리 기준 |
| 복합 기본 키 | `(consumer_name, event_id)`로 같은 처리기의 중복 차단 |

예정된 PostgreSQL 처리 방식은 Inbox INSERT와 업무 변경을 같은 트랜잭션에 둔다.
`ON CONFLICT DO NOTHING`으로 이미 처리한 이벤트면 업무를 반복하지 않고 정상 종료한다.
새 이벤트의 업무 반영이 실패하면 Inbox INSERT도 롤백하고 재전달을 허용한다.
offset은 DB 커밋 이후 진행한다. 실제 listener·트랜잭션 서비스·정리 작업은 아직 없다.
Spring 참고 예시의 `AckMode`·group 이름·backoff 값을 Content 운영 설정으로 사용하지 않는다.

### GraphRefreshCompleted 수신

Content는 `project_id`·`request_id`를 자신의 최신화 실행과 대조하고, 완료 상태와
`prompt_version`을 기록하며 결과 위치에서 [refresh-result-v3](S3_IO_CONTRACT.md)를 읽는 연동이 필요하다.
완료 상태 `SUCCEEDED`에는 제안 있음과 변경 없음이 모두 포함된다. 결과 적용은 사용자 승인 이후다.
같은 요청의 완료가 다시 와도 제안·상태를 중복 생성하지 않아야 한다.

Inbox 트랜잭션은 S3 읽기·삭제를 원자적으로 묶지 않는다. 결과 읽기 실패, 이미 삭제된 프로젝트,
만료된 요청, 늦게 도착한 완료, 서로 다른 이벤트 ID로 재전달된 같은 요청을 어떻게 처리할지
결정해야 한다. 계약 초안은 삭제된 프로젝트의 늦은 결과를 Content가 정리하는 흐름을 제시하지만
현재 구현된 처리기는 없다.
기존 `refresh_document_drafts`는 기존 대상 문서를 참조하므로, 신규 문서 후보와 임시 ID를 담는
v3 결과 수용 방식도 구현 전에 정해야 한다.

## 실패·DLQ·offset

계약 초안은 일시 장애를 재시도하고, 복구 불가능한 payload·알 수 없는 enum 또는 재시도 소진을
소비자별 DLQ로 격리한다. 관심 없는 `ce_type`은 경고 후 건너뛰는 설계다.
구체적인 재시도 횟수·시간 예산·영속 실패 기록은 아직 서비스 설정으로 구현되지 않았다.

| 소비 주체 | 원본 토픽 | 실패 발행 대상 |
| --- | --- | --- |
| GraphRAG | `content.project.changed.v1` | `content.project.changed.v1.graph-rag.dlq` |
| GraphRAG | `content.graph-refresh.requested.v1` | `content.graph-refresh.requested.v1.graph-rag.dlq` |
| Content | `graph-rag.graph-refresh.completed.v1` | `graph-rag.graph-refresh.completed.v1.content.dlq` |
| Content | `auth.user.deleted.v1` | `auth.user.deleted.v1.content.dlq` |

DLQ 발행 성공 확인 전에 원본 offset을 진행하지 않는다. 실패 발행 자체가 실패하면 원본을
유지해야 한다. key·원본 헤더·오류 정보는 메시지 계약 초안의 DLQ 규약을 따른다.
DLQ 전송 후 중단되면 DLQ 메시지도 중복될 수 있다.

프로젝트 변경 원본은 3일 또는 파티션별 512 MiB, GraphRAG의 변경 DLQ는 7일 또는
파티션별 256 MiB로 제한된다. 요청 원본은 7일, 요청 DLQ는 30일이다.
본문 원본·DLQ는 닫힌 segment 정리에 의해 실제 보존 시간이 더 길어질 수 있다.
S3 입력의 만료와 Kafka DLQ 보존은 별개이므로 DLQ가 남아 있어도 최신화를 재실행할 수 없는
경우가 있다. 보존 범위를 넘어 이벤트가 유실되면 Content 원본으로 다시 동기화하는 절차가 필요하다.

## 구현 전 확인 항목

- Content 이벤트 JSON Schema와 현재 관계 구조·이벤트 순서 값 확정
- 요청 이벤트와 내부 `RefreshRequest`의 필드·해시·설정 연결
- 구현한 GraphRAG 완료 Inbox·Neptune 적용과 실제 consumer offset·rebalance의 복구 경계
- Content의 완료 listener, S3 v3 결과 수용·승인·삭제 처리
- consumer group, poll·rebalance·shutdown, 재시도·DLQ·보존 기간 초과 재동기화 검증

개념·Java 예시는 [Inbox 참고 자료](reference/inbox.md), 발행과 완료 재시도는
[발행 문서](PUBLISHER.md)를 참고한다.
