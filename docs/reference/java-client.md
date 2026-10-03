# Java kafka-clients 실행 구조

애플리케이션 스레드와 백그라운드 스레드 사이의 버퍼·이벤트·Future 전달을 설명한다.

관련 문서: [네트워크](network.md), [Spring Kafka](spring-kafka.md).

- **app 스레드와 백그라운드 스레드**
  - Kafka 클라이언트 라이브러리(Java 기준)는 두 종류의 스레드로 돎.
  - app 스레드: 애플리케이션이 원래 돌리던 스레드. send(), poll(), commitSync() 같은 API를 부르면 라이브러리 코드가 이 스레드 위에서 실행됨. 라이브러리가 만든 스레드가 아님.
    - 우리 인프라에서는 content-api의 outbox relay라면 스케줄러가 실행하는 스레드(virtual thread를 켜 두어 가상 스레드), consumer라면 앱이 만든 poll 루프 스레드.
  - 백그라운드 스레드: 라이브러리가 생성자에서 하나 띄움. 네트워크 I/O를 전담하는 이벤트 루프 ([네트워크의 이벤트 루프](network.md)).
    - producer: sender thread (kafka-producer-network-thread).
    - consumer(`group.protocol=consumer`): consumer_background_thread.
  - 나눈 이유: 앱이 처리로 바쁜 동안에도 전송과 heartbeat가 멈추지 않고, 네트워크를 기다리는 동안 app 스레드가 붙잡히지 않게 하려는 것.
- **스레드 사이의 통로**
  - 큐: 요청이나 알림을 이벤트 객체로 만들어 넣으면 상대가 꺼내 처리함.
  - 공유 버퍼: 데이터 자체를 lock으로 보호되는 자료구조에 넣고 상대가 꺼냄.
  - Future: '나중에 채워질 결과' 객체. 기다리는 쪽은 get()에서 멈추고, 채우는 쪽이 완료시키면 풀림.
  - 깨우기: 상대가 자고 있으면 넣은 쪽이 깨워야 함. 상대가 selector에서 자면 selector wakeup, lock의 condition에서 자면 signal.
- **Producer: 공유 버퍼 + Future**
  - app → sender: RecordAccumulator(partition별 batch 대기열)가 공유 버퍼. send()가 레코드를 batch에 넣고 바로 리턴하면, sender thread가 batch 단위로 꺼내 보냄. 이벤트 큐는 없음.
  - 깨우기: batch가 꽉 찼거나 새 batch가 생기면 send()가 sender thread를 깨움. 그 외에는 sender가 `linger.ms`가 지나 스스로 깨어날 때 보냄.
  - sender → app: 레코드마다 Future. 응답이 오면 sender thread가 Future를 완료하고 callback을 실행함.
    - callback은 app 스레드가 아니라 sender thread에서 실행됨. 여기서 DB 작업처럼 오래 걸리는 일을 하면 이 producer의 모든 전송이 멈춤.
  - KafkaProducer는 여러 app 스레드가 동시에 send()해도 됨. accumulator가 동시 접근을 처리함.
- **Consumer: 이벤트 큐 2개 + 공유 버퍼** (`group.protocol=consumer` 기준)
  ```
  app 스레드                                          백그라운드 스레드 (루프)
   poll() ── 신호 이벤트 ──▶ [application event queue] ──▶ ① 큐를 비우며 이벤트 처리
          └─ wakeup ─ selector 깨움 ──────────────────────▶ ② 보낼 요청 모으기
   commitSync() ── 이벤트(+Future) ──▶ 큐                    ③ 소켓 select (최대 5초)
          └─ Future 대기 ◀────────── Future 완료 ─────────── ④ 기한 지난 이벤트 정리
   poll() 안에서 꺼냄 ◀── [background event queue] ◀── 에러, 콜백 요청
   poll() 대기 ◀── signal ── [FetchBuffer] ◀── Fetch 응답 넣기
  ```

  - app → 백그라운드 (application event queue)
    - 보내고 끝: 큐에 넣고 백그라운드를 깨운 뒤 바로 돌아옴. poll()의 신호가 이 방식이라 poll()이 네트워크를 기다리지 않음.
    - 보내고 결과 대기: 이벤트에 Future와 기한이 붙음. app 스레드는 Future를 기다리고, 백그라운드가 일을 끝내면 완료해서 풀어 줌. commitSync()와 subscribe()가 이 방식. 기한이 지나면 백그라운드가 timeout 예외로 끝냄.
    - 깨우는 이유: 백그라운드는 ③에서 소켓을 최대 5초까지 기다림. 깨우지 않으면 그동안 이벤트가 처리되지 않음.
  - 백그라운드 → app (background event queue)
    - 깨우지 않음. app 스레드가 poll() 같은 API를 부를 때 꺼내 봄.
    - 에러: 다음 poll()에서 예외로 던져짐.
    - rebalance 콜백은 왕복
      1. 백그라운드가 '콜백 필요' 이벤트(Future 포함)를 넣고, 배정 반영을 멈춘 채 기다림.
      2. app 스레드가 poll() 안에서 꺼내 onPartitionsAssigned 같은 콜백을 실행함.
      3. app 스레드가 '콜백 완료' 이벤트를 application event queue로 돌려보냄.
      4. 백그라운드가 Future를 완료하고 반영을 이어 감(확인 heartbeat).
      - 그래서 poll()을 부르지 않으면 rebalance가 끝나지 않음.
  - 레코드 데이터 (FetchBuffer)
    - Fetch 응답은 이벤트로 보내지 않음. 백그라운드가 lock을 잡고 버퍼에 넣은 뒤 signal을 보냄.
    - poll() 안에서 버퍼가 비어 기다리던 app 스레드가 바로 깨어나 꺼냄. CRC 검사, 압축 해제, 역직렬화는 꺼낸 app 스레드가 함.
  - 구독 상태, 읽을 위치 같은 공유 객체는 큐를 거치지 않고 동기화된 객체로 두 스레드가 직접 봄.
  - KafkaConsumer는 한 번에 한 스레드만 호출해야 함. 동시에 부르면 **ConcurrentModificationException**.
  - classic consumer(`group.protocol=classic`)는 구조가 다름. fetch는 app 스레드의 poll()이 직접 보내고, heartbeat만 전용 스레드가 보냄 ([Consumer](kafka-clients.md)).
- **broker와 같은 패턴**
  - broker: I/O thread가 response queue에 넣고 network thread의 selector를 깨워 응답을 넘김 ([브로커 요청 처리 구조](network.md)).
  - consumer: app 스레드가 이벤트 큐에 넣고 백그라운드의 selector를 깨워 요청을 넘김.
  - 'selector에서 자는 이벤트 루프 스레드에 일을 넘기려면 큐 + wakeup'이라는 같은 해법.
