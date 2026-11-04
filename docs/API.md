# GraphRAG 제공 API

> **책임:** 현재 제공하는 HTTP 경로·응답·오류와 진단 결과의 의미를 설명한다.
>
> **제공자·호출자:** GraphRAG가 제공하며 클러스터 내부 호출을 전제로 한다.
>
> **확인할 때:** 서버를 호출하거나 진단 응답을 해석할 때.
>
> **관련 기준:** Neptune 요청 구성은 [호출 API](API_CALLS.md)를 본다.

현재 앱이 직접 정의한 라우트는 다음 세 개다. 공통 경로 접두사는 없으며 응답은 JSON이다.
요청 본문·쿼리·인증 헤더를 요구하는 업무 API는 아직 없다.

## API 목록

| 기능 | 메서드·경로 | 성공 응답 | 외부 통신 |
|---|---|---|---|
| 서비스 식별 | `GET /` | `200`, `{"service":"graph-rag-api"}` | 없음 |
| 프로세스 상태 | `GET /health` | `200`, `{"status":"ok"}` | 없음 |
| Neptune 연결 확인 | `GET /health/db` | `200`, 아래 진단 결과 | Neptune `GET /status` |

`/health`는 Neptune 상태를 확인하지 않는다. Kafka polling 비활성 시에는 DB가 응답하지 않아도
`200`을 반환한다. polling 활성 시 worker가 실패하거나 종료되면
`503`, `{"status":"error","error":"Kafka polling worker unavailable","trace_id":"<trace>"}`을 반환한다.
정상 응답은 worker 스레드의 생존 확인이며 Kafka 연결·할당·lag의 확인은 아니다.
자동 시작과 설정은 [polling 구현](implementation/KAFKA_POLLING.md)을 따른다.
FastAPI 기본 설정을 사용하므로 `/docs`, `/redoc`, `/openapi.json`도 비활성화하지 않은 상태다.

## Neptune 진단 성공

| 필드 | 값 또는 출처 |
|---|---|
| `status` | `ok` |
| `service` | `graph-rag-api` |
| `database` | `neptune` |

Neptune 주소·역할·엔진·Gremlin 버전은 응답에 싣지 않는다.

현재 코드는 Neptune 응답의 별도 상태 필드를 검사하지 않는다.
HTTP 상태 검사와 JSON 해석·필드 추출이 예외 없이 끝나면 성공으로 응답한다.
따라서 `200`은 그래프 쿼리·쓰기·검색 기능이 정상이라는 증거가 아니다.
`unknown`은 키 누락의 기본값이다. 최상위 응답과 `gremlin`은 객체여야 하며, 존재하는 메타데이터 필드는 문자열이어야 한다. 명시적인 `null`이나 잘못된 자료형은 `503`으로 처리한다.

## Neptune 진단 실패

조회 실패(`GraphStatusUnavailable`) 또는 응답 검증 실패(`InvalidGraphStatus`)는 다음 형태로 `503`을 반환한다.

```json
{
  "status": "error",
  "service": "graph-rag-api",
  "database": "neptune",
  "error": "Graph database unavailable",
  "trace_id": "<trace>"
}
```

`error`는 고정 문구다. 내부 주소·예외 문자열은 응답에 싣지 않는다. 실패 종류는 같은 `trace_id`의
로그 줄(`neptune status call failed`의 `error.type`, `request completed`의 `error.type`)에 남는다.
예상하지 못한 코드 오류는 연결 장애로 변환하지 않고 FastAPI의 기본 `500` 처리로 전달한다.

갱신안 rules·서비스는 내부 Python 유스케이스이며 제공 HTTP 경로를 추가하지 않았다.
현재 구현은 [rules 이식 결과](migration/rules-implementation.md)를 참고한다.
