# Kafka Broker·Cluster·KRaft

브로커의 저장·요청 처리와 클러스터 메타데이터 관리를 설명한다.

관련 문서: [복제와 ISR](kafka-replication.md), [네트워크](network.md).

## Broker, Cluster

- **Broker**
  - 메시지를 받아 디스크에 read/write 하는 kafka 서버 프로세스(JVM).
    - 우리 인프라에서는 1 pod = 1 broker
  - 하는 일은 세 가지.
    1. producer의 쓰기 요청을 받아 파티션 로그 끝에 append
    2. follower replica의 복제 요청(fetch)에 응답
    3. consumer의 읽기 요청(fetch)에 응답
  - **broker id**
    - 클러스터 안에서 broker를 구분하는 고유 번호. 메타데이터의 복제본 위치, leader 표기가 전부 이 id로 되어 있음.
    - 우리 인프라에서는 `lore-sentry-broker-0/1/2` → id 0, 1, 2
  - **보관 단위는 topic이 아니라 partition replica**
    - 한 broker가 여러 topic의 partition replica를 섞어서 가짐.
    - 그중 일부는 leader, 일부는 follower 역할.
    - 우리 인프라에서는 replication factor 3 = broker 3대라서 모든 broker가 모든 partition을 가짐.
  - **디스크 저장 구조**
    - partition마다 디렉터리(`content.file.changed.v1-3/`)가 있고, 그 안에 로그를 일정 크기로 자른 **segment** 파일이 쌓임.
      - `.log`: 메시지 본문
      - `.index`, `.timeindex`: offset, 시간으로 위치를 찾는 색인
    - `retention.ms`가 지난 segment는 파일째 삭제.
    - 우리 인프라에서는 broker당 gp3 10Gi PVC 하나를 모든 topic이 나눠 씀.
    - 한 partition replica는 한 디스크에만 저장됨. 여러 디스크로 나누어 저장되지 않음.
  - **Tiered Storage** (KIP-405)
    - 오래된 segment를 S3 같은 객체 스토리지로 내리고, 로컬 디스크에는 최근 데이터만 두는 기능. Kafka 4.x에서 정식 기능.
    - 의미: '저장 용량'과 '브로커 대수'를 분리함. 지금까지는 오래 보관하려면 디스크를 키우거나 broker를 늘려야 했음.
    - 쓰려면 객체 스토리지 연동 플러그인(RemoteStorageManager)이 따로 필요함. Kafka 본체에는 인터페이스만 있음.
    - 우리 인프라의 broker당 10Gi 제약과 직접 연결되는 선택지. Claim Check와 같이 놓고 볼 것.
  - **빠른 이유**
    - 쓰기는 파일 끝에 붙이기만 하는 순차 I/O라 디스크에서도 빠름.
    - 읽기와 쓰기를 JVM heap이 아니라 OS page cache에 맡김.
      - **page cache**: 리눅스 커널이 파일 내용을 RAM에 올려두는 영역(4KB page 단위). `O_DIRECT` 같은 옵션을 쓰지 않는 한 모든 파일 I/O가 자동으로 여기를 거침.
        - 커널은 남는 메모리를 캐시로 쓰다가, 애플리케이션이 메모리를 요구하면 캐시를 버리고 내줌.
        - 'page cache에 맡긴다' = Kafka가 자체 캐시를 만들지 않고 그냥 파일에 쓰고 파일에서 읽는다는 뜻. 캐싱은 커널이 대신 해줌.
      - **쓰기**: `write()` 시스템 콜은 데이터를 page cache에 복사하고 dirty page로 표시한 뒤 바로 리턴함. 디스크 기록을 기다리지 않음.
        - 실제 디스크 기록은 커널의 writeback 스레드가 나중에 모아서 함. 기본 커널 설정이면 대략 30초 안에 내려감.
        - 그래서 쓰기가 메모리 속도로 끝나고, 디스크에는 큰 덩어리가 순차로 기록됨.
        - page cache는 커널 소유라, JVM이 OOM으로 죽거나 컨테이너가 kill돼도 이미 `write()`한 데이터는 커널이 디스크에 내려씀. 잃는 건 node 자체가 죽거나 전원이 나갈 때뿐 (아래 '내구성' 참고).
      - **읽기**: consumer와 follower는 대부분 방금 쓰인 끝부분(tail)을 읽음. 그 데이터는 아직 page cache에 있어서 디스크를 건드리지 않고 RAM에서 바로 나감.
        - 많이 뒤처진 consumer는 이미 캐시에서 밀려난 오래된 데이터를 읽으므로 디스크에서 읽음.
        - offset 0부터 replay하듯 오래된 데이터를 대량으로 읽으면, 그 데이터가 page cache를 채우면서 tail이 밀려날 수 있음. 한 consumer의 replay가 다른 consumer까지 느리게 만드는 경로.
      - **heap에 캐시하지 않는 이유**
        - 이중 저장: 파일에 쓰는 순간 커널이 어차피 page cache에 담음. heap에도 두면 같은 데이터가 RAM에 두 번 올라가고, Java 객체 오버헤드까지 붙음.
        - GC: heap에 수 GB 데이터가 드나들면 GC pause가 길어짐. page cache는 GC 대상이 아님.
        - 재시작: JVM 프로세스만 다시 뜨면 page cache는 커널에 그대로 남아 있음. heap 캐시는 0부터 다시 채워야 함. (pod를 새로 만들며 볼륨이 다시 마운트되면 page cache도 비워질 수 있음)
    - 우리 인프라에서는 heap 1Gi, pod 메모리 limit 3Gi → 나머지 메모리가 page cache로 쓰임. heap을 크게 잡는다고 빨라지지 않음.
      - 3Gi 분배: heap 1Gi + JVM non-heap 수백 MB(metaspace, thread stack, direct buffer 등) + 나머지 page cache(대략 1.5Gi 안팎).
      - page cache도 pod 메모리(cgroup)로 계산됨. 그래서 limit이 곧 page cache 상한선. limit에 닿으면 커널이 page cache부터 버리므로 OOMKill 원인이 되지는 않음.
      - `kubectl top`의 broker 메모리가 3Gi 근처로 보여도 page cache가 포함된 수치라 정상. 경보는 JVM heap 사용량 기준으로 잡을 것.
    - **zero-copy**: consumer가 읽을 때 `sendfile` 시스템 콜로 page cache에서 네트워크 소켓으로 바로 보냄. JVM heap을 거치지 않음.
      - 일반 방식: `page cache → JVM 버퍼 → socket buffer → NIC`. 데이터가 커널 → 유저 공간 → 커널로 두 번 복사되고, 시스템 콜 전환(context switch) 비용도 생김.
      - zero-copy: `page cache → socket buffer → NIC`. 데이터가 커널 밖으로 나오지 않음.
      - 가능한 이유: 디스크에 저장한 포맷이 곧 네트워크로 보내는 포맷이라 broker가 메시지를 풀거나 고칠 필요가 없음. [Producer](kafka-clients.md)의 end-to-end 압축과 같은 맥락.
      - page cache hit과 zero-copy는 별개의 이득. page cache hit은 '디스크를 안 읽는 것', zero-copy는 '유저 공간으로 복사하지 않는 것'.
      - TLS를 켜면 암호화 때문에 데이터가 유저 공간을 통과해야 해서 zero-copy가 깨짐. TLS 비용이 암복호화 CPU만이 아닌 이유.
        - 경로가 `page cache → JVM 버퍼로 읽기 → SSLEngine으로 암호화 → socket buffer → NIC`로 바뀜.
        - 늘어나는 비용: 암호화 CPU + 커널↔유저 공간 복사 + 암호화용 버퍼 메모리.
        - page cache hit은 그대로임. 디스크 읽기가 늘어나는 게 아니라 메모리 안의 복사와 CPU가 늘어나는 것.
        - 우리 인프라에서는 9092 `plain` 리스너(클라이언트)는 zero-copy가 동작함. 9091 복제 리스너는 Strimzi가 TLS로 구성하므로, follower 복제 트래픽은 이미 zero-copy 없이 동작 중.
        - 클라이언트 리스너에 TLS를 켜면 consumer 읽기 트래픽도 같은 경로를 타게 됨. broker CPU limit이 1코어라 도입 전 여유부터 확인할 것.
  - **내구성은 디스크 flush가 아니라 복제로 확보**
    - Kafka는 기본적으로 fsync를 하지 않음. `log.flush.interval.messages` 기본값이 사실상 무한이라 flush 시점을 OS에 맡김.
    - 그래서 `acks=all`로 성공 응답을 받아도 메시지가 아직 page cache에만 있고 디스크에 안 내려갔을 수 있음.
    - '디스크에 썼으니 안전하다'가 아니라 '서로 다른 broker 3대의 메모리에 있으니 안전하다'는 설계.
    - 뒤집으면, 전원 장애로 replica가 동시에 죽으면 확정된 메시지도 잃을 수 있음. AZ 분산이 중요한 진짜 이유.
    - 우리 인프라에서는 broker 2대가 `2b`에 몰려 있어 이 위험이 큼.
  - **purgatory** (지연 요청 대기실)
    - broker가 바로 답할 수 없는 요청을 보류해 두는 내부 자료구조. 조건이 채워지거나 timeout이 되면 그때 응답함.
      - 이름은 '연옥'(천국도 지옥도 아닌, 판정을 기다리는 곳)에서 옴. 요청이 성공도 실패도 아닌 상태로 기다린다는 뜻.
    - 필요한 이유: 요청 처리 스레드(`num.io.threads`, 기본 8개)가 적음.
      - 스레드가 요청을 붙잡고 데이터를 기다리면, consumer 8개만 대기해도 스레드가 전부 묶여 다른 요청을 하나도 처리하지 못함.
      - 그래서 스레드는 요청을 purgatory에 넣어 두고 바로 다음 요청을 처리하러 감.
    - 무엇을 기다리나
      - Fetch(consumer, follower): 데이터가 `fetch.min.bytes` 이상 쌓이면 완료. `fetch.max.wait.ms`(기본 500ms)가 먼저 지나면 있는 만큼 보냄(없으면 빈 응답). [Consumer](kafka-clients.md)의 long polling이 이것.
      - Produce(`acks=all`): ISR 전원이 그 offset까지 복제해 high watermark가 넘어가면 완료. producer의 `request.timeout.ms`(기본 30초)가 먼저 지나면 `REQUEST_TIMED_OUT` 에러.
      - 그 밖에 DeleteRecords, Tiered Storage 원격 읽기 등도 같은 구조로 대기함.
    - 동작
      1. 요청이 오면 먼저 바로 끝낼 수 있는지 확인. 되면 purgatory를 거치지 않고 즉시 응답.
      2. 안 되면 purgatory에 넣음. 이때 어느 partition의 변화를 기다리는지(watch key)와 timeout을 같이 등록.
      3. 그 partition에 새 메시지가 붙거나 high watermark가 올라가면, 그 변화를 일으킨 스레드가 대기 요청을 다시 확인하고 조건이 맞으면 응답을 내보냄.
      4. timeout이 먼저 오면 그 시점 상태로 응답. 조건 충족과 timeout 중 먼저 일어난 쪽이 딱 한 번만 처리함.
    - `acks=all` 한 건이 purgatory 두 개를 오가는 흐름
      1. follower들의 Fetch가 leader의 Fetch purgatory에서 대기 중 (새 데이터 없음).
      2. Produce 도착 → leader가 append. Produce 요청은 Produce purgatory로 들어가고, 이 append가 Fetch purgatory를 깨워 follower들에게 새 데이터를 즉시 응답함.
      3. follower가 받아 쓰고 다음 Fetch를 보냄. '다음 offset부터 달라'는 요청이 곧 '여기까지 받았다'는 신호.
      4. leader가 high watermark를 올림 → Produce purgatory를 깨움 → producer에 성공 응답.
      - 루프를 돌며 확인하는 곳이 하나도 없는데 각 단계가 이벤트 즉시 이어짐. 그래서 `acks=all` 지연은 대략 follower fetch 한 번 왕복 수준.
      - 우리 인프라에서는 outbox relay가 `acks=all`(기본값)로 보내면 이 흐름을 탐.
    - 모니터링: `kafka.server:type=DelayedOperationPurgatory,name=PurgatorySize,delayedOperation=Produce|Fetch`
      - Fetch 쪽은 평소에도 큼. 한가한 consumer, follower가 전부 여기서 대기 중이라 정상.
      - Produce 쪽이 쌓이면 follower 복제가 느리다는 신호.
      - 우리 인프라에서는 metrics 미설정이라 아직 볼 수 없음.
    - 대기 요청이 수만 개 단위로 쌓일 수 있어서 timeout은 **Hierarchical Timing Wheel**(시계처럼 도는 원형 배열에 만료 시각별로 요청을 꽂아 두는 구조)로 관리함. 등록·삭제가 O(1).
  - **역할 모드** (`process.roles`)
    - `broker`: 메시지만 처리
    - `controller`: 메타데이터만 관리
    - `broker,controller`: 한 JVM이 둘 다 (combined mode, 우리 인프라)
  - **리스너** (Strimzi 기준 포트)
    - 9092: 클라이언트용 (우리 인프라의 `plain` 리스너, TLS 없음)
    - 9091: broker 간 복제용 (Strimzi가 내부 TLS로 자동 구성)
    - 9090: controller 통신용
  - **fenced**
    - broker는 active controller에 주기적으로 heartbeat를 보냄.
    - 끊기면 controller가 해당 broker를 fenced(격리) 상태로 표시하고, 그 broker가 leader였던 partition의 leader를 ISR 안의 다른 replica로 옮김.
