# Spring Kafka Outbox 구현 예시

주문 저장과 이벤트 발행을 연결하는 Java·PostgreSQL 예시다. 실제 Lore Sentry의 이벤트 이름·헤더·topic 설정은 메시지 계약 초안과 구분한다.

관련 문서: [발행 내부 흐름](producer-flow.md), [메시지 계약 초안](message-contract.md), [Inbox 구현](inbox.md).

### 참고사항

- 이 섹션이 답하는 질문: DB 변경과 이벤트 발행을 안전하게 묶으려면 코드를 어떻게 짜고, 무엇을 신경 써야 하나.
- 예시 도메인: 주문 서비스가 주문을 저장하면서 `order.events.v1` topic에 OrderPlaced 이벤트를 발행함. 이름과 값은 모두 예시.
- 전제: Spring Boot 4.1 + spring-kafka 4.1(kafka-clients 4.2), Java 21, PostgreSQL, JdbcClient, Jackson 3. 메시지 key와 value는 문자열(value는 JSON), 이벤트 id는 헤더 event-id로 보냄.
- 전체 흐름: 도메인 트랜잭션이 주문과 outbox 행을 함께 커밋 → relay가 주기적으로 outbox를 읽어 KafkaTemplate으로 발행 → 성공한 행에 발행 표시.
- 라이브러리와 broker 안에서 일어나는 일은 [발행 내부 흐름](producer-flow.md) 참조.

## 1. 의존성·설정·topic

Kafka를 쓰려면 Spring Boot의 Kafka starter를 넣음. 이 starter가 spring-kafka를, spring-kafka가 kafka-clients를 가져옴. outbox를 쓰려면 JDBC도, payload를 JSON으로 만들려면 Jackson도 필요함.

```groovy
dependencies {
    implementation 'org.springframework.boot:spring-boot-starter-kafka'
    implementation 'org.springframework.boot:spring-boot-starter-jdbc'
    implementation 'org.springframework.boot:spring-boot-starter-jackson'   // 웹 starter가 있으면 이미 들어 있음
    runtimeOnly 'org.postgresql:postgresql'

    testImplementation 'org.springframework.boot:spring-boot-starter-kafka-test'
    testImplementation 'org.springframework.boot:spring-boot-testcontainers'
    testImplementation 'org.testcontainers:testcontainers-kafka'
    testImplementation 'org.testcontainers:testcontainers-postgresql'
    testImplementation 'org.testcontainers:testcontainers-junit-jupiter'
}
```

설정은 `spring.kafka.*`에 적음. Spring Boot가 이 값들을 kafka-clients 설정으로 옮겨 ProducerFactory와 KafkaTemplate 빈을 만들어 줌. 전용 속성이 없는 kafka-clients 설정은 `properties` 아래에 kafka-clients 이름 그대로 적음.

```yaml
spring:
  kafka:
    bootstrap-servers: kafka-1:9092,kafka-2:9092,kafka-3:9092
    client-id: order-service
    producer:
      key-serializer: org.apache.kafka.common.serialization.StringSerializer
      value-serializer: org.apache.kafka.common.serialization.StringSerializer
      acks: all
      compression-type: lz4
      properties:
        enable.idempotence: true # 명시해 두면 acks 등을 잘못 바꿨을 때 앱 시작 시 예외로 드러남
        linger.ms: 10
        request.timeout.ms: 15000
        delivery.timeout.ms: 30000 # relay가 결과를 기다리는 상한과 맞춤 (4번)
        max.block.ms: 5000 # send()가 메타데이터나 buffer를 기다리며 멈추는 상한
```

각 값을 이렇게 정한 이유. 나머지는 kafka-clients 기본값을 그대로 씀.

