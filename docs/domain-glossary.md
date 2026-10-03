# Content 기준 도메인 용어집

Content가 관리하는 원본 개념을 기준으로 GraphRAG의 문서·코드·메시지 용어를 맞춘다. 한국어 설명에서는 **문서**, 외부 식별자는 해당 Content API의 **file_id / document_id**를 그대로 사용하며 변환 경계에서 대응시킨다.

확인 기준은 2026-10-03의 로컬 Content 코드(HEAD `fd4dc94`)와 GraphRAG 계약 문서다. 운영 배포 상태를 의미하지 않는다. 아래에서 **구현**은 확인한 코드 동작, **DB 정의**는 테이블·컬럼만 확인한 상태, **계획**은 갱신안 계약의 목표를 뜻한다. 주석과 실제 코드가 다르면 실제 코드의 동작을 우선한다.

## 1. 프로젝트와 문서

| 표준 용어 | Content 이름 | 의미와 사용 기준 |
|---|---|---|
| 프로젝트 | `Project`, `projects`, `project_id` | 창작 자료를 묶는 범위. 문서·관계·갱신안의 프로젝트 범위를 일치시킨다. ID 일치만으로 접근 권한 검증이 끝난 것은 아니다. |
| 문서 | `document`, `FileRows.Document`, API의 file | 제목·본문·속성·관계를 가진 편집 단위. API `/files/{fileId}/content`의 file과 DB document는 같은 문서를 가리킨다. S3 객체를 뜻하는 파일과 구분한다. |
| 문서 식별자 | `document.id`, `file_id`, `document_id` | 위 문서의 UUID. `file_id`와 `document_id`를 서로 다른 엔티티의 ID로 해석하지 않는다. 다만 `target_document_id` 등 필드의 역할은 보존한다. |
| 기본 분류 | `base_folders`, `folder_code` | 세계관·캐릭터·장소·원고 등 사전 정의된 분류. 일반적인 사용자 생성 폴더와 구분한다. |
| 회차 폴더 | `episode_folders`, `episode_id`, `Episode` | 원고를 회차 단위로 묶는 별도 객체. 회차 ID를 문서 ID나 기존 AI의 정수 회차 번호로 대체하지 않는다. |
| 원고 문서 | `folder_code = MANUSCRIPT` | 문서 중 원고 분류의 자료. 모든 문서를 원고 또는 회차라고 부르지 않는다. |
| 설정 문서 | GraphRAG 설명상의 역할 | 갱신안을 제안할 기존 설정 자료를 통칭하는 말. Content에 별도의 Setting 엔티티가 있는 것은 아니다. 실제 대상 분류·ID는 계약에서 명시한다. |
| 잠금 | `locked` | 문서 저장·복원을 제한하는 편집 상태. 휴지통이나 삭제, 외부 조회 권한과 같은 개념이 아니다. |
| 휴지통 | `trashed_at`, 응답의 `isTrashed()` | 문서 행을 유지한 채 휴지통 상태로 표시. 영구 삭제와 구분한다. |
| 문서 상태 | 이벤트 계획의 `ACTIVE`, `TRASHED`, `DELETED` | 전달 계약의 상태 표현. Content 문서 테이블에 이 enum 컬럼이 있다고 가정하지 않는다. |

근거: [FileRows](../../loresentry-content/src/main/java/com/loresentry/content/file/FileRows.java), [DocumentController](../../loresentry-content/src/main/java/com/loresentry/content/document/DocumentController.java), [Content 테이블 정의](../../loresentry-content/src/main/resources/db/migration/V1__create_content_schema.sql).

### 분류와 관계 키의 대응

관계 키는 **가리키는 대상 문서의 분류**를 표현한다. 인물 사이의 ‘친구’, ‘적대’ 같은 자유로운 의미 관계 이름이 아니다.

| 분류 코드 | Content 표시 이름 | 해당 분류를 가리키는 관계 키 |
|---|---|---|
| `WORLDVIEW` | 세계관 | `related_worldview` |
| `CHARACTER` | 캐릭터 | `related_character` |
| `LOCATION` | 장소 | `related_place` |
| `MANUSCRIPT` | 원고 | `related_manuscript` |
| `ORGANIZATION` | 조직 | `related_organization` |
| `ITEM` | 아이템 | `related_item` |
| `EVENT` | 이벤트 | `related_event` |

