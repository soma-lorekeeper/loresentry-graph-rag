# 갱신안 S3 입출력 계약

> **책임:** 입력 공급자와 결과 소비자가 맞춰야 할 S3 객체 위치, JSON 필드, 무결성, 저장·재사용 규칙을 정의한다.
>
> **확인할 때:** 변경 문서를 S3로 전달하거나 저장된 갱신안을 읽는 연동을 구현할 때.

현재 `S3RefreshArtifacts`가 구현한 계약이다. 입력 공급자는 변경 문서 묶음을 저장하고,
GraphRAG는 요청 manifest·실행 스냅샷·결과를 저장한다. Content와의 운영 연동이나
HTTP·Kafka 갱신안 진입점은 아직 연결하지 않았다. 어댑터 주입·환경변수·타임아웃은
[S3 저장소 구현 안내](implementation/S3_ARTIFACTS.md)를 따른다.

S3 결과는 승인 전 제안이다. 결과 생성·저장만으로 Content 문서나 확정 그래프를 변경하지 않는다.
이 문서의 JSON은 LLM의 후보 응답 JSON과 별개다.

## 객체와 위치

| 객체 | 생성 주체 | 버전 | 저장 위치·역할 |
| --- | --- | --- | --- |
| 변경 입력 | 입력 공급자 | `refresh-input-v1` | 요청의 `input_ref`가 지정하는 입력 버킷·key. 이 요청에 전달할 변경 문서 묶음 |
| `request.json` | GraphRAG | `refresh-request-v1` | 출력 버킷. 같은 요청 ID의 입력·설정이 바뀌지 않았는지 확인하는 manifest |
| `context.json` | GraphRAG | `refresh-context-v1` | 출력 버킷. 최초 입력과 추가 조회 문서를 고정한 실행 스냅샷 |
| `result.json` | GraphRAG | `refresh-result-v3` | 출력 버킷. 제안 있음·변경 없음·실패 결과 |

입력은 설정된 `input_bucket`의 `input_prefix/` 아래에 있어야 한다. 파일명은 공급자가
정하며 `input.json`으로 고정하지 않는다. 같은 요청의 출력 세 객체를 입력으로 지정할 수 없다.
입력·출력 버킷은 같아도 된다. 출력 위치는 다음 규칙으로 결정한다.

```text
{prefix}/{encoded_project_id}/{encoded_request_id}/request.json
{prefix}/{encoded_project_id}/{encoded_request_id}/context.json
{prefix}/{encoded_project_id}/{encoded_request_id}/result.json
```

이 표기는 경로 규칙이다. 각 ID에 `urllib.parse.quote(value, safe="")`를 적용한다.
예를 들어 프로젝트 ID `project/a`, 요청 ID `update 1`, prefix `refresh`의 결과 key는
`refresh/project%2Fa/update%201/result.json`이다. 소비자는 반환된 `bucket`·`key`를
S3 SDK에 그대로 전달한다. key를 URL 디코딩하거나 ID로 다시 조합하지 않는다.

위치 참조는 공개 URL이나 presigned URL이 아닌 다음 두 필드다.

```json
{
  "bucket": "artifact-bucket",
  "key": "refresh/project-1/request-1/result.json"
}
```

## 요청과 입력 식별

요청의 `job`에는 `project_id`, `request_id`, `input_fingerprint`가 있다.
두 ID는 비어 있지 않은 문자열이며, fingerprint는 **S3 입력 바이트 전체의 SHA-256을
소문자 16진수 64자로 표현한 값**이다. 입력 JSON에는 fingerprint 자체를 넣지 않는다.
S3 ETag·문서 revision·JSON 객체를 별도 방식으로 해시한 값으로 대체할 수 없다.

공급자는 다음 순서로 전달한다.

1. `refresh-input-v1` JSON을 UTF-8 바이트로 만든다.
2. 그 바이트 전체의 SHA-256을 계산한다.
3. 같은 바이트를 S3에 저장한다.
4. `input_ref`와 계산한 `job.input_fingerprint`를 요청에 전달한다.

해시 계산 뒤 JSON 정렬·들여쓰기·끝 개행을 바꾸면 fingerprint가 달라진다.
GraphRAG는 별도 S3 `VersionId`를 지정하지 않고 읽은 바이트의 해시를 대조한다.
요청 중 사용할 입력 객체는 공급자가 보존해야 한다.

