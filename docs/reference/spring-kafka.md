# Spring Kafka의 책임과 자동 설정

Java 클라이언트를 감싸는 빈·listener container·commit·실패 처리의 역할을 설명한다.

관련 문서: [Java 클라이언트](java-client.md), [Outbox 구현](outbox.md), [Inbox 구현](inbox.md).

- **무엇인가**
  - Spring for Apache Kafka. kafka-clients(Java client)를 감싸서 Spring 방식(빈, 설정, 애노테이션)으로 쓰게 해 주는 라이브러리. Spring 쪽 프로젝트라 Apache Kafka 릴리스에는 들어 있지 않음.
  - 네트워크, 프로토콜, batch, 재시도 같은 Kafka 동작은 전부 그 아래의 Java client가 함. spring-kafka가 맡는 건 그 위의 '실행 틀': producer 객체 관리, consumer poll 루프, commit 시점, 실패 처리.
  - Spring Boot가 `spring.kafka.*` 속성으로 자동 설정함. 버전도 Spring Boot가 맞춰 줌.
- **Java client와의 대응**
  - spring-kafka를 배운다는 건 'Java client에서 내가 짜던 부분을 누가 어떤 기본값으로 대신하나'를 아는 것.
  | Java client                               | spring-kafka                                                                        |
  | ----------------------------------------- | ----------------------------------------------------------------------------------- |
  | KafkaProducer                             | ProducerFactory가 만들고 KafkaTemplate이 감쌈                                       |
  | send() → Future                           | KafkaTemplate.send() → CompletableFuture<SendResult>                                |
  | KafkaConsumer                             | ConsumerFactory가 만들고 listener container가 소유                                  |
  | 앱이 짜는 poll 루프, app 스레드           | listener container, container 스레드                                                |
  | commitSync() 시점                         | AckMode                                                                             |
  | onPartitionsRevoked, onPartitionsAssigned | container에 등록하는 rebalance listener                                             |
  | 재시도, DLQ 직접 구현                     | DefaultErrorHandler + BackOff + DeadLetterPublishingRecoverer, 또는 @RetryableTopic |
  | deserializer 예외 처리                    | ErrorHandlingDeserializer                                                           |
  | pause(), resume()                         | container의 pause, resume                                                           |
  | Admin으로 topic 생성                      | KafkaAdmin + NewTopic 빈                                                            |
- **Producer: KafkaTemplate**
  - ProducerFactory(DefaultKafkaProducerFactory)
    - 첫 사용 때 KafkaProducer를 하나 만들고, 모든 스레드가 그 하나를 공유함. KafkaProducer는 스레드 안전하기 때문 ([Java 클라이언트](java-client.md)).
      - 앱이 뜰 때가 아니라 첫 send() 때 만들어지므로, [발행 내부 흐름](producer-flow.md)의 A1부터 A3까지가 첫 send() 안에서 일어남.
    - 스레드마다 따로 쓰려면 producerPerThread 옵션.
    - transaction을 켜면(`spring.kafka.producer.transaction-id-prefix`) 공유 대신 transactional producer 여러 개를 캐시해 두고 빌려 씀.
  - KafkaTemplate.send()
    - CompletableFuture<SendResult>를 리턴함. SendResult에는 보낸 레코드와 결과(partition, offset)가 들어 있음.
    - 이 Future는 Java client의 callback 안에서 완료됨 → 대개 sender thread(kafka-producer-network-thread)에서 완료됨.
      - 그래서 whenComplete() 같은 후속 작업을 executor 없이 붙이면 sender thread에서 돎. 여기서 DB 작업처럼 오래 걸리는 일을 하면 이 producer의 모든 전송이 멈춤 (Java client의 callback 주의와 같음).
      - 예외: send() 안에서 바로 실패하면 호출한 스레드에서 완료되고, 이미 완료된 Future에 후속 작업을 붙이면 붙인 스레드에서 돎.
    - 실패는 기본적으로 로그만 남김(LoggingProducerListener). 앱이 성공과 실패를 알려면 Future 결과를 확인해야 함.
  - transaction
    - transaction-id-prefix를 설정하면 Spring Boot가 KafkaTransactionManager도 만들어 줌.
    - 이건 Kafka 쓰기들끼리의 원자성([Producer](kafka-clients.md) transaction)이지, DB 트랜잭션과 Kafka 전송을 하나로 묶어 주지 않음. 둘을 함께 쓰면 DB가 먼저 커밋되고 Kafka가 나중에 커밋되며, Kafka 쪽이 실패하면 재전달되므로 DB 반영은 멱등이어야 함 (spring-kafka 문서).
    - 그래서 DB 변경과 이벤트 발행을 묶으려면 여전히 Outbox가 필요함.
  - 우리 인프라에서는 content-api의 outbox relay가 KafkaTemplate을 써도, KafkaProducer를 직접 써도 Kafka 쪽 동작은 같음. 차이는 설정 방식(`spring.kafka.producer.*`)과 테스트 도구 정도.
