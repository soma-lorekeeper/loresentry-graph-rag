# Kafka polling과 fake 처리기

`confluent-kafka==2.16.0`의 일반 `Consumer`로 실제 수신·poll·offset commit을 수행한다.
업무 consumer와 결과 producer는 fake다. JSON 계약 변환, 그래프 변경, refresh 실행,
완료 이벤트 발행은 이 진입점에 연결하지 않았다. 기존 fixture consumer는 그대로 유지한다.

## 실행 구조

- [PollingWorker](../../app/kafka/polling.py)는 실행 스레드에서 Kafka 호출을 전담한다.
  `poll(0.1)`로 수신과 rebalance callback을 처리한다.
- 작업 스레드는 하나다. 한 레코드를 처리하는 동안 할당된 **전체 파티션**을 pause한다.
  poll은 계속하지만 다른 파티션도 기다리므로 현재 구성의 처리량은 직렬이다.
- [FakeConsumerHandler](../../app/kafka/fake.py)는 payload를 해석하지 않고 위치와 바이트
  수를 `FakeProducer`에 전달한다. producer는 최근 100개 기록과 누적 개수만 메모리에 보관한다.
  실제 Kafka producer나 완료 이벤트가 아니며 재시작하면 사라진다.
- handler가 정상 반환하면 그 레코드의 `offset + 1`만 동기 commit한다.
  자동 commit·자동 offset 저장·토픽 자동 생성은 꺼져 있다.

업무 handler로 교체할 때는 성공 반환 전에 저장·발행 확인·완료 Inbox 기록까지 끝내야 한다.
polling runtime은 별도 Inbox나 작업 상태 DB를 추가하지 않는다.

## 재할당·실패·종료

현재 설정은 classic group protocol과 eager `range` 할당이다. cooperative assignor로
설정만 변경하면 안 된다. assign/revoke/lost마다 할당 세대를 바꾸고, 이전 세대에서
시작한 작업의 offset은 commit하지 않는다. 진행 중인 Python 호출 자체는 취소하지 못한다.
새 할당도 기존 작업이 끝날 때까지 pause한다. 재전달된 업무의 부수 효과는 기존 Inbox가
중복을 제어해야 하며, 완료 전 동시 실행을 막는 분산 잠금은 없다.

처리·poll·commit 오류는 worker를 종료한다. handler 실패 시 offset은 진행하지 않는다.
commit 응답을 잃으면 실제 commit 여부가 불확실할 수 있으므로 재전달을 허용해야 한다.
자동 재시도·DLQ는 아직 없다. 운영 인증·TLS 설정도 이 sandbox CLI에는 포함하지 않았다.

SIGINT/SIGTERM은 새 작업 수신을 멈춘다. 진행 중 작업은 polling을 유지하며 기다리고,
동일 할당의 성공한 작업을 commit한 뒤 consumer를 닫는다. 오류 종료 때도 작업 스레드의
종료를 기다리므로 실제 handler는 IO timeout을 설정해야 한다. 강제 프로세스 종료 시에는
미완료 작업이 재전달될 수 있다.

## Linux 호스트에서 실행

GraphRAG 저장소 루트에서 프로젝트 의존성을 설치하고, 접근 가능한 테스트 Kafka에
`graph-rag.sandbox.polling` 토픽을 먼저 생성한다. 아래 broker 주소를 테스트 환경 주소로 바꾼다.

```bash
.venv/bin/python -m scripts.poll_kafka \
  --bootstrap-servers 127.0.0.1:19092 \
  --topic graph-rag.sandbox.polling \
  --group-id graph-rag-fake-polling
```

`--topic`은 반복 지정할 수 있다. 이 CLI는 `.env`를 자동으로 읽지 않는다.
종료하면 `fake_published` 개수를 출력한다. key·payload·본문은 출력하지 않는다.
fake 업무 처리 후에도 실제 offset을 commit하므로 CLI는 `graph-rag.sandbox.*` 토픽과
`graph-rag-fake-*` group만 허용한다. Content 업무 토픽을 이 fake CLI로 소비하지 않는다.

## 서버 시작·종료와 배포

[FastAPI lifespan](../../app/main.py)이 [FakePollingRuntime](../../app/kafka/runtime.py)을
시작한다. Uvicorn 프로세스마다 polling 스레드 하나와 작업 스레드 하나를 사용한다.
현재 Docker 시작 명령은 Uvicorn 프로세스 하나이며 별도 polling CLI 실행은 필요 없다.
프로세스나 replica를 늘리면 같은 consumer group 내에서 파티션을 나눠 받는다.