요청의 `seed_ids`는 입력의 문서 ID 안에 있어야 한다. 선택적인 `changed_documents`를
전달했다면 그 목록의 문서 ID·revision·state가 입력 문서 전체와 정확히 일치해야 한다.
`changed_documents=[]`이면 이 목록 대조는 생략하지만 프로젝트·문서·해시 검증은 수행한다.
요청의 `target_ids`는 갱신할 기존 설정 ID이며 변경 입력 문서 목록과 별개다.

## 변경 입력: refresh-input-v1

| 필드 | 형식 | 계약 |
| --- | --- | --- |
| `schema_version` | 문자열 | 정확히 `refresh-input-v1` |
| `project_id` | 문자열 | 요청의 `job.project_id`와 일치 |
| `request_id` | 문자열 | 요청의 `job.request_id`와 일치 |
| `documents` | 문서 배열 | 전달하는 변경 문서 전체. 문서 ID 중복 금지 |

각 문서는 다음 필드를 사용한다. 필수 필드의 생략, 알 수 없는 필드, 문자열 숫자 등
타입 변환이 필요한 값은 거절한다.

| 문서 필드 | 형식 | 필수·기본값 및 조건 |
| --- | --- | --- |
| `project_id` | 문자열 | 필수. 요청 프로젝트와 일치 |
| `document_id` | 문자열 | 필수. 비어 있지 않은 ID |
| `revision_no` | 정수 | 필수. 1 이상. 문자열·bool은 허용하지 않음 |
| `body_text` | 문자열 또는 null | 필수. `ACTIVE`이면 문자열. `TRASHED`·`DELETED`는 null 허용 |
| `state` | 문자열 | 생략 시 `ACTIVE`. `ACTIVE`, `TRASHED`, `DELETED` 중 하나 |
| `folder_code` | 문자열 | 생략 시 `MANUSCRIPT`. 아래의 분류 중 하나 |
| `title` | 문자열 또는 null | 선택. 생략 시 null. Content의 독립 제목 필드 |
| `properties` | 문자열 쌍의 배열 | 생략 시 `[]`. 예: `[["name", "서윤"], ["alias", "윤"]]` |
| `relations` | 관계 배열 | 생략 시 `[]`. 각 항목은 아래의 관계 필드 사용 |

분류는 `MANUSCRIPT`, `WORLDVIEW`, `CHARACTER`, `LOCATION`, `ORGANIZATION`, `ITEM`,
`EVENT`다. 변경 입력 개수는 기본·지원 상한 20개이며 비활성 문서도 센다.
ACTIVE 본문은 문서별 기본·지원 상한 5,000 Unicode 코드 포인트다.
요청의 `max_changed_documents`·`max_input_chars`로 더 낮은 한도를 지정할 수 있다.

본문은 Content의 구조화 body JSON이 아닌 분석용 `body_text`다.
공급자가 구조화 본문을 문자열로 변환해 전달하며, 이후 공백·줄바꿈·표기·이모지는
GraphRAG가 정규화하지 않는다. 속성·제목·본문의 구분은 [도메인 용어집](domain-glossary.md)을 따른다.

입력 예시:

```json
{
  "schema_version": "refresh-input-v1",
  "project_id": "project-1",
  "request_id": "request-1",
  "documents": [
    {
      "project_id": "project-1",
      "document_id": "chapter-05",
      "revision_no": 6,
      "body_text": "비가 내렸다.\n서윤은 문을 닫았다.",
      "properties": [],
      "relations": [],
      "state": "ACTIVE",
      "folder_code": "MANUSCRIPT",
      "title": "5화"
    }
  ]
}
```

내장 관계의 필드는 `project_id`, `document_id`, `target_document_id`, `relation_key`,
선택적인 `description`이다. description의 기본값은 빈 문자열이다.
관계의 project·document ID는 소유 문서와 일치해야 하고 자기 연결은 금지한다.
분류별 관계 키와 양방향 의미는 [제안 계약](migration/proposal-contract.md)을 따른다.

## 요청 manifest와 실행 스냅샷

| 객체 | 최상위 필드 | 내부 내용 |
| --- | --- | --- |
| `request.json` | `schema_version`, `request` | `request`는 전체 `RefreshRequest` |
| `context.json` | `schema_version`, `snapshot` | `snapshot`은 전체 `ExecutionSnapshot` |

`request`에는 `job`, `input_ref`, `seed_ids`, `target_ids`, `discover_related`,
`required_graph_version`, `settings`, `model_settings`, `changed_documents`가 들어간다.
`settings`는 선택·문맥·호출 예산이며, `model_settings`는 모델·프롬프트·후보 스키마 버전이다.
입력뿐 아니라 이 설정들도 같은 요청 ID의 재실행에서 일치해야 한다.

