# GraphRAG 검증 계획

> **책임:** 기존 테스트가 검증하는 범위와 추가로 필요한 코드·운영 검증을 구분한다.
>
> **확인할 때:** 변경 후 검증할 시나리오를 고르거나 테스트 통과의 의미를 판단할 때.
>
> **관련 기준:** 응답은 [제공 API](../API.md), 호출 설정은 [호출 API](../API_CALLS.md), 실행 방법은 [프로젝트 README](../../README.md#test)를 본다.

검증은 순수 판단, 대체 구현으로 연결한 앱, 실제 HTTP IO를 나눈다.
로컬 통합 테스트는 임시 포트의 HTTP 서버를 사용하며 외부 네트워크·AWS 자격 증명·실제 Neptune은 필요하지 않다.

## 자동화 테스트

| 파일 | 확인 기준 |
|---|---|
| [test_graph.py](../../tests/test_graph.py) | 같은 입력은 같은 결과, 입력 불변, 누락은 unknown, 잘못된 구조·null·비문자열은 거절. FastAPI나 DB 없이 실행 |
| [test_main.py](../../tests/test_main.py) | 기본 라우트는 IO 없음, 성공 JSON 유지, 알려진 실패는 503, 코드 오류는 전파, 앱별 주입 의존성 독립 |
| [test_neptune_integration.py](../../tests/test_neptune_integration.py) | 실제 어댑터와 라우트 연결, GET /status 경로, JSON 정상·비정상, HTTP 실패, 읽기 타임아웃, 자동 재시도 없음, 반복 읽기 |

```bash
# 프로젝트의 Python 3.12 가상환경 사용
.venv/bin/python -m pytest tests/test_graph.py -q
.venv/bin/python -m pytest -m 'not integration' -q
.venv/bin/python -m pytest -m integration -q
```

전체 검증은 `.venv/bin/python -m pytest -q`로 실행한다.
[CI/CD](../../.github/workflows/ci-cd.yaml)의 기존 `pytest` 명령도 모든 테스트를 수집한다.
순수 테스트에는 서버가 없으며, 통합 테스트 실행 환경에는 루프백 소켓 사용 권한이 필요하다.

## 검증 한계

이 사례는 읽기 전용 운영 진단이다. 실제 업무 규칙·DB 쓰기 트랜잭션·Kafka 전달 보장을 검증한 것은 아니다.
통합 테스트는 HTTP 어댑터의 실제 전송을 검증하지만 Neptune의 TLS·IAM 인증·그래프 연산은 검증하지 않는다.
라이브 서비스에 도달하지 않았다는 사실과 테스트용 HTTP 서버에서 IO가 실행됐다는 사실을 구분한다.

## 운영 환경에서 별도 확인할 항목

| 항목 | 확인 기준 |
|---|---|
| 실제 배포 | 배포 이미지가 의도한 커밋인지, Pod 준비 상태와 실제 probe 경로가 무엇인지 확인 |
| Neptune 접근 | 실행 환경의 DNS·네트워크·TLS·포트와 실제 `/health/db` 응답 확인 |
| 인증 요구 | 대상 Neptune의 인증 요구와 현재 서명 없는 HTTP 요청의 호환 여부 확인 |
| 접근 제한 | 진단 API와 FastAPI 문서 경로가 의도한 네트워크 범위에서만 접근 가능한지 확인 |

이 항목들은 이 저장소의 테스트로 검증되지 않는다. 실제 운영 상태는 배포·인프라 증거와
실행 결과를 별도로 확인해야 한다.

## 업무 기능 구현 시 추가할 범위

그래프 저장·검색·이벤트 소비가 추가되면 프로젝트 격리, 접근 권한, 휴지통 제외,
명시적 파일 참조만의 관계 생성, 이벤트 중복·순서·재처리와 AI Chat 연동을 검증해야 한다.
현재는 해당 업무 코드와 테스트가 모두 없다.
