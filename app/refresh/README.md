# Refresh 갱신안 생성

`RefreshService`는 여섯 외부 IO 포트와 `RefreshRules`를 생성자로 주입받는다.
기본 rules는 입력·선택·청킹·문맥·후보 검증을 수행한다. 실제 IO 어댑터와 HTTP·Kafka
진입점은 연결하지 않았으며, 테스트는 실제 rules와 외부 fake를 조합한다.

처음 읽을 때는 `models.py`에서 값의 의미를 보고, `rules.py`의 판단 단계를 확인한 뒤
`service.py`의 조회·판단·실행 순서를 읽는다. `ports.py`는 실행 구현이 지킬 계약이다.

| 파일 | 책임 |
|---|---|
| `selection.py` | 입력 범위·manifest 검증, 부족한 본문 ID 선택, 스냅샷 확정 |
| `evidence.py`, `../text/chunking.py` | 원문 구간 분할과 근거 중복 제거 |
| `context.py`, `prompts.py` | 결정적 출처·문맥·프롬프트 구성과 예산 확인 |
| `validation.py` | 문서·관계 후보 검증, 중복·충돌·NO_CHANGE 판정 |
| `serialization.py` | 결과 JSON과 완료 이벤트 값으로 순수 변환 |
| `service.py` | IO 순서·다중 호출·저장·발행·복구 조율 |

판단 함수는 포트를 호출하지 않는다. 추가 자료가 필요하면 ID를 반환하고 서비스가 조회한
결과를 다시 전달한다. 시간·난수·환경변수도 순수 함수에서 조회하지 않는다.

- [Content 기준 용어집](../../docs/domain-glossary.md)
- [실제 이식 내용과 근거](../../docs/migration/rules-implementation.md)
- [입출력 계약](../../docs/migration/proposal-contract.md)
- [테스트 실행](../../tests/refresh/README.md)

클래스·함수 첫 문자열은 docstring이다. 책임과 사용 조건을 한국어로 설명하고 필요하면
Google 스타일 `Args:`·`Returns:`·`Raises:`를 사용한다.

```bash
.venv/bin/python -m pydoc app.refresh.service.RefreshService.run
```