`snapshot`에는 다음 값이 들어간다.

| 필드 | 내용 |
| --- | --- |
| `request` | 스냅샷을 만들 때의 전체 요청 |
| `documents` | 입력 문서와 추가 조회 문서. 위 문서 필드와 동일한 형식 |
| `related` | `document_ids`, `relations`, `readiness`, `observed_version` |
| `changed_ids` | S3 변경 입력에 있던 문서 ID 전체 |
| `target_ids` | 최종 선택한 갱신 대상. ACTIVE 기존 설정이며 원고는 포함하지 않음 |

최초 스냅샷 저장 전 입력을 다시 읽고 변경 문서 전체가 원본과 같은지 확인한다.
모델 호출 전에 스냅샷을 저장하며, 재실행은 저장된 문서·revision을 사용한다.
현재 저장기는 `title=null`, `separate_discovery=false`, `chunk_overlap_chars=0`을
호환을 위해 각각 생략할 수 있다. 필드 생략은 해당 내부 기본값으로 해석한다.

## 결과: refresh-result-v3

| 최상위 필드 | 형식·내용 |
| --- | --- |
| `schema_version` | `refresh-result-v3` |
| `project_id`, `request_id` | 요청 식별자 |
| `outcome` | `PROPOSED`, `NO_CHANGE`, `FAILED` |
| `document_proposals` | 기존 문서의 전체 `body_text` 교체안 |
| `new_document_proposals` | 임시 ID를 가진 신규 설정 생성안 |
| `relation_proposals` | 두 문서·후보 사이의 ADD 연결안 |
| `facts` | 제안이 참조하는 구조화 사실 |
| `sources` | `document_id`, `revision_no`, `origin`, `state`. origin은 `INPUT` 또는 `CONTENT` |
| `evidence` | `evidence_id`, `document_id`, `revision_no`, `start`, `end`, `quote`로 된 공통 근거 사전 |
| `error` | 성공이면 null, 실패이면 `code`, `message`, `retryable` |
| `execution` | 입력 해시·버전·예산·사용량·실행 범위·선택적 진단 및 계획 |

PROPOSED는 제안이 하나 이상 있고 error가 null이다. NO_CHANGE는 제안·facts가 비어 있고
error가 null이다. FAILED는 error가 있으며 제안·facts는 전부 비어 있다.
실패 전에 확보한 sources나 실행 계획·시도 기록은 남을 수 있다.
성공 결과를 저장하려면 context가 있어야 한다. 스냅샷 생성 전의 종료 실패는 context 없이도 저장할 수 있다.

제안과 사실의 필드는 다음과 같다. 근거 본문을 항목마다 복제하지 않고 `evidence_refs`로 참조한다.

| 항목 | 필드 |
| --- | --- |
| 기존 문서 수정 | `target_document_id`, `base_revision_no`, `field="body_text"`, `value`, `fact_ids`, `evidence_refs` |
| 신규 설정 | `candidate_id`, `folder_code`, `name`, `body_text`, `fact_ids`, `evidence_refs` |
| 관계 ADD | `document_id`, `target_document_id`, `base_revision_no`, `target_base_revision_no`, `relation_key`, `reverse_relation_key`, `description`, `operation="ADD"`, `fact_ids`, `evidence_refs` |
| 구조화 사실 | `fact_id`, `actor`, `action`, `recipient`, `object`, `certainty`, `extent`, `time`, `statement`, `evidence_refs`. 선택적으로 `confidence`, `source_alignments` |

actor·recipient·object는 null 또는 `{surface, document_id}`다. 명시 문서 참조가 없으면
그 내부 document_id는 null이다. 신규 ID는 `new:유형:이름`이며 실제 Content 문서 ID가 아니다.
기존 문서 끝의 기준 revision은 정수, 신규 후보 끝은 null이다.
`fact_ids`와 `evidence_refs`는 같은 result 안의 사전에 연결된다.
유사도 보정된 사실은 원래 표현·복원 표현·점수·원문 위치·원래 statement를
`source_alignments`에 보존한다. 이 기록은 근거 원문 자체를 바꾸지 않는다.
각 보정 기록의 필드는 `field`, `original`, `restored`, `similarity`, `start`, `end`,
`original_statement`다. confidence는 있을 때 유한한 0∼1 숫자이며 정답 확률을 보증하지 않는다.
certainty는 `CONFIRMED`, `REPORTED`, `PLANNED`, `UNKNOWN`, extent는 `PARTIAL`,
`COMPLETE`, `UNSPECIFIED`, time은 `CURRENT`, `PAST`, `FUTURE`, `UNSPECIFIED` 중 하나다.