- **Cluster**
  - broker 여러 대가 같은 메타데이터를 공유하며 한 몸처럼 동작하는 묶음. 하나의 cluster id를 가짐.
    - 우리 인프라에서는 3 brokers = 1 cluster (`lore-sentry`)
  - **클라이언트 접속 흐름 (bootstrap)**
    1. 클라이언트는 bootstrap 주소로 아무 broker에 처음 접속
    2. 그 broker가 로컬에 캐싱한 메타데이터(partition별 leader 위치)를 알려줌
    3. 이후에는 각 partition의 leader broker에 **직접** 연결해서 쓰고 읽음
    - bootstrap은 첫 연결용 입구일 뿐, 모든 트래픽이 여기를 지나는 게 아님.
    - 우리 인프라에서는
      - `lore-sentry-kafka-bootstrap:9092`: 입구 역할의 Service
      - `lore-sentry-kafka-brokers`: 각 broker를 직접 찾는 headless Service
  - **클라이언트는 낡은 메타데이터를 들고 있을 수 있음**
    - 클라이언트는 'partition 3의 leader는 broker 1' 같은 정보를 캐싱해 둠. leader가 바뀌면 낡은 정보로 요청을 보내게 됨.
    - 이때 broker가 `NOT_LEADER_OR_FOLLOWER` 에러를 돌려주고, 클라이언트는 메타데이터를 새로 받아 자동 재시도함.
    - 로그에 이 에러가 가끔 보이는 건 정상. 장애가 아니라 leader 전환의 흔적임. 계속 반복되면 그때가 문제.
  - **설정의 세 층위**
    - cluster/broker 설정: `Kafka` CR의 `config` (예: `default.replication.factor`, `auto.create.topics.enable`)
    - topic 설정: `KafkaTopic` CR의 `config` (예: `retention.ms`). broker 기본값을 topic별로 덮어씀.
    - broker 개별 리소스: `KafkaNodePool` (CPU, 메모리, 디스크)
  - **quota / throttling**
    - broker가 클라이언트별로 초당 바이트 수나 요청 비율을 제한할 수 있음.
    - 한 서비스가 폭주해서 클러스터 전체를 마비시키는 걸 막는 장치.
    - `client.id`나 사용자 단위로 걸기 때문에 인증을 켜야 의미가 커짐. 지금은 단일 테넌트라 미적용.
  - **확장**
    - broker를 추가해도 기존 partition은 자동으로 옮겨지지 않음. 새 broker는 새로 만든 partition부터 받음.
    - 기존 partition을 고르게 나누려면 재배치(reassignment)가 필요. Strimzi에서는 `KafkaRebalance`(Cruise Control)로 함.
  - **내구성 경계**
    - replication factor는 broker 수를 넘을 수 없음.
    - 우리 인프라에서는 RF 3 = broker 3대 → broker 1대 장애까지 쓰기 가능 (`min.insync.replicas: 2`).
    - 노드 2대가 `2b` AZ에 몰려 있어서, 2b AZ 장애 시 broker 2대를 동시에 잃음 → 쓰기 중단.