- **Consumer: listener container**
  - 구조
    - @KafkaListener 메서드를 쓰면 container factory(ConcurrentKafkaListenerContainerFactory)가 container를 만듦.
    - `concurrency`를 N으로 두면 안에 자식 container N개가 생기고, 각자 KafkaConsumer 하나와 스레드 하나를 가짐. N이 partition 수보다 크면 남는 consumer는 놀게 됨.
    - listener 메서드는 그 consumer 스레드(container 스레드)에서 실행됨. [Java 클라이언트](java-client.md)의 'app 스레드'가 바로 이 스레드.
    - 스레드 이름은 '리스너 id-번호-C-n' 형태. 로그에서 어느 consumer인지 구분할 때 씀.
    - 우리 인프라에서는 content-api가 virtual thread를 켜 두었으므로(`spring.threads.virtual.enabled: true`), Spring Boot가 container 스레드도 가상 스레드로 만듦(이름 'kafka-n').
  - 루프: 앞 회차 처리분 commit → poll() → listener 호출(레코드마다, batch listener면 묶음으로) → 다시 처음으로.
  - commit
    - `enable.auto.commit`을 직접 설정하지 않으면 container가 false로 바꾸고 직접 commit함. 그래서 기본이 '처리 후 commit'(at-least-once).
    - 언제 commit하나는 AckMode로 정함. 기본 BATCH: poll 한 번에 받은 레코드를 전부 처리한 뒤, 다음 poll 직전에 commit.
      - RECORD: 레코드마다. TIME, COUNT, COUNT_TIME: 시간이나 개수 기준. MANUAL, MANUAL_IMMEDIATE: listener가 받은 Acknowledgment로 직접.
    - commit은 기본적으로 동기(commitSync).
  - 처리 실패 (에러 핸들러)
    - transaction manager를 쓰지 않으면 기본 DefaultErrorHandler가 처리함. recoverer를 지정하지 않았을 때의 건너뛰기와, 재시도·DLQ 설정은 [Inbox 오류 처리](inbox.md#4-에러-처리-재시도와-dlq)에서 설명함.
    - 긴 대기는 [처리 시간 예산과 container pause](inbox.md#5-처리-시간-예산)를 따름.
  - DLT (Dead Letter Topic)
    - DeadLetterPublishingRecoverer를 에러 핸들러에 달면 '로그만 남기고 건너뜀' 대신 DLT로 보냄.
    - 기본 목적지: 원래 topic 이름 + '-dlt', 같은 partition 번호. (3.3부터. 그 전 버전은 '.DLT')
    - 원래 topic, partition, offset, 예외 정보가 헤더(kafka_dlt-로 시작)로 붙어서, 나중에 원인을 보고 재투입할 수 있음.
  - 역직렬화 실패 (poison pill)
    - deserializer가 예외를 던지면 poll()에서 같은 레코드를 계속 다시 만나 앞으로 나아가지 못함.
    - ErrorHandlingDeserializer로 진짜 deserializer를 감싸면, 실패를 헤더에 담아 넘기고 에러 핸들러가 받아 재시도 없이 DLT로 보냄.
  - non-blocking 재시도 (@RetryableTopic)
    - 실패한 메시지를 재시도 topic('-retry')으로 옮겨 두고 원래 partition은 계속 진행함. 기본 3번 시도 후 DLT.
    - 대신 **같은 key의 순서 보장이 깨짐.** 순서가 중요한 흐름(revision 순서가 있는 이벤트 등)에는 맞지 않음.
  - JSON: spring-kafka 4.0부터 JsonSerializer, JsonDeserializer는 폐기 예정. Jackson 3 기반 JacksonJsonSerializer, JacksonJsonDeserializer를 씀.
- **KIP-848 (`group.protocol=consumer`)**
  - spring-kafka 4.0부터 정식 지원. `spring.kafka.consumer.properties.group.protocol=consumer`로 켬. 기본값은 여전히 classic.
  - 달라지는 점
    - 배정이 조금씩 바뀌므로 onPartitionsAssigned가 작은 partition 묶음으로 여러 번 불릴 수 있음.
    - 클라이언트 쪽 assignor 설정은 무시됨. 배정은 coordinator가 계산함 ([Consumer Group](kafka-clients.md)).
- **Spring Boot 자동 설정**
  - `spring.kafka.*`만 채우면 KafkaTemplate, ProducerFactory, ConsumerFactory, container factory, KafkaAdmin 빈이 만들어짐.
  - transaction-id-prefix를 설정하면 KafkaTransactionManager가, retry topic 설정을 켜면 관련 빈이 추가됨.
  - KafkaAdmin은 NewTopic 빈을 보고 앱 시작 때 topic을 만들어 줌.
    - 우리 인프라에서는 topic을 Strimzi KafkaTopic CR로 관리하므로 NewTopic 빈은 쓰지 않는 편이 맞음. topic 정의가 두 곳으로 갈라지기 때문.
- **기술 스택으로 가져가려면 알아야 할 것** (우선순위 순)
  1. container가 대신 정하는 세 가지: commit 시점(AckMode), 실패 처리(DefaultErrorHandler 기본값), 스레드(concurrency, container 스레드). 전달 보장이 여기서 정해짐.
  2. DLT와 ErrorHandlingDeserializer 설정. 기본값 그대로면 실패 메시지가 사라지거나 partition이 멈춤.
  3. KafkaTemplate Future의 완료 스레드. 후속 작업을 sender thread에서 돌리지 않기.
  4. transaction의 범위. Kafka끼리만 원자적이고, DB와는 Outbox로 묶음.
  5. 버전 관계와 `spring.kafka.*` 속성 이름. Java client 설정이 어떤 속성으로 매핑되는지.
  - 나머지(메시지 변환, 헤더 매핑, 테스트용 embedded broker 등)는 필요할 때 찾아봐도 됨.
