# refresh 완료 Inbox와 재실행

refresh는 진행 상태나 실행 소유권을 저장하지 않는다. 완료 Inbox가 있으면 반환하고,
없으면 S3 결과·실행 문맥을 재사용하거나 다시 실행한다. 별도 작업 DB와 lease는 두지 않는다.

## 처리 순서

1. `RefreshInbox.find(request)`로 완료를 확인한다. 있으면 저장된 완료 값을 반환하며 S3·모델·publisher를 호출하지 않는다.
2. 미완료이면 S3 결과를 읽는다. 이미 저장된 결과는 `FAILED`도 그대로 재사용한다.
3. 결과가 없으면 저장된 실행 문맥을 사용한다. 문맥도 없으면 입력·Neptune 원홉·Content를 읽고 문맥을 저장한 뒤 모델을 호출한다.
4. 결과를 불변 저장한다.
5. `publisher.publish(completion)`으로 완료 이벤트를 발행하고 전달 확인을 기다린다.
6. `RefreshInbox.complete(request, completion)`으로 완료를 기록한다.
7. 서비스가 정상 반환한 뒤 전달 계층이 offset을 커밋한다.

재시도 불가능한 계산 오류는 제안 없는 `FAILED` 결과로 저장·발행하고 완료를 기록한다.
일시 장애와 예상하지 못한 오류는 전파하며 완료를 남기지 않는다.

## 구현과 연결

| 코드 | 책임 |
|---|---|
| [ports.py](../../app/refresh/ports.py) | `RefreshInbox.find`, `complete` 계약 |
| [inbox.py](../../app/refresh/inbox.py) | 요청 전체의 결정적 SHA-256, 완료 값의 요청 식별 검사 |
| [refresh_inbox.py](../../app/adapters/refresh_inbox.py) | 실제 Neptune 완료 Inbox 조회·원자적 삽입·충돌 검사 |
| [service.py](../../app/refresh/service.py) | 결과 저장·발행·Inbox 순서와 S3 재사용 |
| [neptune_refresh.py](../../app/adapters/neptune_refresh.py) | Neptune 원홉·완료 Inbox와 Content HTTP 조립 |

`neptune_refresh_service(...)`는 `inbox`를 생략하면 기존 Neptune writer 설정으로
`NeptuneRefreshInbox`를 만든다. `documents` 생략 시의 Content 설정은
[Content HTTP 조회](CONTENT_HTTP.md)를 따른다. 호출 측은 `artifacts`, `model`,
`publisher`, 신뢰한 `user_id`를 전달하고 `service.run(request)`를 호출한다.
`jobs`와 `execution_id` 인자는 제거했다. 평가 도구는 `FakeInbox`를 명시적으로 주입한다.
환경변수 추가만으로 FastAPI나 Kafka worker가 이 서비스를 실행하지는 않는다.

## 저장 모델

완료 기록도 기존 Neptune의 `InboxEvent` label을 사용하지만 그래프 투영 처리기와는
독립적이다. 업무 중복 단위는 `(consumer_name, project_id, request_id)`다.
Kafka의 다른 이벤트 ID로 동일 요청이 재전달되어도 이 완료 기록을 재사용한다.
이는 그래프 변경의 `(consumer_name, event_id)` Inbox와 다른 논리 처리 계약이다.

| 항목 | 값 |
|---|---|
| vertex ID | `inbox:graph-rag-refresh:<project_id>:<request_id>` |
| `consumer_name` | `graph-rag-refresh` 고정 |
| `project_id`, `request_id` | 정규 UUID 문자열 |
| `request_sha256` | 입력 위치·fingerprint·시드·대상·모델·선택 설정을 포함한 요청 전체의 해시 |
| `completion_json` | `refresh-inbox-v1`. 결과 S3 위치·업무 결과·오류·프롬프트 버전이 있는 완료 값 |
| `processed_at` | 이번 완료 삽입의 UTC 시각 |

본문·후보 목록·`RUNNING`·`RETRYABLE` 같은 진행 상태는 넣지 않는다.
동일 요청의 재기록은 같은 완료 값일 때만 성공하며 처리 시각을 갱신하지 않는다.
같은 요청 ID에 다른 입력·설정·완료를 사용하면 `RequestConflict`다.
완료 삽입의 중복 검사와 쓰기는 Gremlin 요청 하나로 묶는다.
Neptune 조회 실패·손상된 완료 기록을 미처리로 대체하지 않는다.

## 장애와 동시 실행

| 실패 시점 | 다음 실행 |
|---|---|
| 실행 문맥 저장 전 | 입력부터 재실행 가능 |
| 문맥 저장 후, 결과 저장 전 | 문맥 재사용. 완료되지 않은 모델 호출은 반복 가능 |
| 결과 저장 후, 완료 발행 전 | 저장 결과로 발행 재시도 |
| 발행 성공 후 Inbox 저장 전 | 저장 결과로 완료를 다시 발행할 수 있음 |
| Inbox 커밋 응답 유실 | 다음 조회에 기록이 있으면 발행 없이 반환 |
| Inbox 저장 후 offset 커밋 전 | 재수신해도 완료 값만 반환 |

완료 기록은 실행 잠금이 아니다. rebalance 등으로 실행이 겹치면 모델 비용과 발행이 중복될
수 있다. 전달 계층은 파티션별 처리·poll 유지·소유권 상실 시 진행 중 작업 관리를 수행해야 한다.
Content는 `request_id` 기준 업무 중복도 처리해야 한다.

S3 조건부 저장은 기존 문맥·결과를 덮어쓰지 않는다. 동시 실행의 저장 경합에서는
동일 요청의 먼저 저장된 문맥·결과를 읽어 재사용한다. 입력·설정 충돌은 계속 거절한다.
저장소 접근·해석 실패는 전파하지만 실제로 없는 미완료 문맥은 입력부터 재구성할 수 있다.
완료 Inbox가 있으면 오래된 S3 객체가 없어도 재생성하지 않는다. S3 보존과 결과 수용은
별도 운영 계약이며 Inbox·S3·Kafka 전체가 하나의 트랜잭션인 것은 아니다.

기존 저장 결과는 검증 정책 변경으로 덮어쓰지 않는다. 변경한 정책으로 다시 평가하려면
새 `request_id`를 사용한다. 프로젝트 삭제는 그래프의 `File`·`REFERENCES`만 제거하므로
완료 Inbox는 보존된다. 초기에는 Inbox 자동 만료를 하지 않는다.

## 검증

Linux 호스트의 저장소 루트에서 프로젝트 환경으로 실행한다.

```bash
.venv/bin/python -m pytest tests/refresh/test_recovery.py tests/test_refresh_inbox.py -q
```

[전용 Gremlin 서버](GRAPH_PROJECTION.md#검증)를 실행한 뒤 실제 저장·재시작·응답 유실과
Content HTTP 연결을 검증할 수 있다.

```bash
GREMLIN_TEST_ENDPOINT=http://127.0.0.1:18182 .venv/bin/python -m pytest tests/integration/test_refresh_inbox_gremlin.py tests/integration/test_refresh_neptune.py -q
```

이 검증은 로컬 Gremlin·HTTP 대체 서버를 사용한다. 운영 AWS Neptune·Content·Kafka
연동과 중복 실행 비용의 운영 한도는 검증하지 않는다.