## Controller (KRaft)

- Cluster의 관리자 역할. 어느 broker가 살아 있고, 각 파티션의 leader가 누구인지 같은 **메타데이터를 관리.**
- before ZooKeeper → after KRaft
- K8s의 controller와는 다른 개념
- controller가 하나만 있으면 그 하나가 죽는 순간 클러스터 전체가 멈춤. 그래서 KRaft는 controller를 여러 대 두고 Raft 합의로 묶음.
- Metadata
  - “클러스터가 지금 어떻게 생겼는가”에 대한 정보 전부.
  - 메시지 내용은 들어 있지 않음.
  - follower가 이걸 복제하는 이유: leader가 죽어도 즉시 이어받고, 기록이 한 곳에만 있다가 사라지지 않게 하기 위함.
  - 메타데이터에 들어 있는 것
    | 분류             | 내용                                                       |
    | ---------------- | ---------------------------------------------------------- |
    | Broker           | 등록된 broker ID, 접속 주소, 살아 있는지 (fenced 여부)     |
    | Topic            | 이름, 고유 ID, 파티션 수                                   |
    | Partition status | 복제본 위치, leader, ISR, leader epoch                     |
    | 설정             | topic, broker 설정                                         |
    | 보안             | ACL, SCRAM 계정                                            |
    | 기타             | quota, producer ID 할당 범위, 기능 버전(`metadataVersion`) |
  - 들어 있지 않은 것
    - 메시지 자체: 각 broker의 파티션 로그에 존재
    - consumer offset: 내부 topic \_\_consumer_offsets에 있고, broker가 관리.
  - 저장 방식: 이벤트 로그
    - 메타데이터는 현재 상태 표로 저장하지 않고, 변경 이벤트를 순서대로 쌓은 로그(`__cluster_metadata`)로 저장.
    - `offset 100: RegisterBroker   { id: 2, host: ... }
offset 101: Topic            { name: content.file.changed.v1, id: ... }
offset 102: Partition        { p: 3, replicas: [0,1,2], leader: 0, isr: [0,1,2] }
offset 103: PartitionChange  { p: 3, leader: 1, isr: [1,2] }`
    - 로그가 무한히 길어지지 않도록 주기적으로 스냅샷을 떠서 앞부분을 정리.
  - 참고: broker도 이 로그를 받음.
    - 투표하지 않는 observer로 같은 로그를 받아 로컬에 캐싱.
