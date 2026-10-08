# 갱신안 S3 저장소

> **책임:** S3 어댑터의 설정, 입력 형식, 저장·복구 규칙을 설명한다.
>
> **상태:** boto3 기반 구현과 로컬 HTTP 통합 검증 완료. 실제 AWS 버킷·IAM·보존 정책은 미검증이다.

`app/adapters/refresh_artifacts.py`의 `S3RefreshArtifacts`는
`RefreshArtifactStore`를 구현한다. 입력을 읽고 실행 스냅샷과 결과를 저장한다.
원고 본문이나 Content 문서·확정 그래프에 제안을 적용하지 않는다.
기본 평가 CLI는 기존 fake 저장소를 사용한다. 환경변수 추가만으로 CLI나 FastAPI가
S3로 전환되지 않으며, HTTP·Kafka 갱신안 진입점은 아직 없다.

## 서비스에 연결하기

프로젝트 가상환경에 `requirements.txt`의 의존성을 설치한 뒤 서비스 조립 코드에서
어댑터를 주입한다. 아래는 연결 예시이며 `request`, `execution_id`와 나머지 다섯 포트는
호출자가 준비해야 한다. 설정 매핑은 호출자가 명시적으로 전달한다.

```python
import os

from app.adapters.refresh_artifacts import S3ArtifactSettings, s3_artifacts
from app.refresh.service import RefreshService

settings = S3ArtifactSettings.from_environment(os.environ)
with s3_artifacts(settings, region_name=os.environ.get("AWS_REGION")) as artifacts:
    service = RefreshService(
        artifacts=artifacts,
        relations=relations,
        documents=documents,
        model=model,
        jobs=jobs,
        publisher=publisher,
    )
    completion = service.run(request, execution_id)
```

| 설정 | 의미·기본값 |
|---|---|
| `GRAPHRAG_S3_BUCKET` | 실행 스냅샷·결과 버킷. 필수 |
| `GRAPHRAG_S3_INPUT_BUCKET` | 허용할 입력 버킷. 필수. 출력 버킷과 같아도 됨 |
| `GRAPHRAG_S3_PREFIX` | 출력 prefix. 기본 `refresh` |
| `GRAPHRAG_S3_INPUT_PREFIX` | 입력 key가 속해야 하는 prefix. 기본 `refresh` |
| `GRAPHRAG_S3_MAX_OBJECT_BYTES` | 객체별 읽기·쓰기 한도. 기본 `8388608`바이트 |

prefix는 앞뒤 `/`나 빈 경로·`.`·`..` 요소가 없는 상대 경로다.
입력은 지정 버킷과 `input_prefix/` 안에 있어야 하며 해당 요청의 출력 객체와 겹칠 수 없다.
이 범위 검사는 사용자 인가를 대신하지 않는다. 요청 수신 계층에서 프로젝트 접근 권한을 확인해야 한다.

클라이언트는 boto3 표준 자격증명 탐색을 사용한다. 실행 환경의 IAM 역할이나 AWS 설정을
준비하며 키를 코드에 넣지 않는다. 팩토리는 클라이언트를 종료하고 연결 5초·읽기 30초의
타임아웃을 설정한다. 로컬/S3 호환 테스트는 `endpoint_url`을 명시적으로 전달할 수 있다.
버킷 생성, 공개 URL 발급, 삭제, lifecycle·IAM 변경은 수행하지 않는다.
필요한 권한은 입력 객체의 `s3:GetObject`, 출력 객체의 `s3:GetObject`·`s3:PutObject`다.
SSE-KMS 등 버킷 정책에 따른 추가 권한은 운영 환경에서 별도 확인한다.

## 입력 형식과 fingerprint

입력 객체는 `refresh-input-v1`이며 `project_id`, `request_id`, `documents`를 담는다.
각 문서는 내부 `DocumentSnapshot` 필드 이름을 사용한다. 모델 정의와 엄격 검증은
`app/adapters/refresh_schema.py`의 `InputObject`가 기준이다. 알 수 없는 필드와 타입
변환이 필요한 값은 거절하며, ACTIVE 본문은 문자열이어야 한다. TRASHED·DELETED는
본문 `null`을 허용한다. 프로젝트·상태·revision 검증에는 기존 순수 rules를 사용한다.

입력 공급자는 다음 함수로 바이트를 만들 수 있다. `source`는 `InputSnapshot`이다.

```python
from app.adapters.refresh_schema import fingerprint, input_bytes

body = input_bytes(source)
input_fingerprint = fingerprint(body)
# Content가 body를 입력 S3 객체로 저장하고,
# 요청의 job.input_fingerprint에 input_fingerprint를 전달한다.
```