`LOCATION`의 키는 `related_location`이 아니다. 코드 문자열을 임의 변환하지 않고 Content의 대응표를 따른다. 이 표가 존재한다고 저장 API가 모든 입력 키를 이 목록으로 엄격히 제한한다고 단정하지 않는다. GraphRAG의 허용 키 검증은 별도로 구현해야 한다.

근거: [기본 분류 seed](../../loresentry-content/src/main/resources/db/migration/V2__seed_base_folders.sql), [RelationKeys](../../loresentry-content/src/main/java/com/loresentry/content/document/RelationKeys.java).

## 2. 본문·속성·스냅샷

| 표준 용어 | Content 이름 | 의미와 구분 |
|---|---|---|
| 제목 | `title` | 문서의 이름. 본문이나 설명 속성과 다르다. |
| 구조화 본문 | API `body`, DB `body_json` | `schema_version`과 에디터 `doc`을 담은 JSON. 실제 편집 문서의 구조·서식을 보존한다. |
| 본문 텍스트 | `body_text` | Content가 구조화 본문에서 추출한 순수 텍스트. 검색·AI 입력에 쓰는 파생 값이며 에디터 JSON 자체가 아니다. |
| 레거시 본문 | DB `body_md`, 응답 `legacy_body_md` | 이전 Markdown 자료의 호환용 값. 새 저장 요청의 기준 본문으로 사용하지 않는다. |
| 글자 수 | `char_count` | Content는 본문 텍스트에서 줄바꿈 `\n`을 제외한 Unicode 코드 포인트 수를 센다. GraphRAG 입력 한도의 `len(body_text)`와 동일하지 않다. |
| 텍스트 속성 | `properties`, `TextProperty(key, value)` | 문서에 붙는 키·문자열 값. DB의 `property_key`, `text_value`에 대응하며 다른 문서 참조와 구분한다. |
| 문서 설명 | 속성 키 `description` | 관계도 노드에 사용하는 문서 설명. 관계 연결 자체의 설명과 다르다. |
| 문서 스냅샷 | Content `DocumentSnapshot` | 제목·구조화 본문·속성·관계를 묶은 값. 저장 요청과 버전 스냅샷에 사용한다. 자체에는 문서 ID·revision이 없으며 응답이나 버전 메타데이터가 함께 제공한다. |
| 조회 시점 스냅샷 | GraphRAG `DocumentSnapshot` | 프로젝트·문서 ID, revision, 본문 문자열 등을 담는 내부 값. 이름은 같지만 Content DTO와 구조가 다르므로 그대로 직렬화하거나 동일시하지 않는다. |
| 실행 스냅샷 | GraphRAG `ExecutionSnapshot` | 한 갱신 요청에서 확보한 기본·추가 자료와 선택 설정을 고정한 묶음. Content의 사용자 버전 기록과 다르다. |

근거: [Content DocumentSnapshot](../../loresentry-content/src/main/java/com/loresentry/content/document/DocumentSnapshot.java), [DocumentResponses](../../loresentry-content/src/main/java/com/loresentry/content/document/DocumentResponses.java), [BodyText](../../loresentry-content/src/main/java/com/loresentry/content/document/BodyText.java), [본문 JSON 마이그레이션](../../loresentry-content/src/main/resources/db/migration/V9__document_body_json.sql), [GraphRAG 내부 모델](../app/refresh/models.py).

## 3. 관계와 그래프