- controller는 1 leader와 다수의 followers로 구성됨.
  - leader: 모든 결정을 내리는 active한 컨트롤러.
  - follower: 메타데이터 로그를 실시간 복제하면서 대기하는 컨트롤러.
- 우리 인프라에서는 pod 3개가 controller와 broker를 겸임(`roles: [controller, broker]`). controller 과반(2/3)이 살아 있어야 클러스터가 동작.
  - 이런 케이스를 combined mode라고 함.
  - kafka 공식 문서는 combined mode를 중요한(critical) 운영 환경에는 권장하지 않음. 소규모·개발용에 적합.
    - 이유: controller를 broker 부하로부터 격리할 수 없음. broker 부하가 크면 같은 JVM의 controller도 느려짐.
    - pod 하나가 재시작되면 broker와 controller를 동시에 한 대씩 잃음.
    - 규모가 커지면 controller 전용 pool을 따로 두는 구성이 일반적.
- KRaft
  - = Kafka + Raft(합의 알고리즘)
  - 메타데이터 관리 방식 모드
  - kafka 내부적으로 돌아가는 controller leader 합의 알고리즘
  1. 평상시: heartbeat
     - leader는 짧은 간격으로 follower들에게 alive 하다는 heartbeat 신호를 보냄.
     - 이때 메타데이터 변경분도 같이 복제
  2. leader가 죽으면: 선거
     - A: leader, B, C: follower.
     - 타임아웃: B가 time limit까지 hearbeat를 못 받으면 leader가 죽은 것으로 판단.
     - 이 대기 시간은 노드마다 무작위로 조금씩 다름.
       - 모두가 동시에 출마해 표가 갈리는 걸 줄이기 위함. 무작위로 흔드는 jitter가 맞음.
     - B가 term(임기 번호)을 1 올리고, 자신에게 한 표를 준 뒤 C에게 투표를 요청.
       - 로그 전체를 넘기는 게 아니라, 자기 로그의 마지막 항목 정보(term, offset)만 같이 보냄.
     - C는 두 조건을 모두 만족하면 찬성.
       1. 이번 term에 아직 아무에게도 투표하지 않음.
       2. B의 로그가 자기 것보다 뒤처지지 않음
          - 비교 기준: 마지막 항목의 term을 먼저 비교하고, 같으면 로그 길이(offset)를 비교.
          - '전체에서 가장 최신인 후보'가 당선되는 게 아니라 '투표자보다 뒤처지지 않은 후보'가 당선됨. 그래도 확정된 데이터는 잃지 않음 (아래 3번).
     - B가 과반을 얻으면 이번 term의 leader가 되어 heartbeat를 보내기 시작. 나중에 A가 살아나도 자기 term이 B’s term보다 낮은 걸 보고 follower로 돌아감
     - 표가 갈려 아무도 과반을 못 얻으면 그 term은 당선자 없이 끝나고, 무작위 대기 후 term을 올려 재선거.
     - 과반은 살아 있는 대수가 아니라 구성 대수 기준. 5대 중 1대가 죽어도 필요한 표는 3표.
     - term은 일종의 세대 번호. 낮은 term의 요청은 무시되므로, 네트워크 단절에서 돌아온 옛 leader(zombie)가 결정을 내리는 걸 막음.
  3. 왜 과반인가
     - 과반끼리는 반드시 한 명 이상 겹치기 때문.
     - leader가 둘 생기지 않음.
       - 3대 중 2표를 받은 후보가 동시에 둘일 수 없음.
     - 데이터를 잃지 않음.
       - 메타데이터 확정(commit)은 과반에 복제되어야 가능함. 확정되어야 요청자에게 성공 응답이 감.
       - 확정된 항목이 있으면, 그 항목이 없는 노드는 많아야 (전체 - 과반)대. 그래서 그 항목이 없는 후보는 과반 표를 모을 수 없음.
       - 따라서 당선된 leader가 가지지 않은 메타데이터는 uncommitted 상태임.
         - leader는 확정된 메타데이터를 **반드시 전부** 가지고 있음. (확정 전 항목을 더 가지고 있을 수는 있음)
         - follower에만 있던 확정 전 항목은 새 leader의 로그로 덮어써짐. 성공 응답이 나간 적 없으니 요청자가 재시도함.
  - 그래서 3대는 1대 장애, 5대는 2대 장애까지 견딤.
    - 4대는 과반이 3이라 3대와 똑같이 1대 장애만 견딤. 비용만 늘어서 보통 홀수로 구성.
  - 과반을 잃으면 (3대 중 2대 장애)
    - leader 선출도, 메타데이터 확정도 불가 → partition leader 재지정, topic 생성, ISR 변경이 전부 멈춤.
    - broker는 마지막으로 캐싱한 메타데이터로 기존 partition을 계속 서비스할 수는 있음.
    - 우리 인프라는 combined mode라 controller 2대 장애 = broker 2대 장애 → `min.insync.replicas: 2`를 못 채워 쓰기도 멈춤.
    - 1대가 돌아오면 과반이 회복되어 자동으로 재선거·복구. 확정 데이터는 잃지 않음.