| 설정                                    | 기본값        | 예시 값       | 이유                                                                                                                                                                                  |
| --------------------------------------- | ------------- | ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `acks`                                  | all           | all           | ISR 전원이 기록해야 성공. 기본값이지만 명시해 두면 누가 바꿀 때 눈에 띔.                                                                                                              |
| `enable.idempotence`                    | true          | true (명시)   | 재시도로 생기는 중복과 순서 역전을 broker가 거름. 명시하지 않으면 acks를 all이 아니게 바꾸는 순간 조용히 꺼짐.                                                                        |
| `linger.ms`                             | 5             | 10            | relay는 한 번에 여러 행을 보내므로 조금 더 모아 보내면 요청 수가 줄고 압축률이 오름.                                                                                                  |
| `compression.type`                      | none          | lz4           | JSON은 압축이 잘 됨. 네트워크와 broker 디스크를 아낌.                                                                                                                                 |
| `request.timeout.ms`                    | 30000         | 15000         | 요청 하나의 응답을 기다리는 상한. 넘으면 재시도.                                                                                                                                      |
| `delivery.timeout.ms`                   | 120000        | 30000         | send()부터 성공이나 실패가 확정되기까지의 상한. relay는 이 시간 동안 DB 잠금을 잡고 기다리므로 줄임. `linger.ms`와 `request.timeout.ms`를 더한 값 이상이어야 함(아니면 시작 시 예외). |
| `max.block.ms`                          | 60000         | 5000          | 메타데이터를 못 받거나 buffer가 찼을 때 send()가 멈추는 상한. topic이 없으면 행마다 이만큼 멈추므로 짧게.                                                                             |
| `retries`                               | 사실상 무제한 | 건드리지 않음 | 재시도 한도는 `delivery.timeout.ms`가 정함. 0으로 두면 idempotence가 꺼짐.                                                                                                            |
| `max.in.flight.requests.per.connection` | 5             | 건드리지 않음 | idempotence는 5 이하에서만 순서를 지킴. 5를 넘기면 시작 시 예외.                                                                                                                      |

topic은 운영 환경에서 미리 만들어 두는 것이 원칙. 앱이 뜰 때 만들게 하려면 NewTopic 빈을 등록함. Spring Boot의 KafkaAdmin이 없으면 만들고, 있으면 건드리지 않음.

```java
@Configuration
class KafkaTopicConfig {

    @Bean
    NewTopic orderEvents() {
        return TopicBuilder.name("order.events.v1")
                .partitions(6)
                .replicas(3)
                .config(TopicConfig.MIN_IN_SYNC_REPLICAS_CONFIG, "2")
                .build();
    }
}
```

**신경 쓸 점**

- idempotence가 조용히 꺼지는 조건: `enable.idempotence`를 명시하지 않은 상태에서 `acks`를 all이 아니게 하거나 `retries`를 0으로 두면, 로그 한 줄만 남기고 꺼짐. 명시해 두면 같은 상황에서 앱이 시작하지 못하고 예외가 남.
- `spring.kafka.producer.transaction-id-prefix`를 설정해도 DB 트랜잭션과 Kafka 발행이 하나로 묶이지 않음. DB와 묶는 것은 outbox의 몫.
- partition 수를 나중에 늘리면 같은 key가 다른 partition으로 가서 순서가 섞임. 처음부터 넉넉하게 잡음.
- **topic을 Terraform, Strimzi 같은 도구로 관리한다면 NewTopic 빈은 두지 않음. 정의가 두 곳으로 갈라짐.**

## 2. Outbox 테이블

outbox는 "보내야 할 이벤트"를 적어 두는 테이블. 도메인 테이블과 같은 DB에 있어야 같은 트랜잭션으로 묶임.

```sql
CREATE TABLE outbox_event (
    id             UUID         PRIMARY KEY,                    -- 이벤트 id. 헤더 event-id로 나감
    seq            BIGINT       GENERATED ALWAYS AS IDENTITY,   -- 발행 순서
    aggregate_type VARCHAR(50)  NOT NULL,                       -- 예: ORDER
    aggregate_id   UUID         NOT NULL,
    event_type     VARCHAR(100) NOT NULL,                       -- 예: OrderPlaced
    event_key      VARCHAR(100) NOT NULL,                       -- Kafka key. 순서가 필요한 단위(보통 aggregate id)
    payload        JSONB        NOT NULL,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    published_at   TIMESTAMPTZ,                                 -- 발행 성공 시각. NULL이면 아직 안 보냄
    attempts       INT          NOT NULL DEFAULT 0,             -- 발행 실패 횟수
    last_error     TEXT,
    quarantined_at TIMESTAMPTZ                                  -- 더 시도하지 않기로 격리한 시각 (5번)
);

-- relay가 찾는 행(미발행, 미격리)만 담는 부분 인덱스. 발행된 행이 쌓여도 커지지 않음
CREATE INDEX ix_outbox_event_pending
    ON outbox_event (seq)
    WHERE published_at IS NULL AND quarantined_at IS NULL;
```

