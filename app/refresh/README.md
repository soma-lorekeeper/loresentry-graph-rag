# Refresh 실행 골격

`RefreshService`에 여섯 외부 IO 구현과 `RefreshRules`를 생성자 주입한다.
기본 업무 규칙은 모두 미구현 오류를 발생시키며 HTTP·Kafka에 연결하지 않았다.

- [Content 기준 도메인 용어집](../../docs/domain-glossary.md)
- [현재 구현과 후속 마이그레이션](../../docs/migration/fake-baseline.md)
- [외부 IO 없이 실행하는 테스트](../../tests/refresh/README.md)

테스트 전용 fake와 내부 stub은 `tests/fakes/`에 있고 운영 코드에서 import하지 않는다.

## 코드의 설명 읽기

클래스·함수 본문 첫 위치의 `"""..."""`는 docstring이다. 한국어로 책임과 사용 조건을
설명하고, 자세한 설명이 필요한 곳에는 Google 스타일의 `Args:`·`Returns:`·`Raises:`·
`Attributes:`를 사용한다. 타입 힌트만으로 알 수 있는 정보는 반복하지 않는다.

- `ports.py`: 외부 IO 구현이 지켜야 할 조회·저장·소유권·발행 계약.
- `service.py`: 실행 시 부수 효과, 실패 처리, 재호출과 중복 발행 가능성.
- `rules.py`: 구현할 판단 책임과 현재 항상 발생하는 미구현 예외.
- `models.py`: 내부 값의 의미와 데이터 생성만으로 검증되지 않는 조건.

공통 계약은 포트에 두고 실제 어댑터에는 해당 구현의 추가 조건을 적는다.
단순 초기화나 테스트 이름을 반복하는 설명은 생략한다. 동작이 바뀌면 docstring도
함께 수정하며, 문서에 적힌 계약과 실제 구현·테스트를 구분해서 확인한다.

레포 루트에서 프로젝트 가상환경으로 설명을 읽을 수 있다.

```bash
.venv/bin/python -m pydoc app.refresh.service.RefreshService.run
```

이 문서화로 업무 규칙이 구현되거나 HTTP·Kafka 연결이 추가된 것은 아니다.
