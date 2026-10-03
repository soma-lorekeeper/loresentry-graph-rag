# Refresh 실행 골격

`RefreshService`에 여섯 외부 IO 구현과 `RefreshRules`를 생성자 주입한다.
기본 업무 규칙은 모두 미구현 오류를 발생시키며 HTTP·Kafka에 연결하지 않았다.

- [현재 구현과 후속 마이그레이션](../../docs/migration/fake-baseline.md)
- [외부 IO 없이 실행하는 테스트](../../tests/refresh/README.md)

테스트 전용 fake와 내부 stub은 `tests/fakes/`에 있고 운영 코드에서 import하지 않는다.
