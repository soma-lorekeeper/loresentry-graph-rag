# Spring Kafka Inbox 구현 예시

주문 이벤트로 배송을 준비하는 Java·PostgreSQL 예시다. Python·Neptune을 사용하는 GraphRAG의 구현 자체를 뜻하지 않는다.

관련 문서: [소비 내부 흐름](consumer-flow.md), [Outbox 구현](outbox.md), [메시지 계약 초안](message-contract.md).

### 참고사항

- 이 섹션이 답하는 질문: 이벤트를 받아 비즈니스 로직에 넘기기까지 코드를 어떻게 짜고, 무엇을 신경 써야 하나.
- 예시 도메인: 배송 서비스가 `order.events.v1`의 OrderPlaced를 받아 배송 준비 행을 만듦. [Outbox 예시](outbox.md)의 주문 서비스가 보낸 이벤트(key = 주문 id, 헤더 event-id·event-type, value = JSON)를 받는다고 가정. 이름과 값은 모두 예시.
- 전제: Spring Boot 4.1 + spring-kafka 4.1(kafka-clients 4.2), Java 21, PostgreSQL, JdbcClient, Jackson 3.
- Inbox가 기본: 메시지는 최소 한 번(at-least-once) 오므로 같은 이벤트가 두 번 올 수 있음. 처리한 이벤트 id를 Inbox 테이블에 남겨, 도메인 반영과 같은 트랜잭션에서 중복을 거름. 전체 흐름: listener container가 poll → 레코드마다 listener 메서드 호출 → 트랜잭션 안에서 Inbox 기록과 도메인 반영 → 정상 리턴한 묶음의 offset을 container가 commit.
- 라이브러리와 broker 안에서 일어나는 일은 [소비 내부 흐름](consumer-flow.md) 참조.

## 1. 의존성·설정

producer와 같은 starter를 씀. Kafka starter에는 Jackson이 들어 있지 않으므로, 웹 starter가 없는 서비스라면 Jackson starter를 따로 넣음.

