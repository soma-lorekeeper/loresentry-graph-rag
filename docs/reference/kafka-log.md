# Kafka Topic·Partition·Offset

로그의 주소 체계, 보존·압축 정리와 시간 기반 조회를 설명한다.

관련 문서: [복제와 ISR](kafka-replication.md), [메시지 계약 초안](message-contract.md).

- format
  ```yaml
  topic: content.file.changed.v1
  -> partition 0: [m0][m1][m2][m3]... <- offset 0, 1, 2, 3
  -> partition 1: [m0][m1] ...
  -> ...
  ```
- **Topic**
  - 메시지를 분류하는 이름. DB의 테이블 같은 것
- **Partition**
  - topic을 쪼갠 append-only 로그.
  - 내부에 메시지가 뒤에서부터 추가되서 쌓이는 형태.
  - 여러 broker와 consumer가 나눠 처리할 수 있는 병렬 처리 단위.
  - 순서는 파티션 안에서만 보장
  - partition 수는 늘릴 수만 있고 줄일 수 없음.
  - partition 수 = 한 consumer group의 최대 병렬도. 처음에 여유 있게 잡는 게 보통.
- **Message**
  - partition의 한 segment에 들어가는 메시지 record.
  - offset, key, value, headers, timestamp로 이루어짐.
- **Key**
  - 같은 key는 항상 같은 파티션으로 감.
    - 단, partition 수가 그대로일 때만. partition을 늘리면 `hash(key) % partition 수`가 바뀌어 같은 key가 다른 partition으로 감.
  - key가 없으면 여러 partition에 흩어져 순서 보장 없음.
  - projectId를 key로 쓰면 프로젝트 단위 순서를 보장할 수 있음.
- **Offset**
  - 파티션 안에서 메시지의 번호. partition마다 0부터 따로 셈 (topic 전체 번호가 아님).
  - 한 번 쓰인 번호는 재사용되지 않음. retention으로 앞 메시지가 지워져도 번호는 계속 증가.
  - 번호가 연속적이라는 보장은 없음. 중간에 구멍이 생길 수 있음.
    - transaction marker(control record)가 로그에서 offset을 한 칸 차지함. 클라이언트에는 안 보이는데 번호는 씀.
    - compaction이 중간 레코드를 지우면 그 자리가 비어 있음.
    - '다음 offset = 현재 + 1'을 전제로 짠 consumer 로직은 깨짐.
  - consumer는 어디까지 읽었는지를 offset으로 기록함.
    - 정확히는 '다음에 읽을 offset'(마지막 처리 offset + 1)을 commit함.
  - partition 로그의 주요 위치
    - log start offset: 아직 남아 있는 가장 오래된 메시지
    - high watermark: 이 offset 미만까지가 확정된 메시지. consumer는 여기까지만 읽음
    - log end offset: 다음 메시지가 쓰일 자리
- **Retention / cleanup.policy**
  - `delete`: 보존 기간(`retention.ms`)이나 크기(`retention.bytes`)를 넘은 segment를 통째로 삭제. 우리 topic이 이 방식.
  - `compact`: key별로 마지막 메시지만 남기고 이전 값은 정리. '현재 상태' 스냅샷 용도 (예: `__consumer_offsets`).
  - 삭제는 segment 단위라 정확히 보존 기간이 되는 순간 지워지지는 않음. 쓰기 중인 활성 segment는 닫힌 뒤에야 삭제 대상이 됨.
  - compaction의 동작
    - key별 최신 값만 남으므로 로그가 아니라 테이블처럼 동작함.
    - 삭제는 value가 `null`인 메시지(**tombstone**)로 표현. '이 key는 이제 없다'는 표식.
    - tombstone은 바로 지워지지 않고 `delete.retention.ms`(기본 24시간) 동안 남음. consumer가 삭제 사실을 읽을 기회를 줘야 하기 때문.
    - cleaner는 '지저분한 비율'이 임계치를 넘을 때 동작함. 중복이 즉시 사라지지 않으므로, compacted topic을 읽는 consumer는 같은 key의 옛 값을 볼 수 있음.
- **시간으로 찾기**
  - `.timeindex` 덕분에 `offsetsForTimes(시각)`으로 '그 시각 이후 첫 메시지의 offset'을 얻을 수 있음. '2시간 전부터 재처리'가 가능한 이유.
  - timestamp의 의미는 `message.timestamp.type`으로 갈림.
    - `CreateTime`(기본): producer가 찍은 시각. 클라이언트 시계에 의존하고 순서가 뒤섞일 수 있음.
    - `LogAppendTime`: broker가 받은 시각. 단조 증가가 보장됨.
  - 시간 기준 retention도 이 값을 씀. 기본이 `CreateTime`이라 클라이언트 시계가 틀리면 retention이 이상하게 동작함.
