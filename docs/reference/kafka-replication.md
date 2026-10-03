# Kafka 복제·Leader·ISR

메시지 확정, 리더 전환과 가용성·내구성의 경계를 설명한다.

관련 문서: [클러스터와 KRaft](kafka-cluster.md), [발행 내부 흐름](producer-flow.md).

- **Replica**
  - 파티션 하나를 여러 broker에 복제한 것.
  - replication factor에는 leader 자신도 포함. RF 3 = leader 1 + follower 2.
  - 같은 partition의 replica는 서로 다른 broker에 놓임. 그래서 RF는 broker 수를 넘을 수 없음.
- **Leader**
  - 복제본 중 읽기, 쓰기를 담당하는 하나. 나머지는 follower로 leader를 따라 복사함.
    - leader가 밀어주는 게 아니라 follower가 leader에게 fetch 요청을 보내 가져가는 pull 방식. consumer와 같은 방식으로 복제함.
  - partition 마다 따로 존재.
  - leader가 죽으면, controller가 복제본 목록을 순서대로 보면서 살아있고 ISR에 있는 첫 번째 복제본을 leader로 지정.
  - preferred leader: replica 목록의 첫 번째. 장애 후 복구되면 leader를 여기로 되돌려(`auto.leader.rebalance.enable`) 한 broker에 leader가 몰리지 않게 함.
  - leader epoch: leader가 바뀔 때마다 증가하는 번호. Raft의 term과 같은 역할. 새 leader 기준으로 follower 로그의 어긋난 끝부분(확정 전 메시지)을 잘라냄.
  - controller leader와 구분
    - controller leader: 클러스터에 1개, Raft 투표로 선출
    - partition leader: partition마다 1개, controller가 ISR 중에서 지정 (투표 없음)
  - consumer가 항상 leader에서 읽는 건 아님 (KIP-392)
    - `client.rack`을 설정하고 broker가 `RackAwareReplicaSelector`를 쓰면, consumer는 같은 AZ에 있는 follower에서 읽을 수 있음.
    - 목적은 AZ 간 데이터 전송 비용 절감. AWS에서 AZ를 넘는 트래픽은 비용이 발생함.
    - 읽는 범위는 여전히 high watermark까지라 일관성은 유지됨.
- **ISR(In-Sync Replicas)**
  - leader를 제때 따라잡고 있는 복제본 집합.
  - 저장 완료라고 답해야 하는 시점
    - 파티션 복제본이 3개(leader 1 + follower 2)일 때, producer에게 성공을 언제 알릴지 정해야 함.
    - leader만 쓰면 성공이라 하면, 빠르지만 leader가 죽는 순간 follower에 없던 메시지가 유실됨.
    - 모든 replica에 write하면 성공이라 하면, 안전하지만 replica 하나만 느려도 전체 쓰기가 느려지고, 하나만 죽어도 쓰기가 멈춤.
    - ISR은 그 사이를 선택. 신뢰할 수 있는 복제본 집합을 replica의 부분 집합으로 정의하고 유지함. 이 부분 집합은 변경될 수 있음.
  - replica.lag.time.max.ms 안에 leader의 끝까지 따라온 적이 있으면 ISR에 남음.
  - 명단은 동적으로 이 time limit 안에 들어온 replica를 잡음.
  - 확정(commit)의 기준은 따라서 ISR 전원 기록이 됨.
    - `acks=all`이면 메시지가 현재 ISR 전원에 기록됐을 때 확정되고 producer에게 성공이 감.
    - 확정 자체는 acks 설정과 무관하게 ISR 전원 기록으로 결정됨. acks는 producer에게 '언제 성공이라고 답할지'만 정함. `acks=1`이면 확정 전에 성공을 받으므로 유실될 수 있음.
    - ISR에서 빼거나 넣는 판정은 partition leader가 제안하고, controller가 메타데이터로 확정함.
  - ISR 멤버가 모두 죽으면,
    - unclean.leader.election.enable로 동작이 갈림.
    - false(default): 멤버가 돌아올 때까지 파티션을 멈춤.
    - true: ISR 밖의 뒤쳐진 복제본이라도 leader로 세움. 서비스는 재개되지만 확정된 메시지를 잃을 수 있음.
  - high watermark
    - ISR 전원이 가진 메시지의 경계. 정확히는 '확정된 마지막 offset + 1'이라, high watermark 미만까지가 확정된 메시지.
    - consumer는 high watermark 미만까지만 읽을 수 있음. 확정 전 메시지는 볼 수 없음.
  - 동적 축소
    - follower가 timelimit 이내에 write를 따라오지 못하면 ISR에서 제외됨.
    - 반대로 timelimit 내에 따라오면 ISR에 다시 들어옴.
  - 축소의 하한: `min.insync.replicas`
    - 축소를 무한정 허용하면 ISR이 leader 하나만 남는 문제가 생길 수 있기에, 하한을 정의해둠.
    - 가용성을 조금 포기하고 내구성을 지킴.
    - `acks=all`인 producer에만 적용됨. `acks=0/1`은 ISR 크기와 상관없이 쓰기가 진행됨.
    - `replica.lag.time.max.ms`를 넘어서면, 해당 follower를 ISR 그룹에서 제외. 또한 ISR 그룹이 `min.insync.replicas`를 복구할 때까지 `acks=all` produce 요청은 모두 거부됨.
      - acks=1, acks=0에 대해서만 produce 요청이 허용됨.
- acks=all + min.insync.replicas:2: ISR 2개 이상에 기록돼야 쓰기 성공이라는 뜻.
- **Raft 과반 vs ISR**
  - controller 메타데이터: 고정된 과반에 기록되면 확정. f대 장애를 견디려면 2f+1대 필요.
  - partition 메시지: 동적인 ISR 전원에 기록되면 확정. f대 장애를 견디려면 replica f+1개로 충분.
  - 메시지는 데이터 양이 커서 replica 하나가 곧 디스크 비용이라 ISR 방식이 유리. 대신 'ISR이 지금 누구인가'는 Raft로 운영되는 controller가 확정.
  - 우리 인프라처럼 RF 3 + min.insync 2면 결과적으로 '3개 중 2개'라 과반과 비슷해 보이지만 원리는 다름.
