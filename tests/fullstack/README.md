# 두 서비스 전체 연동 검증

Linux 호스트에서 실제 Content HTTP·PostgreSQL·Outbox relay·Kafka·GraphRAG worker·Gremlin·
S3 호환 서버·완료 Kafka·Content Inbox/READY를 검증한다. 모델만 NoChangeModel로 대체하며
원격 OpenAI 호출이나 AWS 자격증명은 사용하지 않는다. 테스트 계정과 버킷은 로컬 전용이다.
S3 이미지는 검증한 digest로 고정하고 볼륨은 소용량으로 제한한다.

## 준비와 실행

Content 저장소에서 Java 21 환경의 `./gradlew bootJar`로 JAR를 먼저 만든다.
실행 중인 테스트 Content에 마운트한 JAR를 재빌드했다면 서버를 다시 시작해야 한다.
다음 명령은 GraphRAG 저장소 루트에서 프로젝트 가상환경을 사용한다.

```bash
docker compose -p graph-rag-kafka-verification -f tests/kafka/compose.yaml up -d --wait
docker compose -p graph-rag-gremlin-verification -f tests/gremlin/compose.yaml up -d
.venv/bin/python -m scripts.prepare_test_topics
docker compose -p graph-rag-fullstack-verification -f tests/fullstack/compose.yaml up -d --wait
CONTENT_TEST_BASE_URL=http://127.0.0.1:18002 \
GREMLIN_TEST_ENDPOINT=http://127.0.0.1:18182 \
KAFKA_TEST_BOOTSTRAP_SERVERS=127.0.0.1:19092 \
  .venv/bin/python -m pytest tests/integration/test_content_roundtrip.py -q
```

prepare_test_topics는 고정 localhost:19092의 일회용 broker에 업무 토픽을 준비한다.
운영 broker에는 실행하지 않는다. Kafka 단독 transport 테스트는 기존 업무 토픽이 없는 별도 broker가 필요하므로
이 구성의 정리 후 실행한다. 호스트 포트 15432·18002·18333·18182·19092가 비어 있어야 한다.
S3 서버의 내부 포트도 127.0.0.1로 바인딩한다.

## 정리

테스트 이름으로 생성한 리소스만 제거한다.

```bash
docker compose -p graph-rag-fullstack-verification -f tests/fullstack/compose.yaml down -v
docker compose -p graph-rag-kafka-verification -f tests/kafka/compose.yaml down -v
docker compose -p graph-rag-gremlin-verification -f tests/gremlin/compose.yaml down -v
```

로컬 TinkerTransactionGraph와 S3 호환 검증은 운영 AWS Neptune/S3의 IAM·동시성·내구성·
실제 모델 품질·Argo CD rollout 검증을 대체하지 않는다.
