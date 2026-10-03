# Kafka 네트워크와 I/O

소켓·이벤트 루프, 브로커 요청 큐와 연결 재사용을 설명한다.

관련 문서: [브로커와 purgatory](kafka-cluster.md), [Java 클라이언트](java-client.md).

- **소켓**
  - 네트워크 연결의 끝점. TCP로 연결되면 양쪽이 소켓을 하나씩 가짐 (producer 쪽 하나, broker 쪽 하나).
  - 프로그램 입장에서는 읽고 쓰는 파일과 같음. 한쪽이 쓰면 상대가 읽음.
  - 도착한 데이터는 프로그램이 읽을 때까지 OS 커널이 소켓 버퍼에 보관함.
  - 소켓은 번호표(파일 디스크립터)와 커널 버퍼일 뿐이라, 연결이 살아 있으려고 스레드가 그 앞에서 기다릴 필요가 없음.
- **블로킹 I/O와 이벤트 루프**
  - 블로킹 방식: 연결마다 스레드를 붙이고 `read()`를 부름. 데이터가 올 때까지 스레드가 멈춰서 기다림. 연결이 수천 개면 스레드도 수천 개.
  - 이벤트 루프 방식: 스레드 하나가 OS에 '이 소켓들 중 준비된 게 뭐야?'를 한꺼번에 물음(Linux `epoll`, Java `Selector`). 준비된 소켓만 읽고 쓰고, 다시 물음.
  - 우리 인프라에서는 graph-rag의 FastAPI(uvicorn)가 asyncio 이벤트 루프로 돎. Kafka network thread와 같은 방식.
- **Callback**
  - 결과를 기다리지 않고, 결과가 나오면 실행할 함수를 미리 넘겨 두는 것.
  - 예: producer의 `send(record, callback)`. `send()`는 바로 리턴하고, ack가 오면 라이브러리가 callback을 불러 줌.
- **broker의 요청 처리 구조**
  ```
  network thread ─▶ request queue ─▶ I/O thread ─┬─ 바로 처리 가능 ───────────────┐
   (소켓에서 읽기)                               └─ 불가능 → purgatory ─ 완료 ─┤
                                                                           ▼
  network thread ◀────────── response queue (network thread별) ◀────────┘
   (소켓에 쓰기)
  ```

  - network thread(`num.network.threads`, 3): 이벤트 루프로 소켓 수천 개를 돌봄. 요청을 읽어 request queue에 넣고, 응답을 소켓에 씀.
  - request queue(`queued.max.requests`, 500): 가득 차면 network thread가 소켓 읽기를 멈춤 → 클라이언트에서는 지연으로 보임.
  - I/O thread(`num.io.threads`, 8): 요청을 실제로 처리함 (log append, 읽기 등).
  - response queue는 network thread마다 따로 있음. 연결은 처음 맺을 때 network thread 하나에 배정되고, 그 연결의 응답은 항상 그 thread의 큐로 감.
- **기다림은 스레드가 아니라 객체**
  - I/O thread는 바로 끝낼 수 없는 요청을 `DelayedOperation` 객체(요청 + 완료 조건 + 응답을 보내는 콜백)로 만들어 purgatory에 넣고, 바로 다음 요청으로 감 ([브로커의 purgatory](kafka-cluster.md)).
  - 조건이 채워지면 그 변화를 일으킨 스레드가, timeout이면 purgatory 전용 타이머 스레드가 콜백을 실행함. 콜백은 응답을 response queue에 넣음.
  - 예: `acks=all` produce의 응답을 만드는 건 그 요청을 받은 스레드가 아니라, follower의 Fetch를 처리하다 high watermark를 올린 다른 I/O thread.
  - 대기 요청 하나의 비용은 객체 몇백 바이트. 스레드 스택(보통 1MB 예약)을 쓰지 않아서 수만 개가 쌓여도 괜찮음.
  - 코루틴이나 async/await와 같은 효과를 언어 기능 없이 콜백과 이벤트 루프로 직접 구현한 것. `DelayedOperation`이 손으로 쓴 continuation(이어서 할 일).
