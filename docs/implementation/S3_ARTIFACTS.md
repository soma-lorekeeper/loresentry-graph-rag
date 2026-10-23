# 갱신안 S3 저장소

> **책임:** S3 어댑터의 설정·서비스 주입·운영 검증 범위를 설명한다.
>
> **상태:** boto3 기반 구현과 로컬 HTTP 통합 검증 완료. 실제 AWS 버킷·IAM·보존 정책은 미검증이다.

`app/adapters/refresh_artifacts.py`의 `S3RefreshArtifacts`는
`RefreshArtifactStore`를 구현한다. 입력을 읽고 실행 스냅샷과 결과를 저장한다.
원고 본문이나 Content 문서·확정 그래프에 제안을 적용하지 않는다.
기본 평가 CLI는 기존 fake 저장소를 사용한다. 환경변수 추가만으로 CLI나 FastAPI가
S3로 전환되지 않으며, HTTP·Kafka 갱신안 진입점은 아직 없다.

## 서비스에 연결하기

프로젝트 가상환경에 `requirements.txt`의 의존성을 설치한 뒤 서비스 조립 코드에서
어댑터를 주입한다. 아래는 연결 예시이며 `request`와 나머지 다섯 포트는
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
        inbox=inbox,
    )
    completion = service.run(request)
```

| 설정 | 의미·기본값 |
|---|---|
| `GRAPH_REFRESH_COMPLETED_BUCKET` | 실행 스냅샷·결과 버킷. 필수 |
| `GRAPH_REFRESH_REQUEST_BUCKET` | 허용할 입력 버킷. 필수. 출력 버킷과 같아도 됨 |
| `GRAPH_REFRESH_PREFIX` | 출력 prefix. 기본 `refresh` |
| `GRAPH_REFRESH_INPUT_PREFIX` | 입력 key가 속해야 하는 prefix. 기본 `refresh` |
| `GRAPH_REFRESH_MAX_OBJECT_BYTES` | 객체별 읽기·쓰기 한도. 기본 `16777216`바이트 |

prefix는 앞뒤 `/`나 빈 경로·`.`·`..` 요소가 없는 상대 경로다.
입력은 지정 버킷과 `input_prefix/` 안에 있어야 하며 해당 요청의 출력 객체와 겹칠 수 없다.
이 범위 검사는 사용자 인가를 대신하지 않는다. 요청 수신 계층에서 프로젝트 접근 권한을 확인해야 한다.

클라이언트는 boto3 표준 자격증명 탐색을 사용한다. 실행 환경의 IAM 역할이나 AWS 설정을
준비하며 키를 코드에 넣지 않는다. 팩토리는 클라이언트를 종료하고 연결 10초·읽기 60초의
타임아웃을 설정한다. 로컬/S3 호환 테스트는 `endpoint_url`을 명시적으로 전달할 수 있다.
버킷 생성, 공개 URL 발급, 삭제, lifecycle·IAM 변경은 수행하지 않는다.
필요한 권한은 입력 객체의 `s3:GetObject`, 출력 객체의 `s3:GetObject`·`s3:PutObject`다.
SSE-KMS 등 버킷 정책에 따른 추가 권한은 운영 환경에서 별도 확인한다.

## 입출력 계약과 저장 규칙

객체 위치·JSON 필드·fingerprint·출력 메타데이터·불변 저장·완료 전달은
[S3 입출력 계약](../S3_IO_CONTRACT.md)을 기준으로 한다.
입력은 Content가 만드는 `refresh-input-v2`, 요청은 `refresh-request-v1`, 스냅샷은
`refresh-context-v1`, 새 결과 저장은 `refresh-result-v3`다.
과거 `refresh-result-v1`·`v2` 결과는 읽기 호환한다.

테스트·평가는 Content 대신 `app.adapters.refresh_schema.input_bytes(source)`로 v2 저장 바이트를
만들고 `fingerprint(body)`로 그 바이트의 해시를 계산할 수 있다. `source`는 `InputSnapshot`이다.
기존 fake 평가의 fingerprint는 자료 구조를 해시한 값이므로 S3 요청에 그대로 사용할 수 없다.

쓰기에는 `IfNoneMatch="*"`를 사용하고 기존 객체를 덮어쓰지 않는다.
request manifest·context·result와 [완료 Inbox](REFRESH_INBOX.md)·완료 발행은 하나의 트랜잭션이 아니다.
상세 재사용·만료·복구 조건은 [계약의 저장·복구 규칙](../S3_IO_CONTRACT.md#저장-순서와-소비복구)을 따른다.

## 오류와 검증

선택적으로 읽는 request/context/result에서 `NoSuchKey`만 미저장으로 처리한다.
입력 누락, 없는 버킷, 권한 거절은 구분한다. 객체 크기는 헤더와 실제 읽은 바이트 모두
확인하고 스트림을 닫는다. 오류에는 원고, 자격증명, SDK의 원문 응답을 포함하지 않는다.

오류 코드·retryable과 객체 없음의 구분은
[입출력 오류 계약](../S3_IO_CONTRACT.md#오류-계약)을 따른다.

SDK 자동 재시도는 끄며 한 번의 포트 호출 안에서 재시도하지 않는다. 내부 rules에서
발생하는 입력 오류와 `RequestConflict`는 기존 계약대로 전달한다.
`tests/refresh/test_s3_artifacts.py`는 엄격 타입·크기·스트림 종료·설정을,
`tests/integration/test_s3_http.py`는 실제 boto3의 로컬 HTTP 전송, 불변 쓰기,
새 어댑터의 결과 복구, 원고 보존, 오류 분류와 무재시도를 확인한다.
로컬 서버 검증은 실제 AWS의 IAM·TLS·KMS·내구성·동시 실행 보장을 입증하지 않는다.
