# Lore Sentry 메시지 계약 초안

문서·본문·관계·revision_no의 의미는 [Content 기준 용어집](../domain-glossary.md)을 따른다. 이벤트의 FileChanged·file_id·files는 Content API 이름을 유지하지만 모두 도메인 문서를 가리킨다. 아래 payload는 구현 전 계약 초안이며 기존 배포 스키마의 변경 완료를 뜻하지 않는다.

이벤트·topic·키·value·header·호환성·크기·S3 Claim Check·DLQ 규약을 모은다. 원문에 적힌 설계안이며 배포된 설정을 확인한 문서는 아니다.

관련 문서: [Outbox 구현 예시](outbox.md), [Inbox 구현 예시](inbox.md), [미결정 사항](README.md#미결정과-미작성-항목).

## Event

| 이벤트                | 발행 → 구독                 | 필요한 경우                                                         |
| --------------------- | --------------------------- | ------------------------------------------------------------------- |
| FileChanged           | content → graph-rag, search | 파일 생성, 수정(본문·제목·폴더·관계), 휴지통 이동, 복원, 영구 삭제  |
| ProjectDeleted        | content → graph-rag, search | 프로젝트 영구 삭제. 유저 삭제로 그 유저의 프로젝트를 지울 때도 같음 |
| UserDeleted           | auth → content              | 유저 삭제(탈퇴)                                                     |
| GraphRefreshRequested | content → graph-rag         | 그래프 최신화 요청                                                  |
| GraphRefreshCompleted | graph-rag → content         | 그래프 최신화 종료 (성공, 실패 모두)                                |

- 공통 필드 **event_id**, **event_type**, **occurred_at**는 header로 보냄(각각 ce_id, ce_type, ce_time, Headers 규약). 필드 이름은 content API와 같은 snake_case.
- **FileChanged**
  - 변경분이 아니라 파일의 현재 상태 전체를 담음.
  - 본문만 바뀐 저장도 발행함. search는 본문으로 색인하고, graph-rag는 changed_fields를 보고 할 일이 없으면 바로 offset commit.
  - 관계는 업무상 무방향 연결이고 현재 Content 저장은 양방향 두 행이다. 관계 변경 시 양쪽 문서 이벤트 발행이 필요하며, 대상 분류에 따라 양쪽 relation_key가 달라진다.
  - 파일을 영구 삭제하면 관계가 있던 상대 파일들의 이벤트도 발행. 관계 행이 CASCADE로 같이 지워지므로.
  - 휴지통 이동은 graph-rag, search에서 지우지 않고 trashed_at, is_trashed로 표시만 함. 조회와 그래프 탐색에서 휴지통 파일을 거르고, 복원하면 RESTORED 스냅샷으로 덮어쓰며 표시를 지움.
  ```json
  {
    "project_id": "…",
    "file_id": "…",
    "version": 42,
    "change_type": "UPDATED",
    "changed_fields": ["BODY"],
    "file": {
      "title": "도깨비 학살",
      "folder_code": "EVENT",
      "body_text": "…"
    },
    "relations": [
      {
        "relation_id": "…",
        "relation_key": "related_character",
        "target_document_id": "…",
        "description": "이 연결의 설명"
      }
    ]
  }
  ```

  - **version — 미결정**: 문서 조건부 저장에서 증가하는 `revision_no`와 별도로, 이름 변경·폴더 이동·휴지통 처리까지 포함한 파일 이벤트의 순서를 표현하려는 값이다.
    - 원문에는 파일별 증가 컬럼을 추가하는 안과 `outbox_id`를 사용하는 메모가 함께 있다.
    - 아래 헤더 규약은 outbox 행 id를 UUID로 정의한다. 이벤트 식별자와 순서 비교 값의 관계를 확정하기 전에는 두 안을 하나의 구현 계약으로 취급하지 않는다.
    - `revision_no`와 관계 저장 구조도 함께 결정해야 한다.
  - **change_type**: CREATED, UPDATED, TRASHED, RESTORED, DELETED. TRASHED와 DELETED면 file, relations는 null.
  - **changed_fields**: TITLE, BODY, FOLDER, RELATIONS 중 바뀐 것. 소비자가 건너뛸지 판단하는 힌트라서, 무시하고 전체를 적용해도 결과가 같아야 함.
  - **body_text**: 서버가 에디터 JSON(body_json)에서 뽑은 순수 텍스트. 에디터 JSON은 싣지 않음. 상한이 100만 자라 크기 제한 섹션에서 claim check 기준을 정함.
  - **relations**: 현재 문서에서 바라본 관계 행 목록. target_document_id는 상대 문서, relation_key는 상대 분류의 참조 키, description은 연결 자체의 설명이다. relation_id는 방향별 저장 행 ID이므로 양쪽 이벤트에서 같은 ID·키를 공유한다고 가정하지 않는다. 관계도 연결은 문서 쌍으로 중복 제거한다.
- **ProjectDeleted**
  - 프로젝트를 영구 삭제하는 트랜잭션에서 같이 씀. 휴지통 이동은 발행하지 않음.
  - 유저 삭제도 이 이벤트로 전파됨. content가 UserDeleted를 받아 그 유저의 프로젝트를 지우면서 프로젝트마다 하나씩 발행.
  ```json
  { "project_id": "…", "reason": "OWNER_DELETED" }
  ```

  - **reason**: PROJECT_DELETED, OWNER_DELETED. 기록용.
- **UserDeleted**
  - auth가 유저를 삭제하는 트랜잭션에서 발행. 구독은 content만.
  - email, 이름 같은 개인정보는 넣지 않음.
  ```json
  { "user_id": "…" }
  ```
- **GraphRefreshRequested**
  - 마지막 최신화 이후 revision_no가 오른 파일을 보냄. 관계로 이어진 추가 파일은 graph-rag가 content HTTP API로 가져감.
  - Content가 변경 문서 전체의 본문·revision·현재 관계를 S3에 올리고, 이벤트에는 변경 목록과 S3 위치만 담음. 첫 MVP는 최대 20개·ACTIVE 본문당 5,000자를 지원하며 초과 시 명시적 입력 실패로 처리함. 일부 문서나 본문을 조용히 잘라내지 않음.
  - 기존 설정 수정·새 설정 생성·관계 ADD 제안과 결과 S3의 의미는 [갱신안 입력·결과 계약](../migration/proposal-contract.md)을 따름. 추가 조회 자료는 변경 입력 한도와 별도로 관리함.
  - 생성 후보의 candidate_id와 기존 문서 ID를 구분하며, 승인 시 실제 ID 매핑은 Content 책임이다. 이는 결과 객체 내부 규칙으로 완료 이벤트에 제안 본문을 추가하지 않는다.
  - 프로젝트당 진행 중인 요청은 하나.
  ```json
  {
    "request_id": "…",
    "project_id": "…",
    "files": [{ "file_id": "…", "revision_no": 42, "state": "ACTIVE" }],
    "input": {
      "bucket": "…",
      "key": "refresh/{project_id}/{request_id}/input.json"
    }
  }
  ```

  - **request_id**: refresh_runs 행의 id. 결과 이벤트와 짝을 맞추는 키.
  - **files**: 휴지통, 삭제된 파일도 포함. state는 ACTIVE, TRASHED, DELETED이고 본문(body_text)은 ACTIVE만 S3에 올림.
  - **revision_no**: 문서 조건부 저장의 충돌 판정 번호. 본문 내용이 달라진 횟수가 아니며, 새 저장이 성공하면 같은 본문이어도 증가할 수 있다. 역관계·휴지통 등 모든 변경 순서를 보장하는 FileChanged version과 구분한다.
- **GraphRefreshCompleted**
  - 최신화가 끝나면 graph-rag가 결과를 S3에 쓰고 발행. 실패도 같은 이벤트로 보냄.
  ```json
  {
    "request_id": "…",
    "project_id": "…",
    "outcome": "SUCCEEDED",
    "result": {
      "bucket": "…",
      "key": "refresh/{project_id}/{request_id}/result.json"
    },
    "error": null,
    "prompt_version": "…"
  }
  ```

  - **outcome**: SUCCEEDED면 저장된 result 위치를 채움. 이는 제안 생성 완료이며 문서·그래프 적용 완료가 아님. S3의 PROPOSED와 NO_CHANGE는 모두 SUCCEEDED로 전달함.
  - FAILED면 error(code, message)를 채움. 실패 결과를 S3에 저장했다면 result 위치도 전달하고, 저장 자체가 불가능한 전달 계층 실패는 result=null로 구분함. 결과 저장 실패를 성공으로 발행하지 않음.
  - **prompt_version**: 추출에 쓴 규칙 버전. refresh_runs에 기록.
  - 결과 파일에는 문서 내용 변경 제안과 새로운 관계 추가 제안을 별도 목록으로 담음. 근거 문서·revision_no·원문 위치를 포함하며 HTTP로 가져온 자료도 기록함. Kafka에는 제안 본문을 복제하지 않음.
  - Content는 완료를 수신해도 자동 적용하지 않음. 사용자 확정 후 Content 원본에 반영하고 확정 변경 이벤트로 그래프를 동기화함.

## Topic 네이밍 · 파티션 수 · 키 선택

| topic                                  | 이벤트                      | 발행 → 구독                 | key         | partition |
| -------------------------------------- | --------------------------- | --------------------------- | ----------- | --------- |
| `content.project.changed.v1`           | FileChanged, ProjectDeleted | content → graph-rag, search | project_id  | 6         |
| `content.graph-refresh.requested.v1`   | GraphRefreshRequested       | content → graph-rag         | project_id  | 3         |
| `graph-rag.graph-refresh.completed.v1` | GraphRefreshCompleted       | graph-rag → content         | project_id  | 3         |
| `auth.user.deleted.v1`                 | UserDeleted                 | auth → content              | user_id     | 3         |
| 소비자별 DLQ(DLQ 섹션)                 | 처리에 실패한 메시지        | -                           | 원본과 같음 | 3         |

- 공통 설정: replica 3, `min.insync.replicas` 2. 보존 기간과 크기 한도는 '크기 제한과 Claim Check' 참고.
- 클러스터에 있는 `content.file.changed.v1`과 `.dlq`는 CRD 확인용 잠정 topic이라 위 이름으로 교체함.
- **이름 규칙**
  - `<발행 서비스>.<대상>.<일어난 일(과거형)>.v<버전>`
  - 이벤트가 하나인 topic은 그 이벤트의 과거형(requested, completed, deleted), 여러 이벤트가 섞인 topic은 그것들을 아우르는 과거형(changed).
  - 소문자만 쓰고 구분자는 `.`, 단어 연결은 `-`. `_`는 쓰지 않음. `.`와 섞이면 metric 이름이 겹침.
  - 버전은 v1부터 시작하고, 호환이 깨지는 변경일 때만 새 버전 topic을 만듦.
  - DLQ는 `<원본 topic>.<소비자 서비스>.dlq`(DLQ 섹션). 재시도 topic은 순서를 깨므로 두지 않음.
- **key**
  - `content.project.changed.v1`은 project_id. 프로젝트 안의 모든 변경(파일, 관계, 삭제)이 커밋 순서대로 처리되고, 한 파일의 순서도 같이 지켜짐.
  - file_id로 하면 ProjectDeleted가 그 프로젝트의 파일 이벤트보다 먼저 처리될 수 있고, 관계 변경으로 함께 나가는 두 파일 이벤트의 순서도 섞임.
  - 대신 한 프로젝트 안에서는 병렬 처리가 안 됨. 프로젝트끼리는 partition에 나뉘어 병렬로 처리되고, 지금 트래픽에는 충분함.
  - 그래프 최신화 topic도 project_id. 프로젝트별 갱신안 요청을 직렬로 관리하므로 같은 프로젝트의 요청은 순서대로 하나씩 처리되어야 함. 요청과 결과의 짝은 key가 아니라 payload의 request_id로 맞춤.
- **ProjectDeleted를 FileChanged와 같은 topic에 둔 이유**
  - Kafka는 같은 topic, 같은 key 안에서만 순서를 보장함. topic을 나누면 밀려 있던 FileChanged가 ProjectDeleted보다 늦게 처리되어, 지운 프로젝트의 데이터를 다시 만들 수 있음.
  - 두 이벤트를 모두 graph-rag와 search가 구독하므로 합쳐도 필요 없는 이벤트를 받는 쪽이 없음. consumer는 event_type으로 구분.
- **partition 수**
  - `content.project.changed.v1`은 트래픽이 가장 많고, key 순서 때문에 나중에 늘리기 어려워서 처음부터 6. pod 1, 2, 3, 6개 어느 구성에도 고르게 나뉨.
  - 나머지는 broker 수와 같은 3. `content.graph-refresh.requested.v1`은 partition 수가 동시에 돌릴 수 있는 추출 수이고, 프로젝트당 진행 중인 요청이 하나라 나중에 늘려도 문제가 작음.
  - consumer의 pod 수 × concurrency는 partition 수를 넘지 않게 함.

## Message format (value 스키마)

- 기준은 CloudEvents 1.0 Kafka binding의 binary mode. value에는 이벤트 데이터(Event 섹션의 JSON)만 넣고, id·타입·시각 같은 메타데이터는 header로 보냄 (Headers 규약).
- value는 UTF-8 JSON 객체 하나. key는 UTF-8 문자열(project_id, user_id).
- **표기**
  - 필드 이름은 snake_case.
  - 시각은 RFC 3339 UTC, 밀리초까지. 예: `2026-09-30T10:00:00.123Z`
  - id는 소문자 UUID 문자열, enum은 대문자 스네이크 문자열(예: `UPDATED`), 정수는 JSON number.
- **null과 생략**
  - 스키마에 있는 필드는 항상 보내고, 값이 없으면 null. 예: TRASHED의 file.
  - 나중에 추가한 필드는 옛 메시지에 없을 수 있으므로 소비자는 없으면 기본값으로 읽음.
- **소비자 규칙 (Tolerant Reader)**
  - 모르는 필드는 무시.
  - 구독하는 topic에 처리하지 않는 ce_type이 오면 경고 로그만 남기고 건너뜀.
  - 알고 있는 이벤트에 모르는 enum 값이 오면 DLQ로 보냄. 조용히 넘기면 누락이 안 보임.
- 압축: `content.project.changed.v1` producer는 `compression.type=zstd` (한글 본문). Python 클라이언트도 zstd 지원을 확인.

## Headers 규약

- 기준은 CloudEvents 1.0 Kafka binding의 binary mode. consumer는 value를 파싱하기 전에 header만 보고 중복 확인과 타입 구분을 할 수 있음.

| header           | 값                                             | 필수 |
| ---------------- | ---------------------------------------------- | ---- |
| `ce_specversion` | `1.0`                                          | O    |
| `ce_id`          | event_id. outbox 행 id(UUID)이고 Inbox 키      | O    |
| `ce_type`        | event_type. Event 섹션의 이름(FileChanged 등)  | O    |
| `ce_source`      | 발행 서비스. `/content`, `/graph-rag`, `/auth` | O    |
| `ce_time`        | occurred_at. RFC 3339                          | O    |
| `content-type`   | `application/json`                             | O    |
| `traceparent`    | W3C Trace Context. 분산 추적을 붙일 때         | 선택 |

- 값은 UTF-8 문자열이고 같은 이름은 하나만 둠.
- ce_type에는 표준이 권장하는 reverse-DNS 접두사를 붙이지 않음. 내부에서만 쓰는 이벤트라 Event 섹션 이름 그대로 씀.
- DLQ로 보낸 메시지에 붙이는 실패 정보 header는 DLQ 섹션.

## Schema versioning

- 스키마는 이벤트마다 JSON Schema 파일 하나. 위치는 docs 저장소 `events/schemas/<event>.v1.json`.
  - producer 테스트는 발행하는 payload를 스키마로 검증하고, consumer 테스트는 스키마의 예시 payload로 검증.
- **호환 규칙**
  - 기본은 FULL. 옛 소비자가 새 메시지를, 새 소비자가 옛 메시지를 모두 읽을 수 있어야 함.
  - 바로 발행해도 되는 변경: 선택 필드 추가, 설명 수정.
  - 소비자를 먼저 배포하고 발행하는 변경(BACKWARD): enum 값 추가, topic에 새 event_type 추가.
  - 새 버전 topic(`.v2`)이 필요한 변경: 필드 삭제, 이름·타입·의미 변경, 필수 필드 추가, key 변경.
- **새 버전 topic으로 옮기는 순서**
  1. `.v2` topic을 만듦.
  2. relay가 같은 outbox 행을 v1과 v2에 같은 ce_id로 발행. 둘 다 ack를 받은 뒤 발행 표시.
  3. 소비자를 v2로 옮김. Inbox가 ce_id로 중복을 거름.
  4. v1 발행을 멈추고, 보존 기간이 지나면 v1 topic을 지움.
- **Schema Registry**
  - 지금은 두지 않음. 스키마 파일과 테스트로 충분한 규모이고, 노드 CPU 여유가 적음.
  - 도입 조건: 서비스·소비자가 늘어 리뷰로 호환 규칙을 지키기 어려울 때, 외부 소비자가 생길 때, Avro나 Protobuf로 바꿀 때.
  - 후보는 Apicurio Registry(Apache-2.0, Confluent 호환 API). JSON Schema의 호환 판정은 `additionalProperties` 설정에 따라 달라지므로 호환 모드는 도입 때 정함.

## 크기 제한과 Claim Check

- **크기 한도** (Kafka 기본 약 1MB)
  - `content.project.changed.v1`과 그 DLQ들은 topic `max.message.bytes`를 4MiB(`4194304`)로 올림. FileChanged는 body_text 100만 자 기준 최대 약 3MB.
  - producer `max.request.size`도 4MiB(`4194304`). content와, DLQ로 보내는 graph-rag, search가 대상(librdkafka는 `message.max.bytes`). 압축 전 크기로 검사하므로 zstd를 켜도 필요함.
  - consumer의 fetch 한도는 기본값 그대로. 한도보다 큰 레코드도 받아 옴.
  - 나머지 topic은 수 KB라 기본 1MB.
  - 한도를 넘으면 producer가 바로 예외를 냄. outbox 행을 격리하고 알림([Outbox 발행 실패 처리](outbox.md#5-발행-실패-처리-계단)).
- **발행량과 디스크**
  - broker 3대에 replica 3이라 모든 broker가 전체 데이터를 가짐. broker당 디스크 10Gi이고 KRaft 메타데이터도 같은 볼륨을 씀.
  - relay는 같은 파일의 아직 안 보낸 FileChanged 중 최신 하나만 보냄. 건너뛴 이벤트의 changed_fields는 합쳐서 보냄. 합치지 않으면 소비자가 건너뛰기 판단을 잘못해 변경을 놓침.
  - `content.project.changed.v1`은 `retention.bytes` partition당 512MiB, `segment.bytes` 128MiB. 아직 닫히지 않은 segment가 partition마다 하나 더 있어 broker당 최대 약 3.75GiB.
  - 그 DLQ들은 `retention.bytes` partition당 256MiB, `segment.bytes` 64MiB(broker당 최대 약 1.9GiB). 소비자 하나가 모든 이벤트를 DLQ로 보내면 본문 topic이 통째로 복사되는데, broker 3대가 같은 데이터를 갖고 KRaft 메타데이터도 같은 디스크를 써서 클러스터 전체가 멈춤.
  - broker 디스크 사용량 알림(예: 70%).
- **보존 기간**
  - `content.project.changed.v1` 3일, 그 DLQ들 7일. 본문이 들어 있어 짧게 둠(개인정보처리방침의 "지체 없이 삭제").
  - 본문 topic과 그 DLQ 모두 `segment.ms` 1일. 닫힌 segment만 지워지므로 실제로는 보존 기간에 최대 1일이 더해짐.
  - 나머지 topic 7일, 그 DLQ 30일. id와 S3 위치만 들어 있음.
  - 모든 consumer group은 `auto.offset.reset=earliest`로 두고 lag 알림을 검. 보존 기간보다 긴 장애는 replay 대신 content에서 전체를 다시 동기화함(content에 없는 프로젝트는 지움).
- **Claim Check (S3)**
  - 대상: GraphRefreshRequested의 입력 본문, GraphRefreshCompleted의 결과.
  - 버킷: 미디어 버킷(CloudFront로 공개 조회)과 분리한 비공개 버킷. 새 버킷 기본값(공개 차단, SSE-S3 암호화)을 그대로 씀.
  - key: `refresh/{project_id}/{request_id}/input.json`, `refresh/{project_id}/{request_id}/result.json`
  - input을 먼저 올리고 그다음 refresh_runs와 outbox를 한 트랜잭션으로 씀. key에 request_id가 있어 덮어쓰지 않음.
  - graph-rag는 result가 이미 있으면 LLM을 다시 돌리지 않고 완료 이벤트만 다시 보냄.
  - 권한: content는 `refresh/*` Put, Get, Delete. graph-rag는 input Get, result 및 실행 스냅샷 Put, Get이 필요함(Pod Identity 추가 필요). 실행 스냅샷의 객체 키는 실제 연동 전에 고정함. Content 원본·확정 그래프 쓰기 권한을 갱신안 생성에 사용하지 않음.
- **S3 만료와 삭제**
  - Lifecycle 규칙: `refresh/`는 생성 후 3일에 만료, 끝나지 않은 multipart 업로드는 1일 뒤 정리.
  - 3일인 이유: content의 최신화 타임아웃(예: 1시간)이 지나면 객체를 다시 쓰지 않음. 장애를 들여다볼 여유만 둠.
  - S3는 만료를 다음 00:00 UTC에 맞춰 비동기로 처리하므로 실제로는 3~4일.
  - 만료 뒤 DLQ 재처리에서 객체가 없으면(NoSuchKey) 지난 요청으로 보고 건너뜀.
  - 프로젝트 삭제: ProjectDeleted를 쓰는 트랜잭션이 커밋된 뒤 content가 `refresh/{project_id}/`를 지움(목록 조회 후 일괄 삭제).
  - graph-rag는 다른 topic을 읽으므로 삭제 뒤에 result를 쓸 수 있음. content는 삭제된 프로젝트의 완료 이벤트를 받으면 그 result도 지움. 나머지는 Lifecycle이 치움.

## DLQ

| 원본 topic                             | DLQ                                                | 보내는 쪽 |
| -------------------------------------- | -------------------------------------------------- | --------- |
| `content.project.changed.v1`           | `content.project.changed.v1.graph-rag.dlq`         | graph-rag |
| `content.project.changed.v1`           | `content.project.changed.v1.search.dlq`            | search    |
| `content.graph-refresh.requested.v1`   | `content.graph-refresh.requested.v1.graph-rag.dlq` | graph-rag |
| `graph-rag.graph-refresh.completed.v1` | `graph-rag.graph-refresh.completed.v1.content.dlq` | content   |
| `auth.user.deleted.v1`                 | `auth.user.deleted.v1.content.dlq`                 | content   |

- **이름 규칙**: `<원본 topic>.<소비자 서비스>.dlq`. topic마다가 아니라 소비자마다 둠.
  - 실패는 소비자마다 다르게 일어남(ES 장애, Neptune 장애 등). topic마다 하나면 여러 소비자의 실패가 섞이고, 원래 topic으로 재투입하면 이미 성공한 소비자도 다시 받음.
  - 소비자가 하나인 topic도 같은 규칙. 소비자가 늘어도 기존 DLQ 이름을 바꿀 필요 없음.
  - DLQ는 그 소비자 서비스 소유. 알림과 재처리도 그 서비스가 맡음.
- **보내는 경우**
  - 재시도를 다 써도 실패한 메시지([Inbox 재시도와 DLQ](inbox.md#4-에러-처리-재시도와-dlq)).
  - 재시도해도 소용없는 실패는 바로 보냄: 역직렬화 실패, 스키마에 안 맞는 payload, 알고 있는 이벤트의 모르는 enum 값.
  - 처리하지 않는 ce_type은 DLQ로 보내지 않고 건너뜀(Message format).
- **설정**
  - partition 3, replica 3. key는 원본 그대로이고, 보낼 때 partition을 -1로 두어 key로 정해지게 함(원본 6개, DLQ 3개).
  - 보존 기간·메시지 크기·디스크 한도는 [크기 제한과 Claim Check](#크기-제한과-claim-check)에서 관리한다. DLQ producer에도 해당 크기 한도를 적용해야 큰 메시지 때문에 partition이 멈추지 않는다.
- **header**
  - 원본 header(ce\_\*)는 그대로 두고 실패 정보를 붙임: `kafka_dlt-original-topic`, `kafka_dlt-original-partition`, `kafka_dlt-original-offset`, `kafka_dlt-exception-fqcn`, `kafka_dlt-exception-message`
  - spring-kafka DeadLetterPublishingRecoverer가 붙이는 이름 그대로 씀. graph-rag(Python)도 같은 이름과 형식으로 붙임.
  - partition, offset header는 문자열이 아니라 int, long 바이트(big-endian). 나머지는 UTF-8 문자열.
  - spring-kafka 기본 DLQ 이름(`-dlt`)은 쓰지 않고 destination resolver로 위 이름을 지정함.
- 재처리는 [미작성 운영 항목의 DLQ 재투입 절차](README.md#미결정과-미작성-항목).
