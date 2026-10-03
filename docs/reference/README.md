# Kafka 참고 자료

Kafka 개념, 클라이언트 내부 흐름, Lore Sentry 메시지 계약 초안과 Outbox·Inbox 구현 예시를 주제별로 모았다.

## 읽을 문서

| 필요한 내용 | 문서 | 담당 범위 |
|---|---|---|
| 서버·저장·메타데이터 | [Broker·Cluster·KRaft](kafka-cluster.md) | 디스크·캐시·purgatory·클러스터·controller |
| 로그와 보존 | [Topic·Partition·Offset](kafka-log.md) | key·offset·retention·compaction·시간 조회 |
| 복제와 내구성 | [Leader·ISR](kafka-replication.md) | 복제·확정·리더 전환·Raft와 ISR의 차이 |
| 발행·구독 기본 동작 | [Producer·Consumer·Group](kafka-clients.md) | batching·commit·그룹 배정·rebalance·lag |
| 통신과 대기 | [네트워크와 I/O](network.md) | 소켓·이벤트 루프·요청 큐·연결 재사용 |
| Java 클라이언트 내부 | [kafka-clients](java-client.md) | 스레드·공유 버퍼·이벤트 큐·Future |
| Spring이 맡는 일 | [Spring Kafka](spring-kafka.md) | 빈·container·AckMode·오류 처리·자동 설정 |
| 발행의 상세 순서 | [발행 내부 흐름](producer-flow.md) | 초기 연결부터 broker 복제·ack·발행 표시까지 |
| 소비의 상세 순서 | [소비 내부 흐름](consumer-flow.md) | 그룹 합류부터 fetch·처리·offset commit까지 |
| 서비스 간 이벤트 규약 | [메시지 계약 초안](message-contract.md) | 이벤트·topic·헤더·호환성·크기·Claim Check·DLQ |
| DB 변경과 발행 연결 | [Outbox 구현](outbox.md) | SQL·relay·실패 격리·순서·종료·테스트 예시 |
| 중복 소비와 DB 반영 | [Inbox 구현](inbox.md) | SQL·listener·재시도·DLQ·시간 예산·종료·테스트 예시 |

## 범위와 문서 책임

원본 `reference.md`의 학습 내용과 설계 메모를 재배치했다. 인프라 수치·기본값·버전 설명은 원문의 기록이며 이번 정리에서 운영 환경이나 라이브러리를 새로 검증한 결과가 아니다.
원문의 설계 표기는 `v1-26.09.26`이다. 이 자료와 [GraphRAG 현재 구현 문서](../README.md)는 구분한다.

개념 문서는 동작 원리, 내부 흐름 문서는 계층별 실행 순서, 구현 문서는 코드와 실패 처리 방법을 담당한다.
실제 서비스의 이벤트 이름·헤더·topic·보존 설정은 메시지 계약 초안에 모으고, 다른 문서는 이를 참조한다.

Outbox·Inbox의 주문·배송 예시는 Java·PostgreSQL 학습용이다. 예시의 `event-id`·`event-type`, `order.events.v1`, `.dlq`, `NewTopic` 설정은 Lore Sentry 초안의 `ce_*` 헤더·소비자별 DLQ·Strimzi 관리 설정과 같지 않다.
Spring 소비 예시를 Python·Neptune GraphRAG에 그대로 적용하는 구현 계획으로 읽지 않는다.
개념 부분의 `content.file.changed.v1`·7일 보존 등의 인프라 기록과 메시지 계약 초안의 교체안도 구분한다.

## 미결정과 미작성 항목

원문에서 비어 있던 제목은 아래에 모았다. 설명이나 결정을 새로 채우지는 않았다.

| 구분 | 남은 내용 |
|---|---|
| 메시지 계약 미결정 | FileChanged의 순서 값(`version`, `outbox_id`)과 `revision_no`의 관계, 관계 저장 구조. [해당 메모](message-contract.md#event) 참고 |
| 흐름 보완 | 구간별 실패 지점과 책임 주체, 전달 보장 비교표 |
| 네트워크·보안 설계 | 접속 주소와 리스너, TLS, 인증·인가, NetworkPolicy, 외부 접근 |
| 긴 처리 | Python 클라이언트 선택과 LLM 작업의 pause·worker·commit·resume 전략 |
| 운영 | lag 모니터링(Kafka Exporter 메모만 있음), DLQ 재투입 절차, replay 절차 |
| 생태계 | Kafka Connect, Cruise Control, Kafka Streams, MirrorMaker 2, Kafka Bridge |
| Strimzi CR | operator와 reconcile, Kafka, KafkaNodePool, KafkaTopic, KafkaUser, 그 밖의 CR |