의존성 선언은 [Outbox의 의존성·설정](outbox.md#1-의존성설정topic)을 함께 사용한다.

consumer 설정은 `spring.kafka.consumer.*`, 루프를 도는 container 설정은 `spring.kafka.listener.*`에 적음.

```yaml
spring:
  kafka:
    bootstrap-servers: kafka-1:9092,kafka-2:9092,kafka-3:9092
    consumer:
      group-id: shipping-service # 서비스(용도)마다 고정. 바꾸면 읽던 위치를 잃음
      auto-offset-reset: earliest # commit 기록이 없을 때(첫 배포) 처음부터 읽음
      key-deserializer: org.apache.kafka.common.serialization.StringDeserializer
      value-deserializer: org.apache.kafka.common.serialization.StringDeserializer
      max-poll-records: 50 # poll 한 번에 받는 최대 개수 (5번)
      max-poll-interval: 5m # poll 사이 간격의 상한 (5번)
      properties:
        group.protocol: consumer # 새 group 프로토콜(KIP-848). 쓰지 않으면 이 줄을 지움 (기본 classic)
    listener:
      ack-mode: batch # 기본값. poll 한 묶음을 다 처리한 뒤 commit
      concurrency: 3 # consumer 스레드 수. topic partition 수 이하로
      immediate-stop: false # 종료 때 받아 둔 묶음까지 처리하고 멈춤 (6번)
```

`enable-auto-commit`은 적지 않음. 적지 않으면 container가 auto commit을 끄고, 처리가 끝난 뒤에 직접 commit함.

**신경 쓸 점**

- group-id는 한 번 정하면 바꾸지 않음. 바꾸면 새 group이 되어 commit 기록이 없으므로 `auto-offset-reset`에 따라 처음부터 다시 읽거나(earliest) 그 사이 메시지를 건너뜀(latest).
- 첫 배포 때 `auto-offset-reset`이 latest면 배포 전에 쌓인 이벤트를 읽지 않음. 놓치면 안 되는 이벤트라면 earliest.
- `enable-auto-commit: true`로 두면 처리가 끝나기 전에 commit될 수 있어 유실 위험이 생김.
- `concurrency`가 partition 수보다 크면 남는 스레드는 partition을 못 받고 놈.
  - 전체 consumer 수 = pod replicas 수 \* `concurrency`
  - 정확히는 전체 consumer 수 ≤ partition 수 이어야 함.
  - 그래서 partition 수는 늘어날 것을 감안한 최대 멤버 수로 결정해야 함.
  - pod 수가 Kafka를 제외한 이유로 늘어나야 할 때는 `concurrency`를 낮춰 곱이 partition을 넘지 않도록 함.
  - 권장되는 partition 수는 약수가 많은 값(6, 12, 24)를 추천함. consumer에게 골고루 배정하는 것이 쉽기에.
- `group.protocol: consumer`를 쓰면 `session.timeout.ms`, `heartbeat.interval.ms`, `partition.assignment.strategy`를 설정하는 순간 시작 시 예외가 남. 이 값들은 broker가 정함.
- consumer를 오래 내려 두면 commit 기록이 만료될 수 있음(broker의 `offsets.retention.minutes`, 기본 7일). 다시 띄우면 `auto-offset-reset`이 적용됨.

## 2. Inbox 테이블

Inbox는 "이미 처리한 이벤트 id"를 남기는 테이블. 도메인 테이블과 같은 DB에 둬야 같은 트랜잭션으로 묶임.

```sql
CREATE TABLE inbox_event (
    consumer_name VARCHAR(100) NOT NULL,          -- 어느 처리기가 처리했나
    event_id      UUID         NOT NULL,          -- 헤더 event-id
    processed_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (consumer_name, event_id)
);
```

consumer_name을 키에 넣는 이유: 같은 이벤트를 한 서비스 안의 여러 처리기가 각자 처리한다면, 한쪽이 기록한 id 때문에 다른 쪽이 건너뛰면 안 됨.

Inbox 행도 계속 쌓이므로 주기적으로 지움. 보관 기간은 같은 이벤트가 다시 올 수 있는 기간보다 길어야 함.

```sql
-- topic 보존 기간(예: 7일)보다 넉넉하게 남기고 지움
DELETE FROM inbox_event WHERE processed_at < now() - interval '14 days';
```

**신경 쓸 점**

- Inbox를 topic 보존 기간보다 먼저 지우면, 그 뒤에 재처리(offset을 되돌려 다시 읽기)할 때 이미 처리한 이벤트를 또 반영함.
- DLQ에서 원래 topic으로 되돌려 보내는 경우도 '다시 오는' 경우임. DLQ 보존 기간과 재투입 절차도 보관 기간 계산에 넣음.

## 3. Listener와 처리 서비스

listener 메서드는 얇게 둠: 헤더에서 이벤트 id를 꺼내고, JSON을 DTO로 바꾸고, 처리 서비스에 넘기기만 함. DB 작업은 @Transactional이 붙은 서비스가 함.

```java
/** consumer 쪽 DTO. producer의 클래스를 공유하지 않고 JSON 계약만 맞춤 */
public record OrderPlaced(UUID orderId, UUID customerId, long totalAmount, int version, Instant occurredAt) {
}

/** 다시 시도해도 성공할 수 없는 메시지. 에러 핸들러가 재시도 없이 DLQ로 보냄 (4번) */
public class NonRetryableEventException extends RuntimeException {
    public NonRetryableEventException(String message) {
        super(message);
    }
}

@Component
public class OrderEventListener {

    private final ShipmentService shipmentService;
    private final JsonMapper json;

    public OrderEventListener(ShipmentService shipmentService, JsonMapper json) {
        this.shipmentService = shipmentService;
        this.json = json;
    }

    @KafkaListener(id = "shipping-order-events", groupId = "shipping-service", topics = "order.events.v1")
    public void onMessage(ConsumerRecord<String, String> record) {
        String eventType = header(record, "event-type");
        if (!"OrderPlaced".equals(eventType)) {
            return;                                    // 관심 없는 이벤트는 그냥 넘김 (정상 리턴 → commit됨)
        }

        String eventId = header(record, "event-id");
        if (eventId == null) {
            throw new NonRetryableEventException("event-id 헤더 없음: "
                    + record.topic() + "-" + record.partition() + "@" + record.offset());
        }

        // 트랜잭션 밖에서 파싱. 형식이 틀리면 JacksonException → 재시도 없이 DLQ (4번)
        OrderPlaced event = json.readValue(record.value(), OrderPlaced.class);
        shipmentService.handle(UUID.fromString(eventId), event);
    }

    private static String header(ConsumerRecord<?, ?> record, String name) {
        Header header = record.headers().lastHeader(name);
        return header == null ? null : new String(header.value(), StandardCharsets.UTF_8);
    }
}
```

코드에서 봐야 할 곳

- @KafkaListener에 id와 groupId를 둘 다 적음. groupId 없이 id만 적으면 id가 `group.id`로 쓰여, 이름을 바꾸는 순간 읽던 위치를 잃음.
- 헤더는 ConsumerRecord에서 직접 꺼내 null을 확인함. @Header 파라미터로 받으면 헤더가 없을 때 재시도 대상 예외가 나서 같은 메시지를 계속 재시도함.
- 이 메서드는 container 스레드에서 실행됨. 리턴하면 container가 다음 레코드로 넘어가고, 묶음이 끝나면 commit함.

다음은 실제 처리. Inbox INSERT를 먼저 해서 중복인지 판정하고, 새 이벤트일 때만 도메인에 반영함.

```java
@Service
public class ShipmentService {

    private static final String CONSUMER_NAME = "shipping.order-placed";

    private final JdbcClient jdbc;

    public ShipmentService(JdbcClient jdbc) {
        this.jdbc = jdbc;
    }

    @Transactional
    public void handle(UUID eventId, OrderPlaced event) {
        // 1. Inbox에 먼저 기록 시도. 이미 있으면 0행 → 처리한 적 있는 중복
        int inserted = jdbc.sql("""
                INSERT INTO inbox_event (consumer_name, event_id)
                VALUES (:consumer, :eventId)
                ON CONFLICT DO NOTHING
                """)
            .param("consumer", CONSUMER_NAME)
            .param("eventId", eventId)
            .update();
        if (inserted == 0) {
            return;                                    // 중복: 예외 없이 정상 리턴해야 offset이 넘어감
        }

        // 2. 도메인 반영. Inbox 기록과 같은 트랜잭션이라 함께 확정되거나 함께 취소됨
        jdbc.sql("""
                INSERT INTO shipment (order_id, customer_id, status, order_version)
                VALUES (:orderId, :customerId, 'READY', :version)
                ON CONFLICT (order_id) DO UPDATE
                    SET customer_id = EXCLUDED.customer_id,
                        order_version = EXCLUDED.order_version
                    WHERE shipment.order_version < EXCLUDED.order_version   -- 더 오래된 버전은 무시
                """)
            .param("orderId", event.orderId())
            .param("customerId", event.customerId())
            .param("version", event.version())
            .update();
    }
}
```

SELECT로 먼저 확인하지 않고 INSERT부터 하는 이유: 같은 레코드가 동시에 두 번 처리될 수 있음(예: rebalance 뒤에도 쫓겨난 옛 consumer가 처리를 이어 가는 경우). 둘 다 SELECT에서 '없음'을 보면 둘 다 반영함. INSERT를 먼저 하면 두 번째 트랜잭션은 기본 키에서 첫 번째가 끝나길 기다렸다가 충돌(0행)로 끝나므로 한 번만 반영됨.

도메인 쓰기의 버전 비교(order_version)는 Inbox가 못 막는 경우를 위한 것. 서로 다른 이벤트가 순서가 바뀌어 오면 둘 다 새 이벤트라 Inbox를 통과함. 버전을 비교하면 오래된 이벤트가 새 상태를 덮어쓰지 않음.

**신경 쓸 점**

- 중복이면 예외를 던지지 않고 정상 리턴함. 예외를 던지면 에러 핸들러가 같은 메시지를 계속 재시도함.
- 반대로 진짜 실패는 반드시 예외로 던짐. 예외를 삼키고 리턴하면 성공으로 보고 offset이 commit되어 그 메시지는 다시 오지 않음.
- listener 안에서 처리를 다른 스레드나 비동기 작업으로 넘기지 않음. listener가 리턴하는 순간 처리된 것으로 보고 commit하므로, 넘긴 작업이 실패해도 메시지는 사라짐. 같은 key의 순서도 깨짐.
- Inbox는 같은 DB의 쓰기만 보호함. 이메일 발송, 외부 결제 API 호출 같은 외부 부수 효과는 Inbox로 못 막음. 외부 호출에는 event-id를 멱등 키로 넘기거나, outbox로 한 번 더 넘겨서 처리함.

## 4. 에러 처리: 재시도와 DLQ

listener가 예외를 던지면 container의 에러 핸들러가 받음. 아무것도 설정하지 않으면 기본 동작은 이럼: 간격 없이 10번 시도하고, 그래도 실패하면 로그만 남기고 그 메시지를 건너뜀(유실). 그래서 DLQ로 보내는 에러 핸들러를 직접 등록함.

```java
@Configuration
class KafkaErrorHandlingConfig {

    /** CommonErrorHandler 빈은 하나만 둠. 두 개면 Spring Boot가 아무것도 적용하지 않음 */
    @Bean
    DefaultErrorHandler kafkaErrorHandler(KafkaTemplate<String, String> template) {
        // 재시도를 다 쓴 레코드를 '원래 topic + .dlq'로 보냄.
        // partition을 -1로 두면 producer가 고름 → DLQ의 partition 수가 원래 topic과 달라도 안전
        var recoverer = new DeadLetterPublishingRecoverer(template,
                (record, ex) -> new TopicPartition(record.topic() + ".dlq", -1));

        // 1초 → 2초 → 4초 간격으로 3번 재시도 (처음 시도까지 총 4번)
        var backOff = new ExponentialBackOffWithMaxRetries(3);
        backOff.setInitialInterval(1_000);
        backOff.setMultiplier(2.0);
        backOff.setMaxInterval(10_000);

        var handler = new DefaultErrorHandler(recoverer, backOff);
        // 다시 해도 소용없는 실패는 재시도 없이 바로 DLQ로
        handler.addNotRetryableExceptions(JacksonException.class, NonRetryableEventException.class);
        return handler;
    }

    /** DLQ topic. 원래 topic보다 길게 보존해 원인을 보고 다시 넣을 시간을 줌 */
    @Bean
    NewTopic orderEventsDlq() {
        return TopicBuilder.name("order.events.v1.dlq")
                .partitions(3)
                .replicas(3)
                .config(TopicConfig.RETENTION_MS_CONFIG, String.valueOf(Duration.ofDays(30).toMillis()))
                .build();
    }
}
```

이 빈만 등록하면 Spring Boot가 listener container에 자동으로 붙여 줌. 실패한 메시지는 이렇게 처리됨.

1. 같은 묶음에서 앞서 성공한 레코드들의 offset을 먼저 commit함.
2. 실패한 레코드 위치로 되돌아가(seek) 다음 poll에서 다시 받아 재시도함. 트랜잭션이 롤백됐으므로 Inbox 기록도 없어서 처음 처리처럼 동작함.
3. 재시도를 다 쓰면 DLQ로 보냄. DLQ 메시지에는 원래 topic·partition·offset과 예외 정보가 kafka_dlt- 로 시작하는 헤더로 붙음.
4. DLQ로 보내는 데 성공하면 그 레코드는 처리된 것으로 보고 넘어감.

DLQ에 쌓인 메시지를 원인 수정 후 다시 넣는 절차는 [미작성 운영 항목의 DLQ 재투입 절차](README.md#미결정과-미작성-항목)에서 다룸.

**신경 쓸 점**

- CommonErrorHandler 빈이 두 개 이상이면 Spring Boot는 어느 것도 적용하지 않고 기본 동작(10번 후 건너뜀)으로 돌아감. 경고도 없음.
- DLQ로 보내는 것도 Kafka 발행이라 `spring.kafka.producer.*` 설정을 씀. DLQ topic이 없거나 broker에 닿지 않으면 DLQ 발행이 실패하고, 그 레코드를 다시 재시도하느라 그 partition 전체가 멈춤.
- 재시도 횟수는 메모리에만 있음. 재시도 중에 앱이 재시작되거나 rebalance가 일어나면 처음부터 다시 셈.
- 재시도 동안 그 partition의 뒤 메시지는 모두 기다림. 재시도 간격을 길게 잡을수록 지연이 커짐 (5번).
- 실패한 메시지를 별도 retry topic으로 옮기는 방식(@RetryableTopic)은 뒤 메시지를 막지 않지만, 같은 key의 순서가 깨짐. 순서가 중요한 이벤트에는 쓰지 않음.

## 5. 처리 시간 예산

consumer에서 가장 흔한 장애는 "처리가 느려서 group에서 쫓겨나는 것". poll 사이 간격이 `max.poll.interval.ms`를 넘으면 consumer가 group을 떠나고, partition이 다른 consumer에게 넘어가며, 처리 중이던 묶음은 commit되지 못해 다시 처리됨. 그래서 두 가지를 계산해 둠.

- poll 한 번에 받는 개수 × 레코드 하나의 최악 처리 시간 < `max.poll.interval.ms`. 예: `max-poll-records` 50 × 3초 = 150초 < 5분.
- 재시도 간격 한 번 + 그 레코드 처리 시간 < `max.poll.interval.ms`. 재시도 사이에도 container가 poll을 하므로 간격의 합계가 아니라 한 번의 간격이 기준.

처리 시간이 길거나 재시도 간격을 길게 잡아야 한다면, 기다리는 동안 스레드를 재우지 않고 container를 멈춰 두는 방식으로 바꿈.

```java
/** 4번의 kafkaErrorHandler 빈을 이것으로 바꿈 (빈은 하나만).
    재시도 간격 동안 container를 pause해 두고, 시간이 되면 resume함 */
@Bean
DefaultErrorHandler kafkaErrorHandler(KafkaTemplate<String, String> template,
                                      KafkaListenerEndpointRegistry registry,
                                      TaskScheduler taskScheduler) {   // @EnableScheduling이 있으면 Spring Boot가 만들어 줌
    var recoverer = new DeadLetterPublishingRecoverer(template,
            (record, ex) -> new TopicPartition(record.topic() + ".dlq", -1));
    var backOff = new ExponentialBackOffWithMaxRetries(5);
    backOff.setInitialInterval(30_000);
    backOff.setMaxInterval(600_000);

    var pauseService = new ListenerContainerPauseService(registry, taskScheduler);
    return new DefaultErrorHandler(recoverer, backOff, new ContainerPausingBackOffHandler(pauseService));
}
```

처리 자체를 비동기로 돌려야 하는 경우(외부 API가 오래 걸리는 등)에는 `spring.kafka.listener.ack-mode: manual`과 `async-acks: true`로 레코드마다 처리가 끝났을 때 Acknowledgment.acknowledge()를 부르는 방식이 있음. container가 순서가 뒤섞인 ack를 모아 앞 offset이 모두 끝났을 때만 commit해 줌. 대신 같은 partition 안의 처리 순서는 보장되지 않고 코드가 복잡해지므로, 먼저 `max-poll-records`를 줄이는 것으로 해결되는지 봄.

**신경 쓸 점**

- 외부 호출에는 반드시 타임아웃을 걺. 타임아웃 없는 호출 하나가 멈추면 `max.poll.interval.ms`를 넘겨 group에서 쫓겨나고, 넘겨받은 consumer도 같은 메시지에서 또 멈춤.
- `max-poll-records`를 줄이면 commit이 잦아지고 처리량이 조금 줄지만, 쫓겨나는 위험과 재처리 범위가 줄어듦.
- batch listener는 한 번에 받는 단위가 batch이기에, 중간에 멈출 지점이 없음. `immediate-stop=true` 옵션은 listener 단위가 record 일 때만 효과가 있음.

## 6. rebalance와 종료

rebalance(partition 재배정)와 종료 때 해야 할 일 대부분은 container가 해 줌.

- partition을 빼앗길 때: 그때까지 처리한 레코드의 offset을 먼저 commit하고 넘겨줌. 새 담당자는 그 다음부터 읽음.
- 앱을 종료할 때: 받아 둔 묶음까지 처리하고(`immediate-stop: false`), 남은 offset을 commit한 뒤 group을 떠남.

partition을 받거나 빼앗길 때 앱이 따로 할 일이 있을 때만 rebalance listener를 등록함. 빈으로 등록할 때는 ConsumerAwareRebalanceListener 타입이어야 Spring Boot가 붙여 줌(일반 ConsumerRebalanceListener 빈은 무시됨).

```java
@Bean
ConsumerAwareRebalanceListener rebalanceListener() {
    return new ConsumerAwareRebalanceListener() {

        @Override
        public void onPartitionsRevokedAfterCommit(Consumer<?, ?> consumer, Collection<TopicPartition> partitions) {
            // container가 대기 중인 offset을 commit한 뒤에 불림. partition별 로컬 캐시 정리 같은 일을 여기서
        }

        @Override
        public void onPartitionsAssigned(Consumer<?, ?> consumer, Collection<TopicPartition> partitions) {
            // 새로 받은 partition. 필요하면 로컬 상태를 준비
        }
    };
}
```

종료 시간: container가 멈추는 시간은 `spring.lifecycle.timeout-per-shutdown-phase`(기본 30초) 안에 끝나야 함. 묶음 하나의 최악 처리 시간이 이보다 길면 늘리거나, `immediate-stop: true`로 현재 레코드까지만 처리하고 멈추게 함(나머지는 다음 담당자가 다시 받음. Inbox가 중복을 거름).

**신경 쓸 점**

- 처리 도중 강제 종료되면(시간 초과, kill) 그 묶음은 commit되지 않아 다시 옴. Inbox가 있으면 안전함.
- 컨테이너 환경이라면 종료 유예 시간(예: Kubernetes terminationGracePeriodSeconds)이 `timeout-per-shutdown-phase`보다 길어야 함.

## 7. 비교: kafka-clients로 직접 짜면

spring-kafka 없이 쓰면 스레드, poll 루프, commit, 종료를 모두 직접 짜야 함.

```java
public class RawOrderConsumer implements Runnable {

    private final KafkaConsumer<String, String> consumer;   // 한 번에 한 스레드만 써야 함
    private final AtomicBoolean running = new AtomicBoolean(true);

    public RawOrderConsumer(KafkaConsumer<String, String> consumer) {
        this.consumer = consumer;
    }

    @Override
    public void run() {                                      // 전용 스레드에서 실행
        try {
            consumer.subscribe(List.of("order.events.v1"));
            while (running.get()) {
                ConsumerRecords<String, String> records = consumer.poll(Duration.ofSeconds(1));
                for (ConsumerRecord<String, String> record : records) {
                    process(record);                         // 실패하면? 재시도, DLQ, seek을 직접 구현해야 함
                }
                if (!records.isEmpty()) {
                    consumer.commitSync();                   // 처리 후 commit (설정에서 auto commit을 꺼 둬야 함)
                }
            }
        } catch (WakeupException e) {
            if (running.get()) {
                throw e;                                     // 종료 요청이 아닌데 깨어났다면 진짜 에러
            }
        } finally {
            consumer.close();
        }
    }

    /** 다른 스레드(종료 훅)에서 부름. poll()에서 기다리던 스레드를 깨움 */
    public void shutdown() {
        running.set(false);
        consumer.wakeup();
    }
}
```

spring-kafka가 대신 해 주는 것

- consumer 생성, 전용 스레드, poll 루프, 앱 종료 시 wakeup과 close.
- auto commit을 끄고, 처리가 끝난 묶음만 commit(AckMode).
- 실패 시 seek, 재시도 간격, DLQ 발행(DefaultErrorHandler).
- partition을 빼앗기기 전 대기 offset commit.
- 레코드를 메서드 인자로 바꾸기(@KafkaListener), 여러 consumer 스레드(`concurrency`).

## 8. 테스트

컨테이너로 Kafka와 PostgreSQL을 띄우고, 같은 이벤트를 두 번 보내도 배송이 한 건만 생기는지 확인함.

```java
@SpringBootTest
@Testcontainers
class OrderEventListenerIT {

    @Container
    @ServiceConnection
    static KafkaContainer kafka = new KafkaContainer("apache/kafka:4.2.1");

    @Container
    @ServiceConnection
    static PostgreSQLContainer postgres = new PostgreSQLContainer("postgres:17");

    @Autowired
    KafkaTemplate<String, String> kafkaTemplate;

    @Autowired
    JdbcClient jdbc;

    @Test
    void sameEventTwiceCreatesOneShipment() throws Exception {
        UUID eventId = UUID.randomUUID();
        UUID orderId = UUID.randomUUID();
        String payload = """
                {"orderId":"%s","customerId":"%s","totalAmount":10000,"version":1,"occurredAt":"2026-01-01T00:00:00Z"}
                """.formatted(orderId, UUID.randomUUID());

        for (int i = 0; i < 2; i++) {                        // 같은 이벤트를 두 번 보냄
            ProducerRecord<String, String> record = new ProducerRecord<>("order.events.v1", orderId.toString(), payload);
            record.headers().add("event-id", eventId.toString().getBytes(StandardCharsets.UTF_8));
            record.headers().add("event-type", "OrderPlaced".getBytes(StandardCharsets.UTF_8));
            kafkaTemplate.send(record).get();
        }

        // 배송이 한 건 생길 때까지 기다림. 두 번째 메시지까지 처리된 뒤를 확실히 보려면,
        // 다른 이벤트를 하나 더 보내고 그 처리가 끝나기를 기다린 다음 확인함
        await().atMost(Duration.ofSeconds(10)).untilAsserted(() ->
                assertThat(jdbc.sql("SELECT count(*) FROM shipment WHERE order_id = :id")
                        .param("id", orderId).query(Long.class).single()).isEqualTo(1L));
    }
}
```

더 확인하면 좋은 경우: 형식이 틀린 JSON을 보내면 재시도 없이 `order.events.v1.dlq`에 들어가는지, 처리 중 예외가 나면 설정한 횟수만큼 재시도한 뒤 DLQ로 가는지.
