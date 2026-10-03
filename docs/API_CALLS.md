# GraphRAG가 호출하는 API

> **책임:** Neptune 진단과 OpenAI 생성 호출의 설정·경계·실패 처리를 설명한다.
>
> **호출자·대상:** GraphRAG가 Neptune의 상태 API를 호출한다.
>
> **확인할 때:** 연결 설정이나 DB 진단 실패를 확인할 때.
>
> **관련 기준:** GraphRAG가 반환하는 응답은 [제공 API](API.md)에서 관리한다.

운영 HTTP 경로는 [neptune.py](../app/neptune.py)의 상태 조회를 사용한다.
별도 평가 실행에는 OpenAI Responses 호출 어댑터가 있다. 그래프 쿼리, Content·AI Chat
호출과 임베딩 호출은 아직 없다.

## 요청 구성

| 항목 | 현재 구현 |
|---|---|
| 메서드·주소 | `GET https://{NEPTUNE_ENDPOINT}:{NEPTUNE_PORT}/status` |
| 호출 시점 | `GET /health/db` 요청마다 한 번 |
| `NEPTUNE_ENDPOINT` | 호스트명. 미설정 시 `localhost` |
| `NEPTUNE_PORT` | 포트 문자열. 미설정 시 `8182` |
| 프로토콜 | `https`로 고정 |
| 클라이언트 | 요청마다 `httpx.Client(timeout=5.0)` 생성·종료 |
| 인증 | 명시적인 인증 헤더·AWS SigV4 서명 구현 없음 |
| 재시도 | 애플리케이션 수준의 재시도 루프 없음 |

호스트 값에 스킴이나 경로를 넣지 않는다. 코드는 URL 문자열을 조합할 뿐
환경변수의 빈 값·호스트 형식·포트 범위를 시작 시 검증하지 않는다.
환경변수는 `create_app()`에서 읽어 어댑터에 주입하며, 어댑터와 순수 판단 함수는 직접 읽지 않는다.
Neptune을 별도로 준비하지 않으면 기본 주소는 `https://localhost:8182/status`가 된다.
타임아웃 인자는 `5.0`이며 라우트 전체 처리에 대한 별도 총시간 제한은 없다.

## 응답 사용과 실패 처리

어댑터가 응답에 `raise_for_status()`를 적용한 뒤 JSON을 해석한다.
애플리케이션 서비스가 받은 결과를 순수 함수 `interpret_status()`에 전달한다.
`role`, `dbEngineVersion`, `gremlin.version`만 추출하며 전체 원문은 반환하지 않는다.
필드 누락의 기본값과 외부 응답 형태는 [제공 API](API.md#neptune-진단-성공)를 따른다.

잘못된 JSON 구조와 메타데이터 자료형은 순수 함수가 `InvalidGraphStatus`로 분류한다.
HTTP·네트워크·JSON 해석 실패는 어댑터가 `GraphStatusUnavailable`로 변환하며 라우트가 `503`으로 매핑한다.
실제 HTTP 어댑터·라우트 연결은 로컬 HTTP 서버를 이용한 [통합 테스트](../tests/test_neptune_integration.py)로 검증한다.
운영 Neptune의 TLS·IAM·VPC 연결은 별도 확인 대상이다.

갱신안 서비스는 S3·관계·Content·모델·작업 상태·완료 발행을 Protocol로 요청한다.
OpenAI 모델 포트는 실제 어댑터를 선택할 수 있고 나머지 다섯 포트는 fake다.
[포트 계약](../app/refresh/ports.py)과 [이식 결과](migration/rules-implementation.md)를 참고한다.

## 갱신안 모델 호출

[OpenAIProposalModel](../app/adapters/llm.py)은 동기 SDK의 `responses.create`로
OpenAI `POST /v1/responses`를 호출한다. FastAPI 라우트에는 연결하지 않았으며
평가 도구의 `--live` 모드에서 선택한다. 대화 응답 재생은 네트워크 호출을 하지 않는다.

키·모델·timeout·토큰 예산·클라이언트 종료는 실행 스크립트가 담당한다. SDK와 어댑터 모두
자동 재시도가 없다. 구조화 출력·오류 코드·스냅샷 버전·사용량 정책과 실행 명령은
[OpenAI 이식 결과](migration/openai-adapter.md)를 기준으로 한다.