| 표준 용어 | Content 이름 | 의미와 사용 기준 |
|---|---|---|
| 문서 관계 | `relations`, `document_relations` | 사용자가 문서 사이에 저장한 명시적 연결. 본문에서 AI가 추정한 관계 후보와 구분한다. |
| 관계 대상 | `target_document_id` | 해당 문서에서 바라본 상대 문서 ID. |
| 관계 키 | `relation_key` | 상대 문서 분류에 맞는 참조 키. 방향을 바꾸면 키도 달라질 수 있다. |
| 관계 설명 | 관계의 `description` | 두 문서의 연결이 무엇을 의미하는지 설명하는 문자열. 대상 문서의 설명이 아니다. |
| 관계 행 ID | `document_relations.id` | 저장된 방향별 행의 UUID. 양방향 관계 전체가 반드시 하나의 ID를 공유한다고 가정하지 않는다. |
| 관계도 | Content `GraphResponses.Graph` | 현재 RDB 문서·속성·명시적 관계에서 읽는 `nodes`, `edges`, `episodes` 투영 응답. Neptune 조회와 같은 API가 아니다. |
| 노드 | `GraphResponses.Node` | 관계도에서 문서 ID·제목·분류·문서 설명을 표현한 값. |
| 연결선 | `GraphResponses.Edge` | 관계도에서 문서 쌍의 연결을 한 번만 표시하는 값. `source`와 `target`이 있어도 업무적으로 방향 있는 관계라고 해석하지 않는다. |
| 출처 | edge의 `origin` | 현재 조회 구현은 `USER`를 반환한다. AI 제안이 생겼다는 이유만으로 기존 응답에 `AI` 관계가 자동 저장되지는 않는다. |
| 그래프 투영 | GraphRAG 계획 | Content의 확정 변경을 조회용 그래프에 반영한 데이터. 원본의 소유권은 Content에 있다. |
| 관계 제안 | GraphRAG 계획의 `relation_proposals` | 아직 확정되지 않은 새 연결 후보와 근거. S3에 저장하며 확정 그래프에는 직접 쓰지 않는다. |

현재 Content는 관계를 반대쪽 문서에도 기록한다. 예를 들어 캐릭터 A와 장소 B의 연결은 A에서 `related_place → B`, B에서 `related_character → A`로 표현된다. 의미상 같은 연결이지만 행과 키는 다르다. 관계도 조회는 문서 쌍으로 중복을 제거해 하나의 연결선으로 보여준다.

근거: [DocumentService.mirrorRelations](../../loresentry-content/src/main/java/com/loresentry/content/document/DocumentService.java), [GraphRepository](../../loresentry-content/src/main/java/com/loresentry/content/graph/GraphRepository.java), [GraphResponses](../../loresentry-content/src/main/java/com/loresentry/content/graph/GraphResponses.java).

## 4. Revision과 버전

| 표준 용어 | 이름 | 의미와 구분 |
|---|---|---|
| 문서 revision | `revision_no` | 문서 조건부 저장의 충돌 판정 번호. Content의 저장 UPDATE가 성공하면 증가한다. ‘본문 내용이 달라진 횟수’로 정의하지 않는다. |
| 기대 revision | HTTP `If-Match`, 내부 `expectedRevision` | 호출자가 수정의 기준으로 삼은 번호. 현재 값과 다르면 덮어쓰지 않고 충돌을 알린다. |
| 저장 식별자 | `X-Save-Id`, `last_save_id` | 저장 재시도를 구분하는 멱등 키. 현재 코드에서는 마지막 저장 ID와 같으면 재적용하지 않는다. 모든 과거 저장 ID의 무기한 중복 제거를 뜻하지 않는다. |
| 문서 버전 | `document_versions`, `Version` | 특정 시점의 스냅샷을 보관하는 별도 기록. 버전 행 ID와 revision 번호는 다르다. |
| 버전의 원본 revision | `source_revision_no` | 해당 버전 스냅샷이 어떤 문서 revision에서 만들어졌는지 나타내는 값. |
| 제안 기준 revision | GraphRAG `base_revision_no` | 제안이 검토한 대상 문서의 revision. 새 revision을 발급하거나 현재 값을 갱신하는 필드가 아니다. |
| 변경 이벤트 순서 값 | 메시지 초안의 `version` | 파일의 모든 변경 순서를 표현하려는 별도 값. 아직 미결정이며 `revision_no`나 UUID 이벤트 ID와 동일시하지 않는다. |

문서 저장은 제목·본문·속성·관계를 함께 처리한다. 본문이 동일해도 새로운 저장이 성공하면 revision이 증가할 수 있다. 반대로 상대 문서의 역관계 반영과 별도 파일 메타데이터 변경까지 모두 같은 revision으로 추적되는 것은 아니다. 따라서 이 번호만으로 그래프 반영 완료를 판정하지 않는다.

