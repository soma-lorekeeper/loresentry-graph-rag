# Kafka 소비

GraphRAG는 Content의 확정 변경과 갱신안 요청을 별도 업무 worker로 소비한다.
실제 wire 변환·Neptune 반영·refresh 실행·S3/Content/LLM 조립·서버 시작/종료 연결을 구현했다.
상세 계약·설정·검증은 [Kafka 업무 송수신](implementation/KAFKA_TRANSPORT.md)이 기준이다.

| 토픽 | 이벤트 | key | group |
| --- | --- | --- | --- |
| content.project.changed.v1 | FileChanged·ProjectDeleted | project_id | graph-rag.project-changed |
| content.graph-refresh.requested.v1 | GraphRefreshRequested | project_id | graph-rag.graph-refresh-requested |

업무별 enable은 기본 false다. sandbox fake worker는 별도로 남기며 업무 토픽을 처리하지 않는다.
GraphRAG는 auth.user.deleted.v1을 직접 소비하지 않는다. Content의 프로젝트 삭제 이벤트로 전파한다.

## 변환과 저장

공통 parser는 ce_type → 필수 헤더 → JSON → project key·subject를 확인한다.
FileChanged subject는 file_id, ProjectDeleted는 project_id, refresh 요청은 request_id다.
알 수 없는 필드는 무시하며 미지원 ce_type은 경고 후 skip한다. 알려진 종류의 잘못된 값은 거절한다.
변경 이벤트는 ce_sequence를 요구하고 revision_no와 구분한다.

[handlers.py](../app/kafka/handlers.py)가 전체 문서·관계 스냅샷을 내부 ProjectionChange로 변환한다.
문서별 순서 fence·관계 스냅샷 교체·문서/프로젝트 삭제 tombstone과 Inbox를 원자적으로 기록한다.
옛 이벤트나 삭제 뒤 더 큰 seq의 잘못된 이벤트로 데이터를 부활시키지 않는다.
기존 [그래프 투영](implementation/GRAPH_PROJECTION.md)의 sequence 없는 fixture 경로와 구분한다.

갱신안은 입력 해시·신뢰한 user_id·모델 profile·변경 문서 manifest를 내부 RefreshRequest에 연결한다.
required_events로 입력 생성 시 필요한 투영 이벤트들이 처리됐는지 확인하고 생성한다.
완료 Inbox는 요청 ID와 요청 해시로 중복·충돌을 판정하며 실행 소유권 잠금은 아니다.
[완료 Inbox](implementation/REFRESH_INBOX.md)와 불변 S3 재사용으로 중단을 복구한다.

## 재시도와 DLQ

개별 일시 장애는 최초 시도 뒤 1·2·4·8·10초로 재시도한다. 소진되거나 계약이 잘못된 메시지는
소비자별 DLQ에 발행한다. 완료/DLQ 전달 확인 실패와 분류하지 않은 오류는 정상 반환하지 않는다.
원본 payload·key와 ce_*를 보존하고 안전한 위치·오류 클래스 진단만 추가한다.
원본 6개 파티션의 번호를 3개 파티션 DLQ에 강제하지 않는다.

## poll·offset·종료

자동 commit·offset 저장·토픽 자동 생성을 끈다. 실제 작업 중 전체 할당을 pause하고 poll은 계속한다.
처리가 끝난 레코드의 offset+1만 동기 commit하며 할당 세대가 달라지면 옛 작업을 commit하지 않는다.
두 업무 worker를 분리했으므로 긴 refresh가 투영 소비를 막지 않는다.
현재 worker는 classic/range 전제다. 설정만으로 다른 group protocol로 바꾸지 않는다.

SIGTERM은 새 작업과 다음 모델 호출을 막고 현재 IO의 제한 시간을 기다린다.
작업·전송·Pod 종료의 예산과 실패 health 반영은 [송수신 구현](implementation/KAFKA_TRANSPORT.md#실행-설정)을 따른다.

## Content 완료 수신

Content는 GraphRefreshCompleted를 검증하고 자신이 발행한 요청과 입력 fingerprint·v3 결과를 대조한다.
후보 JSON·READY/FAILED·Inbox를 함께 저장한다. 같은 요청의 중복 완료가 후보를 다시 만들지 않는다.
원고·설정·관계는 사용자 승인 없이 수정하지 않는다. 요청 timeout·삭제 뒤 늦은 결과는 무시·정리한다.
운영 활성화와 최소 권한은 [배포 안내](../deploy/README.md)를 따른다.