- **연결 하나는 한 번에 요청 하나씩 (channel mute)**
  - broker는 한 연결에서 요청을 읽으면, 그 응답을 보낼 때까지 그 연결의 다음 요청을 읽지 않음. 연결 단위로 순서를 보장하는 장치.
  - 그래서 Fetch가 purgatory에서 기다리는 동안 그 연결은 멈춤. Java consumer가 coordinator용 연결을 fetch용과 따로 여는 이유 (heartbeat가 대기 중인 Fetch 뒤에 막히지 않게).
  - producer의 `max.in.flight.requests.per.connection(5)`은 응답을 기다리지 않고 연달아 보내 왕복을 줄이는 것(pipelining)이지, broker가 동시에 처리한다는 뜻은 아님.
    - 요청을 응답을 기다리지 않고 최대 5개까지 연속으로 보낸다는 뜻.
    - 이러면 응답 1 → 요청 2 사이의 네트워크 왕복 시간을 줄일 수 있음.
    - 어차피 broker에서는 한 연결의 요청을 하나씩 차례로 처리함.
    - 재시도하면 순서가 뒤집힐 수 있는데, 이는 idempotent producer로 해결함.
      - producer별, partition별로 batch마다 sequence 번호가 붙음.
      - broker는 기대하는 sequence 번호가 아니면 OUT_OF_ORDER_SEQUENCE_NUMBER로 거부함.
      - 따라서 순서가 항상 보장됨.
      - 다만 broker가 기억하는 sequence 번호 상한은 최근 window 5이기에, in-flight 역시 5가 최대값.
- **클라이언트도 같은 구조**
  - 연결은 한 번 맺으면 계속 재사용함. 켜진 채 쉬는 연결은 `connections.max.idle.ms`(클라이언트 9분, broker 10분)가 지나면 정리됨. 클라이언트 쪽이 1분 짧아서 broker가 먼저 끊는 일 없이 클라이언트가 정리함.
  - 요청마다 번호(correlation id)가 붙고, 응답에 같은 번호가 돌아옴. 클라이언트는 응답을 기다리는 요청 목록(in-flight)을 들고 있다가, 번호로 맞춰 Future와 callback을 완료함.
  - 네트워크는 라이브러리의 백그라운드 스레드가 처리함. 앱 스레드가 멈추는 건 `future.get()`처럼 결과를 직접 기다릴 때뿐.
  - 응답이 `request.timeout.ms`(30초) 안에 오지 않으면 그 연결을 끊고, 기다리던 요청을 실패 처리한 뒤 재시도함.
- **HTTP와 비교**
  - 연결 재사용
    - HTTP/1.0: 기본은 요청 하나당 연결 하나. 응답을 보내면 닫음.
    - HTTP/1.1: keep-alive가 기본. 재사용하지만 한 연결에 요청 하나씩이라, 브라우저는 서버당 연결을 여러 개(보통 6개) 엶.
    - HTTP/2: 서버당 연결 하나를 오래 쓰고, stream id로 요청 여러 개를 동시에 주고받음. Kafka의 correlation id와 같은 역할. 다만 Kafka broker는 한 연결의 요청을 순서대로 하나씩 처리함.
    - HTTP/3: TCP 대신 QUIC(UDP 기반).
    - 쉬는 연결은 idle timeout으로 정리함 (nginx 기본 75초, AWS ALB 기본 60초). Kafka보다 훨씬 짧음.
  - 재사용하는 이유: 연결을 맺는 비용이 큼. TCP handshake에 왕복 1번, TLS까지 하면 왕복이 한두 번 더 듦. 연결을 자주 닫으면 `TIME_WAIT`가 포트를 한동안 붙잡음. 그래서 클라이언트는 connection pool을 씀.
    - 우리 인프라에서는 content-api의 Hikari(`maximum-pool-size: 5`)가 PostgreSQL 연결 5개를 열어 두고 돌려 씀.
  - 스레드 사용
    - Spring MVC(Tomcat): 연결은 selector로 관리하지만, 요청을 처리하는 worker thread는 응답을 보낼 때까지 붙잡힘(DB 응답을 기다리는 동안에도). 동시에 처리할 수 있는 요청 수는 대략 스레드 풀 크기(기본 200).
    - Kafka: I/O thread가 기다려야 하면 purgatory에 맡기고 반납함.
    - 우리 인프라의 content-api는 virtual thread를 켜 둠(`spring.threads.virtual.enabled: true`). 코드는 블로킹 방식이지만, 기다리는 동안 실제 OS 스레드를 내려놓아 비슷한 효과를 냄.
  - HTTP long polling은 Kafka Fetch의 long polling과 같은 발상. 서버가 보낼 게 없으면 응답을 붙잡아 두었다가, 데이터가 생기거나 timeout이 되면 응답함.