발행 순서를 created_at이 아니라 seq로 정한 이유: PostgreSQL의 now()는 트랜잭션이 시작한 시각이라, 늦게 시작해 먼저 커밋한 행이 더 뒤의 시각을 가질 수 있음. seq는 INSERT하는 순간 번호를 받음.

**신경 쓸 점**

- Kafka key를 payload 안에서 꺼내지 말고 컬럼으로 따로 둠. relay가 JSON을 파싱하지 않아도 되고, key별 순서 처리(4번)에도 씀.
- 발행된 행을 지우는 정리 작업이 없으면 테이블이 계속 커짐 (5번).

## 3. 도메인 트랜잭션에서 outbox 쓰기

이벤트를 Kafka로 바로 보내지 않고, 도메인 변경과 같은 트랜잭션에서 outbox 행을 INSERT함. 커밋되면 둘 다 남고, 롤백되면 둘 다 없음.

```java
public record OrderPlaced(UUID orderId, UUID customerId, long totalAmount, int version, Instant occurredAt) {
}

@Service
public class OrderService {

    private final OrderRepository orders;
    private final OutboxWriter outbox;

    public OrderService(OrderRepository orders, OutboxWriter outbox) {
        this.orders = orders;
        this.outbox = outbox;
    }

    @Transactional
    public Order placeOrder(PlaceOrderCommand command) {
        Order order = orders.insert(command);                        // 1. 도메인 쓰기

        OrderPlaced event = new OrderPlaced(order.id(), order.customerId(),
                order.totalAmount(), order.version(), Instant.now());
        outbox.append("ORDER", order.id(), "OrderPlaced",            // 2. 같은 트랜잭션에서 outbox 쓰기
                order.id().toString(), event);

        return order;                                                // 3. 메서드가 끝나면 둘이 함께 커밋
    }
}
```

outbox 쓰기는 작은 컴포넌트로 빼 둠. Propagation.MANDATORY라서 트랜잭션 밖에서 부르면 바로 예외가 나고, 실수로 따로 커밋되는 일을 막음.

```java
@Component
public class OutboxWriter {

    private final JdbcClient jdbc;
    private final JsonMapper json;

    public OutboxWriter(JdbcClient jdbc, JsonMapper json) {
        this.jdbc = jdbc;
        this.json = json;
    }

    /** 호출한 쪽의 트랜잭션 안에서만 실행됨. 트랜잭션이 없으면 예외 */
    @Transactional(propagation = Propagation.MANDATORY)
    public UUID append(String aggregateType, UUID aggregateId, String eventType, String key, Object event) {
        UUID eventId = UUID.randomUUID();
        jdbc.sql("""
                INSERT INTO outbox_event (id, aggregate_type, aggregate_id, event_type, event_key, payload)
                VALUES (:id, :aggregateType, :aggregateId, :eventType, :key, CAST(:payload AS jsonb))
                """)
            .param("id", eventId)
            .param("aggregateType", aggregateType)
            .param("aggregateId", aggregateId)
            .param("eventType", eventType)
            .param("key", key)
            .param("payload", json.writeValueAsString(event))   // 직렬화 실패도 이 트랜잭션 안에서 터짐
            .update();
        return eventId;
    }
}
```

**신경 쓸 점**