fingerprint는 **S3에 저장할 바이트 전체의 SHA-256 16진수**다. ETag나 문서 revision이
아니며, JSON을 다시 정렬하거나 공백을 바꾸면 달라진다. 입력에는 fingerprint 자체를
넣지 않는다. 읽을 때 바이트 해시와 요청 값을 비교한 뒤 JSON을 검증한다.
본문 문자열의 공백·줄바꿈·이모지를 정규화하지 않는다.

기존 fake 평가의 fingerprint는 자료 구조를 해시한 값이므로 S3 요청에 그대로 쓸 수 없다.
이 입력 형식은 GraphRAG 내부 어댑터 계약이다. Content 필드 매핑과 Kafka의 fingerprint
전달 방식은 별도 합의가 필요하며 이번 구현은 기존 Kafka 메시지 스키마를 변경하지 않는다.

## 저장과 재사용

출력 key는 `prefix/project_id/request_id/{request,context,result}.json`이다.
두 ID는 URL 인코딩해 경로 요소로 사용한다. 객체 형식은 다음과 같다.

| 객체 | 내용 |
|---|---|
| `request.json` | `refresh-request-v1`. 입력 위치·해시·탐색·모델 설정을 포함한 전체 요청 |
| `context.json` | `refresh-context-v1`. 모델 호출 전 확보한 실행 스냅샷 |
| `result.json` | 기존 `refresh-result-v2` 제안·실패 결과. 저장된 v1 읽기 호환 |

쓰기에는 `IfNoneMatch="*"`를 사용한다. 이미 있으면 같은 바이트는 재사용하고 다른 내용은
`RequestConflict`로 거절한다. 같은 요청 ID로 모델·탐색 설정이나 입력을 바꾸는 것도
manifest 비교로 거절한다. 이는 여러 객체·작업 DB·Kafka를 묶는 트랜잭션이 아니다.
[AWS PutObject 계약](https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/put_object.html)을 따른다.

출력에는 SHA-256 메타데이터와 업로드 checksum을 기록한다. 읽을 때 메타데이터 해시를
검증하며 메타데이터 없는 수동 업로드 결과는 거절한다. 최초 스냅샷 저장 전 원래 입력을
다시 읽어 모든 변경 문서가 동일한지 확인하므로 원고 본문을 바꾼 스냅샷은 저장할 수 없다.
이미 저장된 동일 스냅샷은 입력을 다시 읽지 않고 재사용한다.

결과 저장 뒤 응답이 유실돼도 재실행은 결과를 먼저 찾아 모델 재호출 없이 완료 처리를
재개한다. 결과와 request manifest가 남아 있으면 입력·context 만료 후에도 결과 재사용이
가능하다. 결과와 함께 manifest를 보존해야 하며, 처리 중인 context를 만료시키지 않도록
보존 기간을 정해야 한다. 아직 결과가 없는 모델 호출 뒤 장애는 재호출로 이어질 수 있다.
완료 발행·실행 소유권·재시도 스케줄은 주입한 job/publisher 및 전달 계층의 책임이다.

## 오류와 검증

선택적으로 읽는 request/context/result에서 `NoSuchKey`만 미저장으로 처리한다.
입력 누락, 없는 버킷, 권한 거절은 구분한다. 객체 크기는 헤더와 실제 읽은 바이트 모두
확인하고 스트림을 닫는다. 오류에는 원고, 자격증명, SDK의 원문 응답을 포함하지 않는다.

| 실패 | 처리 |
|---|---|
| `S3_UNAVAILABLE` | 타임아웃·연결 실패·5xx·조건부 쓰기 충돌(409). 재시도 가능 |
| `S3_ACCESS_DENIED` | 인증·권한 거절. 재시도 불가 |
| `S3_REQUEST_INVALID`, `S3_CONFIGURATION_INVALID` | 요청·버킷·SDK/자격증명 설정 오류 |
| `S3_OBJECT_MISSING`, `S3_OBJECT_TOO_LARGE`, `S3_OBJECT_INVALID` | 필수 객체 누락·용량 초과·출력 무결성/형식 오류 |
| `S3_INPUT_LOCATION_INVALID`, `S3_INPUT_INVALID`, `INPUT_FINGERPRINT_MISMATCH` | 입력 범위·JSON·바이트 해시 오류 |

SDK 자동 재시도는 끄며 한 번의 포트 호출 안에서 재시도하지 않는다. 내부 rules에서
발생하는 입력 오류와 `RequestConflict`는 기존 계약대로 전달한다.
`tests/refresh/test_s3_artifacts.py`는 엄격 타입·크기·스트림 종료·설정을,
`tests/integration/test_s3_http.py`는 실제 boto3의 로컬 HTTP 전송, 불변 쓰기,
새 어댑터의 결과 복구, 원고 보존, 오류 분류와 무재시도를 확인한다.
로컬 서버 검증은 실제 AWS의 IAM·TLS·KMS·내구성·동시 실행 보장을 입증하지 않는다.