버전 종류는 DB에 `AUTO`, `NAMED`, `AI_APPLY`, `RESTORE`, `REFRESH_BASE`로 정의되어 있다. 이름이 테이블에 존재하는 사실과 AI 적용 기능이 구현된 사실은 별개다.

근거: [DocumentService.save / restore](../../loresentry-content/src/main/java/com/loresentry/content/document/DocumentService.java), [DocumentRepository.updateIfRevisionMatches](../../loresentry-content/src/main/java/com/loresentry/content/document/DocumentRepository.java), [테이블 정의](../../loresentry-content/src/main/resources/db/migration/V1__create_content_schema.sql).

## 5. 최신화 요청·변경 제안·사용자 확정

| 표준 용어 | 대응 이름 | 의미와 상태 |
|---|---|---|
| 변경 문서 | 요청의 `files` | 이번 분석에 제공하는 변경 자료. 갱신안을 받을 대상 문서와 일치할 필요는 없다. **계획** |
| 갱신 대상 문서 | `target_document_id` | 내용 변경안을 검토할 기존 문서. 여러 변경 문서가 여러 대상에 영향을 줄 수 있다. **계획** |
| 그래프 최신화 요청 | `GraphRefreshRequested` | 제품의 최신화 동작으로 갱신안 생성을 요청하는 이벤트. 확정 그래프를 즉시 수정하라는 명령으로 해석하지 않는다. **계획** |
| 최신화 실행 기록 | `refresh_runs` | Content의 요청·검토·적용 상태를 담을 테이블. `CAPTURING_BASE`, `GENERATING`, `READY`, `APPLYING`, `APPLIED`, `FAILED`, `CANCELLED`가 정의되어 있다. **DB 정의** |
| 문서 갱신 초안 | `refresh_document_drafts` | 대상별 기준 버전과 비교·검토용 스냅샷을 담을 테이블. GraphRAG의 S3 결과 그 자체는 아니다. **DB 정의** |
| 문서 내용 변경 제안 | `document_proposals` | 대상·기준 revision·제안 값·근거를 가진 후보. 사용자 확정 전 원본에는 반영하지 않는다. **계획** |
| 근거 | `Evidence` | 특정 문서 revision의 원문 구간과 인용문. 제안 문장 자체나 모델의 설명과 구분한다. |
| 제안 생성 결과 | `PROPOSED`, `NO_CHANGE`, `FAILED` | GraphRAG 결과의 업무 상태. 문서나 관계 중 제안이 있으면 PROPOSED다. **계획** |
| 생성 완료 이벤트 | `GraphRefreshCompleted` | S3 결과 위치와 처리 상태를 알림. `SUCCEEDED`는 생성 처리 성공이며 사용자 적용 성공이 아니다. **계획** |
| 사용자 확정·적용 | Content의 후속 처리 | 사용자가 검토한 내용을 원본에 반영하는 단계. Kafka 완료 수신이나 S3 객체 생성과 구분한다. **계획** |
| 확정 변경 동기화 | `FileChanged` 등 | Content 원본의 확정 변경을 조회용 그래프에 반영하는 별도 흐름. 제안 생성과 혼동하지 않는다. **계획** |

본문과 관계 제안은 S3 결과에만 기록한다. 입력·결과 객체는 비공개 처리 자료이며 Content의 공개 이미지 업로드용 미디어 객체와 구분한다. 객체 위치는 bucket과 key의 조합으로 표현하며 key 하나를 공개 URL이라고 부르지 않는다.

상세 동작·한도·결과 전달은 [갱신안 입력·결과 계약](migration/proposal-contract.md), Kafka 외형은 [메시지 계약 초안](reference/message-contract.md)을 따른다. 이 용어집에서 별도 전송 스키마를 만들지는 않는다.

## 6. GraphRAG 이름과 연결할 때의 기준