- 여기서 KafkaTemplate.send()를 부르지 않음 (dual write). 트랜잭션이 롤백돼도 메시지는 이미 나가고, 반대로 커밋 직전에 죽으면 주문은 있는데 이벤트가 없음.
- payload 직렬화는 트랜잭션 안에서 함. 실패하면 주문까지 롤백되어 '이벤트 없는 주문'이 생기지 않음.
- 같은 주문을 동시에 바꾸는 트랜잭션들은 주문 행 잠금(UPDATE 또는 SELECT … FOR UPDATE)으로 줄을 세워야 함. 그래야 seq 순서가 실제 변경 순서와 같아짐.
- payload에는 '무엇이 바뀌었나'보다 '그 시점의 상태'와 버전을 담는 편이 consumer가 중복과 순서 역전을 다루기 쉬움 ([Inbox 처리 서비스](inbox.md#3-listener와-처리-서비스)).

## 4. Relay 구현

relay는 커밋된 outbox 행을 주기적으로 읽어 Kafka로 보내는 스케줄 작업. 두 부분으로 나눔: 언제 돌지 정하는 OutboxRelay와, 한 번에 한 배치를 트랜잭션 안에서 보내는 OutboxPublisher.

```java
@Configuration
@EnableScheduling
class SchedulingConfig {
}

@Component
public class OutboxRelay {

    private static final int BATCH_SIZE = 100;
    private static final int MAX_BATCHES_PER_RUN = 10;

    private final OutboxPublisher publisher;

    public OutboxRelay(OutboxPublisher publisher) {
        this.publisher = publisher;
    }

    /** 이전 실행이 끝나고 1초 뒤에 다시 실행 */
    @Scheduled(fixedDelay = 1000)
    public void relay() {
        // 배치가 꽉 찼고 문제없이 보냈으면 밀린 행이 더 있다는 뜻이라 쉬지 않고 이어서 보냄. 한 번에 너무 오래 돌지 않게 상한을 둠
        // 종료 요청(인터럽트)이 오면 새 배치를 시작하지 않음
        for (int i = 0; i < MAX_BATCHES_PER_RUN && !Thread.currentThread().isInterrupted(); i++) {
            if (!publisher.publishBatch(BATCH_SIZE)) {
                return;                                  // 밀린 행이 없거나 일시 실패가 있었음 → 다음 회차(1초 뒤)에 다시
            }
        }
    }
}
```

publishBatch가 다른 빈에 있는 이유: @Transactional은 Spring 프록시가 적용하므로, 같은 클래스 안에서 this로 부르면 트랜잭션이 걸리지 않음.

다음은 한 배치를 보내는 본체. 한 트랜잭션 안에서 행을 잠그고, 보내고, 결과를 기다려, 성공한 행에 발행 표시를 함.

```java
@Component
public class OutboxPublisher {

    private static final Logger log = LoggerFactory.getLogger(OutboxPublisher.class);
    private static final String TOPIC = "order.events.v1";
    private static final long RELAY_LOCK_KEY = 7_001L;                 // 이 relay 전용 advisory lock 번호
    private static final Duration SEND_WAIT = Duration.ofSeconds(35); // delivery.timeout.ms(30초)보다 조금 길게

    private final JdbcClient jdbc;
    private final KafkaTemplate<String, String> kafka;

    public OutboxPublisher(JdbcClient jdbc, KafkaTemplate<String, String> kafka) {
        this.jdbc = jdbc;
        this.kafka = kafka;
    }

    /** 한 배치를 보냄. relay가 이어서 다음 배치를 보내도 되면 true (배치가 꽉 찼고 일시 실패가 없었음) */
    @Transactional
    public boolean publishBatch(int limit) {
        // 1. 인스턴스가 여러 개여도 relay는 하나만 돌게 함. 트랜잭션이 끝나면 자동으로 풀림 (6번)
        boolean leader = jdbc.sql("SELECT pg_try_advisory_xact_lock(:key)")
                .param("key", RELAY_LOCK_KEY)
                .query(Boolean.class).single();
        if (!leader) {
            return false;
        }

        // 2. 아직 안 보낸 행을 순서대로 가져와 잠금
        List<OutboxRow> rows = jdbc.sql("""
                SELECT id, event_type, event_key, payload::text AS payload
                FROM outbox_event
                WHERE published_at IS NULL AND quarantined_at IS NULL
                ORDER BY seq
                LIMIT :limit
                FOR UPDATE SKIP LOCKED
                """)
            .param("limit", limit)
            .query((rs, n) -> new OutboxRow(
                    rs.getObject("id", UUID.class),
                    rs.getString("event_type"),
                    rs.getString("event_key"),
                    rs.getString("payload")))
            .list();

        // 3. 행마다 보냄. 결과는 아직 기다리지 않음 → 여러 행이 batch로 묶여 나감
        Map<OutboxRow, CompletableFuture<SendResult<String, String>>> inFlight = new LinkedHashMap<>();
        Set<String> failedKeys = new HashSet<>();
        List<Failure> failures = new ArrayList<>();
        for (OutboxRow row : rows) {
            if (failedKeys.contains(row.eventKey())) {
                continue;                                  // 앞 행이 실패한 key는 순서를 지키려고 이번 회차에서 건너뜀
            }
            try {
                inFlight.put(row, kafka.send(toRecord(row)));
            } catch (RuntimeException e) {                 // send()가 바로 실패하는 경우 (메타데이터 대기 초과, 너무 큰 레코드 등)
                failedKeys.add(row.eventKey());
                failures.add(new Failure(row.id(), e));
                if (!isPermanent(rootCause(e))) {
                    break;                                 // 일시 실패(broker에 닿지 않음 등)면 남은 행도 행마다 max.block.ms씩 멈추므로 이번 배치는 여기까지
                }
            }
        }

        // 4. 결과 모으기
        List<UUID> published = new ArrayList<>();
        for (var entry : inFlight.entrySet()) {
            try {
                entry.getValue().get(SEND_WAIT.toMillis(), TimeUnit.MILLISECONDS);
                published.add(entry.getKey().id());
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();        // 종료 신호를 위로 전달 (relay 반복문이 확인함)
                break;                                     // 발행 실패가 아니므로 attempts를 올리지 않고 멈춤. 결과를 모르는 행은 미발행으로 남아 다음 회차에 다시 나감
            } catch (ExecutionException | TimeoutException e) {
                failures.add(new Failure(entry.getKey().id(), e));
            }
        }

        // 5. 성공한 행만 발행 표시. 실패한 행은 기록만 하고 예외는 던지지 않음
        if (!published.isEmpty()) {
            jdbc.sql("UPDATE outbox_event SET published_at = now() WHERE id IN (:ids)")
                .param("ids", published)
                .update();
        }
        boolean transientFailure = false;
        for (Failure failure : failures) {
            boolean permanent = recordFailure(failure);    // 5번 섹션
            transientFailure = transientFailure || !permanent;
        }
        // 일시 실패가 있었다면 broker 장애일 수 있으므로 이번 회차는 여기서 멈춤
        return rows.size() == limit && !transientFailure;
    }

    private ProducerRecord<String, String> toRecord(OutboxRow row) {
        ProducerRecord<String, String> record = new ProducerRecord<>(TOPIC, row.eventKey(), row.payload());
        record.headers().add("event-id", row.id().toString().getBytes(StandardCharsets.UTF_8));
        record.headers().add("event-type", row.eventType().getBytes(StandardCharsets.UTF_8));
        return record;
    }

    record OutboxRow(UUID id, String eventType, String eventKey, String payload) {
    }

    record Failure(UUID id, Throwable error) {
    }
}
```

코드에서 봐야 할 곳

- 3단계에서 send()를 모두 먼저 부르고 4단계에서 결과를 기다림. 행마다 send().get()을 하면 매번 왕복 한 번을 기다리느라 batch가 만들어지지 않음.
- 결과를 기다리는 시간(SEND_WAIT)은 `delivery.timeout.ms`보다 길게 잡음. 더 짧으면 Kafka에는 결국 들어갔는데 relay는 실패로 보고 다음 회차에 또 보냄(중복).
- 이벤트 id와 타입은 헤더로 보냄. consumer가 payload를 파싱하기 전에 중복 확인과 관심 없는 이벤트 거르기를 할 수 있음.
- publishBatch는 '이어서 보내도 되는가'를 돌려줌. 배치가 덜 찼으면 밀린 행이 없고, 일시 실패가 있었으면 broker 장애일 수 있으므로 false. relay는 그 회차를 끝내고 1초 뒤에 다시 시도하므로, 장애 중에 같은 행을 연달아 두드리지 않음.

**신경 쓸 점**

- 행 하나가 실패했다고 예외를 던지면 트랜잭션 전체가 롤백됨. 이미 Kafka에 나간 행의 발행 표시까지 취소되어 다음 회차에 다시 나감(중복). 실패는 기록만 하고 정상 리턴함.
- send()는 두 가지 방식으로 실패함: 바로 예외를 던지거나(이때 `max.block.ms`까지 멈출 수 있음), 나중에 Future가 실패로 끝남. 둘 다 처리해야 함.
- Future의 whenComplete 같은 후속 작업은 producer의 I/O 스레드에서 돎. 여기서 JDBC를 쓰면 그 producer를 공유하는 모든 전송이 멈춤. 결과는 relay 스레드에서 get()으로 받음.
- kafkaTemplate.flush()는 공유 producer 전체를 비우므로, 다른 곳에서 보낸 메시지까지 함께 기다림. 위 코드처럼 Future를 기다리는 것으로 충분함.
- 실패 원인은 한 겹 감싸여 옴. 비동기 실패는 KafkaProducerException, 즉시 실패는 spring-kafka의 KafkaException. getCause()로 원래 예외를 꺼내 판단함 (5번).
- 트랜잭션이 결과를 기다리는 동안(최대 SEND_WAIT) 행 잠금과 DB 커넥션을 잡고 있음. 커넥션 풀 크기와 종료 시간(6번)에 이 시간을 반영함.

## 5. 발행 실패 처리 계단

발행 실패는 세 단계로 처리됨.

1. kafka-clients가 `delivery.timeout.ms` 안에서 알아서 재시도함. 대부분의 일시 장애(leader 변경, 잠깐의 네트워크 문제)는 여기서 끝남.
2. 그래도 실패하면 행이 미발행으로 남고, relay는 그 회차를 멈춘 뒤 다음 회차에 다시 보냄. 일시 실패(broker 장애, 메타데이터 대기 초과, 권한 설정 문제 등)는 몇 번이든 이 단계에서 기다림. 장애가 길어지면 아래 '가장 오래된 미발행 행 나이' 알림으로 사람이 알아챔.
3. 그 행 자체가 문제라 다시 해도 성공할 수 없는 실패(영구 실패)는 바로 격리함. 격리된 행은 relay가 더 보지 않고, 사람이 원인을 보고 처리함.

실패를 기록하는 코드. OutboxPublisher 안에 둠.

```java
/** 실패를 기록함. 영구 실패면 격리하고 true를 돌려줌 */
private boolean recordFailure(Failure failure) {
    Throwable cause = rootCause(failure.error());
    boolean permanent = isPermanent(cause);
    jdbc.sql("""
            UPDATE outbox_event
            SET attempts = attempts + 1,                              -- 기록용. 격리 기준으로 쓰지 않음
                last_error = :error,
                quarantined_at = CASE WHEN :permanent THEN now() END   -- 영구 실패만 격리. 일시 실패는 다음 회차에 다시 시도
            WHERE id = :id
            """)
        .param("id", failure.id())
        .param("error", cause.getClass().getSimpleName() + ": " + cause.getMessage())
        .param("permanent", permanent)
        .update();
    log.warn("outbox 발행 실패 id={} permanent={}", failure.id(), permanent, cause);
    return permanent;
}

/** 그 행 자체가 문제라 다시 보내도 성공할 수 없는 실패인가.
    권한이나 topic 설정 문제는 모든 행에 해당하므로 격리하지 않고 기다림 (고치면 순서대로 이어서 나감) */
static boolean isPermanent(Throwable cause) {
    return cause instanceof RecordTooLargeException      // 메시지가 너무 큼
        || cause instanceof SerializationException;       // 직렬화 불가
}

/** spring-kafka가 감싼 예외를 벗겨 kafka-clients의 원래 예외를 꺼냄 */
static Throwable rootCause(Throwable error) {
    Throwable t = (error instanceof ExecutionException && error.getCause() != null) ? error.getCause() : error;
    while (t instanceof org.springframework.kafka.KafkaException && t.getCause() != null) {
        t = t.getCause();                                  // KafkaProducerException도 이 타입의 하위 클래스
    }
    return t;
}
```

relay가 잘 돌고 있는지는 두 숫자로 봄. 메트릭으로 내보내 알림을 걸어 둠.

```sql
-- 가장 오래된 미발행 행의 나이(초). 계속 커지면 relay가 멈췄거나 broker 장애·설정 문제로 막힌 것 (일시 실패는 격리되지 않으므로 이 알림이 잡음)
SELECT COALESCE(EXTRACT(EPOCH FROM now() - min(created_at)), 0) AS oldest_pending_seconds
FROM outbox_event
WHERE published_at IS NULL AND quarantined_at IS NULL;

-- 격리된 행 수. 0이 아니면 사람이 봐야 함
SELECT count(*) FROM outbox_event WHERE quarantined_at IS NOT NULL;
```

발행된 행은 일정 기간 뒤에 지움. 재발행이나 조사에 쓸 기간만큼 남김.

```java
/** 매시 정각. 한 번에 너무 많이 지우지 않게 나눠서 지움 */
@Scheduled(cron = "0 0 * * * *")
public void purgePublished() {
    int deleted;
    do {
        deleted = jdbc.sql("""
                DELETE FROM outbox_event
                WHERE id IN (SELECT id FROM outbox_event
                             WHERE published_at < now() - interval '7 days'
                             LIMIT 5000)
                """).update();
    } while (deleted == 5000);
}
```

**신경 쓸 점**

- 격리 기준이 없으면 너무 큰 메시지 같은 poison 행이 매 회차 영원히 재시도됨.
- 시도 횟수로 격리하면 안 됨. broker 장애가 길어지면 모든 행의 시도 횟수가 함께 올라 멀쩡한 행까지 poison으로 격리됨. 장애와 poison은 횟수로 구분되지 않으므로, 격리는 그 행 자체의 문제로 판별된 실패만 하고 장애는 알림으로 잡음.
- 행을 격리하면 그 key의 뒤 이벤트는 다음 회차에 나가므로 순서가 한 번 깨짐. 순서가 엄격해야 한다면 격리 대신 그 key 전체를 멈추는 방식을 택함. 어느 쪽이든 consumer가 버전으로 오래된 이벤트를 무시하게 해 두면 피해가 줄어듦.
- topic이 없거나 broker에 닿지 않으면 send()가 행마다 `max.block.ms`씩 멈춤. 그래서 즉시 실패가 일시 실패면 그 배치의 남은 행은 보내지 않고, 이 값도 짧게 둠.
- 정리 작업이 relay와 같은 스케줄러 스레드를 쓰면, 오래 걸리는 DELETE가 relay를 막음 (6번).

## 6. 여러 인스턴스·순서·종료

앱 인스턴스가 여러 개면 relay도 여러 개가 동시에 돎. FOR UPDATE SKIP LOCKED만 쓰면 각자 다른 행을 나눠 가져가 중복 발행은 없음. 하지만 같은 key의 앞 행은 인스턴스 A가, 뒤 행은 인스턴스 B가 가져가 B가 먼저 보내면 순서가 뒤집힘.

그래서 순서가 중요하면 relay를 하나만 돌게 함. 위 코드의 pg_try_advisory_xact_lock이 그 역할. 잠금을 얻은 인스턴스만 보내고, 나머지는 바로 빠짐. 처리량이 모자라면 key의 해시로 행을 나눠(인스턴스마다 담당 key 범위) 각자 돌리는 방법도 있음.

종료할 때는 진행 중인 relay 한 번이 끝날 시간을 줘야 함. 중간에 끊기면 트랜잭션이 롤백되어 이미 나간 행이 다음에 다시 나감(중복).

```yaml
spring:
  lifecycle:
    timeout-per-shutdown-phase: 45s # 종료 단계마다 기다리는 최대 시간 (기본 30s). SEND_WAIT(35s)보다 길게
  task:
    scheduling:
      shutdown:
        await-termination: true # 진행 중인 @Scheduled 실행이 끝날 때까지 기다림
        await-termination-period: 45s
```

종료 순서: 스케줄러가 새 실행을 멈추고 진행 중인 배치를 기다림 → 그다음 ProducerFactory가 닫히며 producer.close()가 남은 batch를 보내고 연결을 닫음. 컨테이너 환경이라면 종료 유예 시간(예: Kubernetes terminationGracePeriodSeconds)도 이보다 길게 둠.

**신경 쓸 점**

- @Scheduled 작업들은 기본적으로 스케줄러 스레드 하나를 같이 씀. 정리 작업이 길어지면 relay가 그만큼 늦어짐. 정리 작업을 나눠 지우거나, 스케줄러 동시 실행 수를 늘림.
- relay 트랜잭션은 결과를 기다리는 동안 잠금과 커넥션을 잡고 있음. `delivery.timeout.ms`와 SEND_WAIT를 줄이면 종료도 빨라지고 커넥션 점유도 짧아짐.
- advisory lock 번호는 서비스마다 겹치지 않게 정함. 같은 DB를 쓰는 다른 기능이 같은 번호를 쓰면 서로를 막음.

## 7. 비교: kafka-clients로 직접 짜면

spring-kafka 없이 쓰면 producer를 직접 만들고, 공유하고, 닫아야 함.

```java
@Configuration
class RawKafkaProducerConfig {

    /** 앱 종료 때 close(): 남은 batch를 보내고 연결을 닫음 */
    @Bean(destroyMethod = "close")
    KafkaProducer<String, String> kafkaProducer() {
        Properties props = new Properties();
        props.put(ProducerConfig.BOOTSTRAP_SERVERS_CONFIG, "kafka-1:9092,kafka-2:9092,kafka-3:9092");
        props.put(ProducerConfig.KEY_SERIALIZER_CLASS_CONFIG, StringSerializer.class);
        props.put(ProducerConfig.VALUE_SERIALIZER_CLASS_CONFIG, StringSerializer.class);
        props.put(ProducerConfig.ACKS_CONFIG, "all");
        props.put(ProducerConfig.ENABLE_IDEMPOTENCE_CONFIG, true);
        return new KafkaProducer<>(props);   // 스레드 안전. 하나를 만들어 모든 스레드가 공유
    }
}

// 보내기: Future를 돌려받고, callback은 producer의 I/O 스레드에서 실행됨
Future<RecordMetadata> future = producer.send(record, (metadata, exception) -> {
    if (exception != null) {
        // 실패 기록. 무거운 일은 하지 않음
    }
});
```

spring-kafka가 대신 해 주는 것

- 설정을 `spring.kafka.*`로 모으고, producer를 처음 쓸 때 하나 만들어 공유하고, 앱이 끝날 때 닫음.
- 결과를 CompletableFuture로 돌려줌. 즉시 실패는 예외로, 비동기 실패는 KafkaProducerException으로 모양이 정리됨.
- 실패를 기본으로 로그에 남김(LoggingProducerListener).
- 테스트 지원(spring-kafka-test, Testcontainers 연결).

## 8. 테스트

실제 Kafka와 PostgreSQL을 컨테이너로 띄워 끝까지 확인함. @ServiceConnection이 컨테이너 주소를 `spring.kafka.bootstrap-servers`와 datasource 설정에 넣어 줌.

```java
@SpringBootTest(properties = "spring.kafka.consumer.auto-offset-reset=earliest")
@Testcontainers
class OutboxRelayIT {

    @Container
    @ServiceConnection
    static KafkaContainer kafka = new KafkaContainer("apache/kafka:4.2.1");   // 운영 broker와 같은 버전으로

    @Container
    @ServiceConnection
    static PostgreSQLContainer postgres = new PostgreSQLContainer("postgres:17");

    @Autowired
    OrderService orderService;

    @Autowired
    ConsumerFactory<String, String> consumerFactory;

    @Test
    void placedOrderIsPublishedThroughOutbox() {
        Order order = orderService.placeOrder(new PlaceOrderCommand(UUID.randomUUID(), 10_000));

        // relay는 @Scheduled로 테스트 안에서도 돎. topic에서 한 건을 읽어 확인
        try (Consumer<String, String> consumer = consumerFactory.createConsumer("outbox-it", "it")) {
            consumer.subscribe(List.of("order.events.v1"));
            ConsumerRecord<String, String> record =
                    KafkaTestUtils.getSingleRecord(consumer, "order.events.v1", Duration.ofSeconds(10));

            assertThat(record.key()).isEqualTo(order.id().toString());
            assertThat(record.headers().lastHeader("event-id")).isNotNull();
        }
    }
}
```

더 확인하면 좋은 경우: `max.request.size`보다 큰 payload를 넣으면 행이 격리되는지, Kafka 컨테이너를 멈췄다 다시 띄우면 행이 격리되지 않고 기다렸다가 순서대로 나가는지, 인스턴스 두 개(relay 두 번 호출)에서도 같은 행이 한 번만 나가는지.
