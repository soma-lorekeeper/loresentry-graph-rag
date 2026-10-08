# 갱신안 fake 기반 검증

현재 [S3 저장소 어댑터](../implementation/S3_ARTIFACTS.md)는 구현했고 실제 boto3의 로컬 HTTP로
검증했다. 기본 평가는 fake 저장소를 유지하며 운영 버킷·IAM·Content 입력 매핑과
Kafka 연결은 후속 작업이다.

[LOREKEEPER-625](https://lorekeepers.atlassian.net/browse/LOREKEEPER-625)에서 내부 골격과
여섯 외부 IO fake를 만들었다. 이후 [rules 이식 결과](rules-implementation.md)에서
기본 판단을 구현하고 실제 rules와 fake IO를 연결했다. HTTP·Kafka 전달 계층에는 연결하지 않았다.

## 대체 구현의 구분

| 구현 | 용도 | 보장하지 않는 것 |
|---|---|---|
| `FakeArtifacts`, `FakeJsonArtifacts` | 입력·불변 실행 스냅샷·결과 저장, JSON 왕복, 저장 실패 주입 | S3 내구성·권한·원자성 |
| `FakeRelations`, `FakeDocuments` | 관계 반영 상태와 추가 본문·누락·삭제·다른 프로젝트 자료 | 실제 Neptune 조회·Content 인증 |
| `FakeModel` | 고정 후보 또는 호출별 후보·예외 순서 | 실제 모델의 정확도·비용 |
| `FakeJobs` | 요청 충돌·실행 소유권·상태 전이 | 분산 잠금·lease·프로세스 장애 복구 |
| `FakePublisher` | 발행 전 실패, 전달 후 응답 유실 | Kafka 전달·offset·DLQ |
| `ScriptedRules` | 순서·실패 전파에 집중하는 기존 골격 테스트 | 업무 규칙의 정확성 |

실제 업무 판단은 `RefreshRules`로 검증한다. `ScriptedRules` 통과를 규칙 구현 근거로
사용하지 않는다. 기존 단일 문서 fixture와 다중 문서 실제 rules fixture를 함께 유지한다.

## 실행과 실패 주입

[테스트 안내](../../tests/refresh/README.md)의 `real_scenario()`는 실제 rules와 JSON fake
저장소를 연결한다. 기존 `tests/fakes/scenario.py`의 `scenario()`는 명시적으로 내부 stub을
주입한다. 운영 코드에서는 `tests/`를 import하지 않는다.

각 사례는 독립된 `Calls` 객체로 호출 순서와 인자를 기록한다.
`calls.failures[연산명]` 큐의 예외는 해당 호출을 실패시키고 `None`은 통과시킨다.
모델은 `responses` 큐로 호출별 후보·실패를 제공한다.
`publisher.publish`와 `publisher.ack`는 각각 전달 전 실패와 전달 후 응답 유실이다.

고정 실행 스냅샷 재사용, 결과 저장 후 모델 재호출 생략, 저장 전 재호출 가능성,
중복 발행과 실행권 해제 실패를 테스트한다. 메모리 상태로 재개하는 테스트이며
실제 프로세스 재시작·분산 동시성 검증이 아니다. 운영 연동은 [후속 계획](follow-up.md)을 따른다.