| 환경변수 | 기본값 | 의미 |
| --- | --- | --- |
| `KAFKA_POLLING_ENABLED` | `false` | `true`이면 서버 시작과 함께 fake polling 실행. 다른 값은 설정 오류 |
| `KAFKA_BOOTSTRAP_SERVERS` | 없음 | 활성화 시 필수인 Kafka 주소 |
| `KAFKA_POLLING_TOPICS` | `graph-rag.sandbox.polling` | 쉼표로 구분한 sandbox 토픽 |
| `KAFKA_POLLING_GROUP_ID` | `graph-rag-fake-polling` | fake 전용 consumer group |

서버 경로에도 CLI와 같은 sandbox·group 제한을 적용한다. 잘못된 활성화 설정이나
consumer 생성 오류는 앱 시작을 실패시킨다. 서버 종료 시 stop을 전달하고 진행 중 처리·commit·
consumer close를 기다린다. runtime의 종료 대기 한도는 15초이며 네트워크 요청 timeout은 10초다.
이 한도는 임의의 Python 작업을 강제 취소하는 기능이 아니므로 실제 업무 handler 연결 시
작업 시간 제한과 Pod 종료 예산을 다시 맞춰야 한다.

worker가 실패하거나 예기치 않게 종료되면 `GET /health`는 `503`을 반환한다.
현재 배포의 readiness/liveness probe가 이를 사용해 트래픽 제외·재시작을 수행한다.
`200`은 polling 스레드가 살아 있다는 뜻이며 Kafka 연결·파티션 할당·lag를 보증하지 않는다.

[GraphRAG Deployment](../../../loresentry-gitops/workload/base/graph-rag/deployment.yaml)는
polling을 활성화하고 기존 `kafka` ConfigMap의 `bootstrap-servers`를 주입한다.
[sandbox 토픽 선언](../../../loresentry-gitops/workload/base/kafka/graph-rag-sandbox-topic.yaml)은
1개 파티션·3개 복제본으로 선언하며 토픽 자동 생성은 사용하지 않는다.
서버 이미지 변경과 GitOps 설정이 모두 배포되어야 적용된다. 이 설명은 운영 반영 완료를 뜻하지 않는다.

Linux 호스트에서 테스트 broker를 준비한 뒤 같은 서버 시작 동작을 확인할 수 있다.
GraphRAG 저장소 루트에서 실행한다.

```bash
KAFKA_POLLING_ENABLED=true \
KAFKA_BOOTSTRAP_SERVERS=127.0.0.1:19092 \
  .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

이 예시도 sandbox 토픽을 미리 생성해야 한다. 환경변수를 지정하지 않는 로컬 서버는
기존처럼 Kafka 없이 실행된다. `.env.example`은 설정 안내이며 자동 로딩을 의미하지 않는다.

## 검증

```bash
.venv/bin/python -m pytest tests/test_kafka_polling.py tests/test_kafka_runtime.py -q
KAFKA_TEST_BOOTSTRAP_SERVERS=127.0.0.1:19092 \
  .venv/bin/python -m pytest tests/integration/test_kafka_polling.py -q
```

통합 테스트에는 토픽 생성·삭제가 허용된 일회용 테스트 broker가 필요하다.
Docker를 사용하는 Linux 호스트에서는 다음 구성으로 broker를 준비하고 정리한다.

```bash
docker compose -p graph-rag-kafka-verification -f tests/kafka/compose.yaml up -d --wait
KAFKA_TEST_BOOTSTRAP_SERVERS=127.0.0.1:19092 \
  .venv/bin/python -m pytest tests/integration/test_kafka_polling.py -q
docker compose -p graph-rag-kafka-verification -f tests/kafka/compose.yaml down -v
```

임의 이름의 sandbox 토픽에 실제 Kafka producer로 세 레코드를 넣고, 실제 Consumer가
fake handler를 호출한 뒤 새 클라이언트에서도 commit offset `3`을 읽는지 확인한다.
테스트 토픽은 종료 시 삭제한다. 환경변수가 없으면 통합 테스트는 skip한다.
서버 통합 테스트는 FastAPI 시작만으로 실제 Kafka를 소비하고, 앱 종료 시 worker가 종료되며
완료 offset이 남는지 확인한다. 수명주기 단위 테스트는 비활성 상태·잘못된 설정·시작 실패·
실행 중 실패의 health 반영도 검증한다.
단위 테스트는 긴 처리 중 poll 유지, 실패 시 commit 중단, revoke/lost 후 오래된 완료 무시,
정확한 offset, 종료와 tombstone·헤더 보존을 검증한다.

클라이언트 API 기준은 [Confluent 공식 문서](https://docs.confluent.io/platform/current/clients/confluent-kafka-python/html/index.html)다.
로컬 broker 검증은 운영 Kafka·인증·네트워크·실제 Content 계약의 검증을 대신하지 않는다.