| 현재 GraphRAG 이름 | Content 기준 대응 | 주의점 |
|---|---|---|
| `DocumentSnapshot.document_id` | 문서 ID (`file_id` / `document.id`) | ID의 문자열 표현과 UUID 검증은 어댑터에서 맞춘다. |
| `DocumentSnapshot.revision_no` | `revision_no` | 본문 변경 횟수나 모든 이벤트의 순서 번호로 해석하지 않는다. |
| `DocumentSnapshot.body_text` | 분석용 `body_text` | Content의 JSON `body`와 타입이 다르다. |
| `DocumentSnapshot.properties` | `properties`의 key/value | 표시용 label과 섞지 않는다. |
| `Relation.document_id`, `target_document_id`, `relation_key` | 현재 문서에서 본 관계 행 | 상대 분류에 따른 키를 유지한다. 반대쪽 키를 복사하지 않으며 연결 중복은 문서 쌍으로 구분한다. |
| `DocumentProposal.field`, `value` | 제안할 문서 필드·값 | 현재 문자열 후보 모델이 Content 구조화 본문이나 관계 제안까지 구현한 것은 아니다. |
| `ArtifactRef` | S3 bucket/key | 문서 ID 또는 관계 ID와 구분한다. |
| `JobKey.request_id` | 최신화 요청 ID | Content 계획의 refresh_runs ID와 연결한다. Kafka event_id와는 별개다. |

## 7. 통일한 기준과 남은 연동 경계

내부 코드·테스트와 메시지·갱신안 문서의 이름을 아래 기준으로 맞췄다. 실제 외부 스키마·어댑터 연동은 별도 구현이며, Content 코드와 이미 배포된 이벤트 이름을 변경한 것은 아니다.

1. **관계 저장:** 방향별 문서·대상 ID와 relation_key를 사용한다. 메시지 초안도 양방향 행·대상 분류별 키로 정리했다. 신규 연결 제안은 문서 쌍으로 중복을 판별하고 방향별 키는 분류에 맞춰 검증한다. 실제 S3·이벤트 직렬화는 Content와 통합 검증해야 한다.
2. **Revision:** 내부 필드는 revision_no, 제안 기준은 base_revision_no로 통일했다. 문서 저장 경로의 revision_no와 별도 메타데이터·역관계 변경을 구분하며 이벤트 순서 값은 별도 합의한다.
3. **본문:** Content의 body는 에디터 JSON이고 GraphRAG의 body_text는 분석용 문자열이다. 텍스트 제안을 사용자가 확정할 때 에디터 JSON으로 반영하는 방법은 별도 계약이 필요하다.
4. **문자 수:** Content char_count는 줄바꿈을 제외하지만 GraphRAG 입력 예산은 원문 전체 코드 포인트 수다. 같은 글자 수 필드로 취급하지 않는다.
5. **AI 관계:** Content GraphResponses의 주석에는 AI 추출 관계를 Neptune에 쌓는 미래 설명이 남아 있다. 현재 조회 구현과 이번 제품 결정은 구분하며, 이번 MVP에서는 AI 관계도 S3 제안으로만 저장한다.
6. **구현 상태:** Content README의 Kafka·AI 최신화 설명, refresh 테이블의 존재만으로 실제 발행·S3 입력 생성·사용자 적용이 구현됐다고 판단하지 않는다. 확인한 GraphRAG 코드도 실제 rules와 관계 제안 모델은 후속 작업이다.

문서에서는 ‘문서를 조회한다’, ‘본문 텍스트로 분석한다’, ‘관계를 제안한다’, ‘사용자가 확정한다’, ‘확정 변경을 동기화한다’를 구분해서 쓴다. ‘그래프를 최신화한다’만으로 이 모든 단계를 지칭하지 않는다.

## 8. 내부 이름 변경과 외부 호환 범위

내부 스냅샷과 근거의 번호는 `revision_no`, 분석 문자열은 `body_text`, 관계 필터는 `relation_keys`로 사용한다. 문서 내용 후보는 `DocumentProposal`, 대상은 `target_document_id`, 기준 번호는 `base_revision_no`, 후보·결과의 목록은 `document_proposals`다.
이 이름들은 미배포 내부 Python 계약에 적용했다. HTTP 진단 응답, Kafka의 기존 이벤트 이름·`file_id`, Content의 JSON `body`는 도메인 이름 변경을 이유로 함께 바꾸지 않는다. 관계 제안 모델과 실제 순수 규칙은 여전히 후속 구현이다.