근거 위치는 0부터 시작하는 Unicode 코드 포인트이며 `[start, end)` 구간이다.
UTF-8 바이트 오프셋이나 JavaScript UTF-16 인덱스가 아니다.
해당 revision의 `body_text[start:end]`가 quote와 정확히 일치한다.

`execution`의 기본 필드는 `input_fingerprint`, `model`, `prompt_version`, `schema_version`,
`settings`, `usage`, `changed_ids`, `target_ids`, `observed_graph_version`이다.
usage는 `calls`, `input_tokens`, `output_tokens`이며 저장된 결과 생성 시도의 사용량이다.
현재 후보 검증 버전은 선택적 `candidate_schema_version`으로 구분한다.
execution의 schema_version은 요청의 후보 계약이며 최상위 결과 버전과 다르다.
실제 생성·검증 후보 버전이 있으면 candidate_schema_version을 기준으로 해석한다.

추가 실행 메타데이터는 있을 때만 저장한다.

| 선택 필드 | 내용 |
| --- | --- |
| `change_plan` | 두 단계 호출의 검토 문서·문단 REPLACE/DELETE/APPEND·신규 설정·관계 계획과 계획 사용량 |
| `attempts` | 최초·보완 시도의 범위·단계·판정·사용량·실패·진단. 단계는 PLAN·WRITE 또는 과거 GENERATE |
| `validation_diagnostics` | 통합 검증의 필드·규칙 진단 |
| `source_scope_extensions` | 계획 마커 외에 추가로 사용한 유효 변경 원고 마커 |
| `paragraph_copy_removals` | 서버가 보존할 문단의 완전 복사본을 제거한 작업 ID·문단 ID·제거 전 text |

두 단계로 작성하더라도 소비자에게 반환하는 기존 문서 수정안은 조립된 전체 본문이다.
LLM의 `paragraph_edits` 응답을 S3 결과로 직접 사용하지 않는다.
현재 기본 프롬프트·후보는 v13·v7이며 confidence를 포함하는 두 단계 후보는 v8이다.
결과 reader는 과거 `refresh-result-v1`·`v2`도 읽지만 새 저장기는 v3를 쓴다.
제안의 의미와 고정 결과 예시는 [제안 계약](migration/proposal-contract.md#실행-가능한-결과-변환과-예시)을 따른다.

## 무결성과 불변 저장

| 조건 | 입력 객체 | GraphRAG 출력 객체 |
| --- | --- | --- |
| 바이트 해시 | 요청의 input_fingerprint와 비교 | S3 사용자 메타데이터 `sha256`과 비교 |
| 메타데이터 | sha256 메타데이터 없어도 읽음 | 소문자 16진수 sha256 필수. 없거나 다르면 거절 |
| 업로드 속성 | 어댑터가 입력의 Content-Type은 검사하지 않음 | `ContentType="application/json"`, `ChecksumSHA256=base64(SHA256 바이트)` |
| 객체 크기 | 설정 한도 적용 | 같은 설정 한도로 읽기·쓰기 제한 |

객체 한도는 기본 16,777,216바이트이며 헤더와 실제 읽은 바이트 모두 확인한다.
본문의 5,000 코드 포인트 한도와 객체의 바이트 한도는 별개다.
출력 JSON 바이트는 키 정렬·공백 없는 구분자·비ASCII 문자 유지로 생성하며 NaN은 허용하지 않는다.
수동으로 출력 JSON을 업로드해도 sha256 메타데이터가 없으면 이 어댑터는 읽지 않는다.

출력 쓰기는 `IfNoneMatch="*"`를 사용해 기존 객체를 덮어쓰지 않는다.
기존 context는 바이트가 같을 때 재사용하며, 기존 result는 복원된 결과 값이 같을 때 재사용한다.
조건부 쓰기 경합에서 이미 생성된 객체는 동일 바이트이면 재사용하고 다르면 RequestConflict다.
같은 요청 ID에 다른 입력 위치·해시·모델·선택 설정을 전달하는 것도 manifest 비교로 거절한다.
입력·설정을 바꾸거나 저장된 FAILED를 새로 평가하려면 새 request_id를 사용한다.

## 저장 순서와 소비·복구

정상 계산은 요청 manifest·context를 저장하고 모델 결과를 검증한 뒤 result를 저장한다.
그 다음 완료를 발행하고 전달 확인 후 완료 Inbox에 기록한다. 입력 오류처럼 스냅샷 확보 전
종료한 실패는 manifest·실패 result만 남을 수 있다.

| S3 outcome | 완료 직렬화 outcome | 소비 방법 |
| --- | --- | --- |
| PROPOSED | SUCCEEDED | result 위치에서 제안을 읽어 승인 절차로 전달 |
| NO_CHANGE | SUCCEEDED | result의 빈 제안 목록으로 변경 없음을 구분 |
| FAILED | FAILED | 저장된 result의 error를 읽음. 실패 결과 저장이 불가능하면 result 위치는 null 가능 |

완료 payload는 request_id·project_id·outcome·result 위치·error·prompt_version을 전달하고
본문·제안 목록을 복제하지 않는다. 완료 payload의 error는 code·message만 담는다.
현재 완료 직렬화 함수와 내부 publisher 포트가 있으며 실제 Kafka producer는 연결하지 않았다.

재실행은 저장된 result를 먼저 읽으며 FAILED도 재사용한다. result와 request manifest가
있으면 입력·context가 없어져도 모델 재호출 없이 복구할 수 있다. result와 manifest는
함께 보존해야 한다. 계산 도중인 입력·context를 만료시키는 정책은 피해야 한다.
result 저장 전 모델 호출 이후의 장애는 재호출로 이어질 수 있고 완료 발행은 중복될 수 있다.
소비자는 프로젝트·요청 ID로 중복 처리하고 제안 확정 시 기준 revision을 재확인한다.
S3 객체·완료 Inbox·완료 발행 전체를 묶는 트랜잭션은 없다.
완료 Inbox가 있으면 S3를 읽거나 재생성하지 않는다. 미완료 문맥이 실제로 없으면 입력부터
재실행할 수 있다. 실행 소유권·진행 상태를 별도 DB에 저장하지 않으며,
상세 복구는 [완료 Inbox](implementation/REFRESH_INBOX.md)를 따른다.

## 오류 계약

| 오류 | 의미 | retryable |
| --- | --- | --- |
| `S3_UNAVAILABLE` | 연결·읽기 실패, 5xx, SlowDown, 조건부 쓰기 409 등 일시 실패 | true |
| `S3_ACCESS_DENIED` | 인증·권한 거절 | false |
| `S3_REQUEST_INVALID` | 버킷·요청 오류 등 위 분류에 속하지 않는 S3 응답 | false |
| `S3_CONFIGURATION_INVALID` | SDK·자격증명 설정 실패 | false |
| `S3_OBJECT_MISSING` | 입력 읽기 등에서 필요한 필수 객체가 없음 | false |
| `S3_OBJECT_TOO_LARGE` | 읽기·쓰기 객체 바이트 한도 초과 | false |
| `S3_OBJECT_INVALID` | 출력 메타데이터·JSON·스냅샷·결과 구조 또는 manifest 손상 | false |
| `S3_INPUT_LOCATION_INVALID` | 입력 버킷·prefix 위반 또는 요청 출력과 같은 위치 | false |
| `S3_INPUT_INVALID` | 엄격 입력 JSON 스키마 위반 | false |
| `INPUT_FINGERPRINT_MISMATCH` | 저장 바이트의 SHA-256이 요청 해시와 다름 | false |
| `INPUT_MISMATCH`, `MANIFEST_MISMATCH` 등 rules 오류 | 요청 식별·변경 목록·문서·예산 등 입력 업무 조건 위반 | false |
| `RequestConflict` 예외 | 같은 요청 ID의 입력·설정·저장 내용 충돌 | 실패 결과로 자동 변환하지 않고 호출자에게 전달 |

선택적으로 읽는 request·context·result의 `NoSuchKey`만 미저장으로 처리한다.
AccessDenied·NoSuchBucket을 객체 없음으로 처리하지 않는다. 결과 저장 전에 발생한
재시도 가능 실패나 저장·발행 실패는 서비스 밖으로 전달하며 성공 완료를 발행하지 않는다.
SDK 자동 재시도는 없고 전달 계층이 retryable을 보고 재시도를 결정한다.

## 코드 기준과 검증 범위

JSON 타입은 [refresh_schema.py](../app/adapters/refresh_schema.py),
객체 읽기·쓰기와 위치는 [refresh_artifacts.py](../app/adapters/refresh_artifacts.py),
결과·완료 payload는 [serialization.py](../app/refresh/serialization.py)가 기준이다.
기존 S3 단위 테스트와 boto3의 로컬 HTTP 통합 테스트가 엄격 입력·해시·불변 쓰기·복구를 확인한다.
실제 AWS 버킷의 IAM·KMS·lifecycle와 Content 운영 연동은 이 검증에 포함하지 않았다.
