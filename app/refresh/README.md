# Refresh 갱신안 생성

`RefreshService`는 여섯 외부 IO 포트와 `RefreshRules`를 생성자로 주입받는다.
기본 rules는 입력·선택·청킹·문맥·후보 검증을 수행한다. OpenAI 어댑터는 별도 평가
스크립트에서 주입한다. [S3 저장소 어댑터](../../docs/implementation/S3_ARTIFACTS.md)도
서비스에 주입할 수 있다. 기본 평가는 저장소 fake를 사용하며 관계·Content·작업 상태·
완료 발행의 운영 어댑터와 HTTP·Kafka 진입점은 미연결이다.

처음 읽을 때는 `models.py`에서 값의 의미를 보고, `rules.py`의 판단 단계를 확인한 뒤
`service.py`의 조회·판단·실행 순서를 읽는다. `ports.py`는 실행 구현이 지킬 계약이다.

| 파일 | 책임 |
|---|---|
| `selection.py` | 입력 범위·manifest 검증, 부족한 본문 ID 선택, 스냅샷 확정 |
| `evidence.py`, `../text/chunking.py` | 원문 구간 분할과 근거 중복 제거 |
| `context.py`, `prompts.py` | 결정적 출처·문맥·프롬프트 구성과 예산 확인 |
| `prompt_versions.py` | 실제 생성에 허용한 프롬프트 버전과 기본 v11 정의 |
| `validation.py` | 문서·관계 후보 검증, 중복·충돌·NO_CHANGE 판정 |
| `facts.py` | 복원한 원문에서 구조화 사실의 행동·명시 역할 식별·제안 참조 검사와 관계 설명 조립 |
| `repair.py` | 구체적 검증 진단·실패 후보를 같은 문맥에 넣는 1회 보완 입력과 예산 검사 |
| `serialization.py` | 결과 JSON과 완료 이벤트 값으로 순수 변환 |
| `service.py` | IO 순서·다중 호출·저장·발행·복구 조율 |

판단 함수는 포트를 호출하지 않는다. 추가 자료가 필요하면 ID를 반환하고 서비스가 조회한
결과를 다시 전달한다. 시간·난수·환경변수도 순수 함수에서 조회하지 않는다.

후보 계약은 `refresh-candidate-v5`를 유지하며 기본 프롬프트는 `refresh-prompt-v11`이다.
명시적으로 선택한 v12는 사실을 먼저 구조화하고 source 원문과 대조한 뒤 추가·수정
본문과 관계를 작성하도록 보강한다. 행위자·수령인·소유자·부정·조건·범위·시점·확실성을
보존하며, 그대로 남기는 기존 본문의 사실을 모두 재추출하지 않는다. 각 fact의 source는
문맥 내 마커 하나만 참조하고 서버가 원래 문서·revision·구간·quote를 복원한다.

`SelectionSettings.chunk_overlap_chars`는 기본 0이다. 0이면 과거 청크·직렬화를 유지하고,
양수는 `chunk_chars`보다 작은 정수만 허용한다(bool은 거절). 이전 청크의 완전한 문장을
한도와 여유가 있을 때 다음 청크에 겹치며, 여유가 없으면 생략한다. 문장부호·줄바꿈 경계와
과대 문장 분할을 사용하고 원문 위치·전체 덮기·청크 크기·전진을 보존한다. 근거 검증도
동일 설정으로 청크를 재생성하고, 겹친 텍스트를 포함한 문맥에 예산을 적용한다.

현행 v5 후보에 구체적 검증 진단이 있으면 서비스가 같은 범위에서 최대 한 번 보완한다.
출처·행동 표현·역할·사실 참조의 오류에 필드 경로와 규칙을 전달하며, 원문과 후보를
서버가 임의로 수정하지 않는다. 이전에 통과한 호출은 반복하지 않고 보완 응답으로 실패
후보 전체를 교체한다. 추가 시도에도 호출·문맥·어댑터 토큰 예산을 적용하며 남은 최초
호출을 예약한다. 전체 통합 실패와 연동·인증·설정 오류는 자동 보완하지 않는다.
보완 실패는 제안 없는 최종 FAILED이며 저장된 FAILED도 동일 요청에서는 그대로 재사용한다.

Responses 어댑터는 seed 설정을 제공하지 않고 `reasoning.effort`도 생략하여 기본
`medium`을 유지한다. 실제 `gpt-6-luna` smoke는 v11의 record-and-consent 한 사례에서
4회 호출·보완 1회 성공 뒤 `PROPOSED`였지만 의미·자유 본문은 사람 검토 대기다.
반복 의미 평가에서도 v12의 우세는 확인하지 못해 기본 v11을 유지했다.
긴 원고의 출력 한도 실패를 확인하고 실행 예산은 16,384토큰으로 늘렸으며,
일반 의미 품질은 [실제 비교 기록](../../docs/migration/refresh-quality.md#6-luna-실제-비교-기록)을 따른다.

[반복 비교 CLI](../../scripts/evaluate_refresh_ab.py)는 v11·v12를 각각 최소 2회 독립 실행하고
선택적으로 v12의 문장 겹침 조건도 비교한다. 보완을 포함한 자식 전체 호출 예산을 미리
예약한다. 비교 요약은 원문·제안 본문·공급자 오류 없이 수치와 상태만 0600 권한으로
저장하며 사람 의미 검토를 남긴다. 명령과 전체 상한 계산은
[어댑터 사용법](../../docs/migration/openai-adapter.md#프롬프트와-청크-겹침-반복-비교)을 따른다.

- [Content 기준 용어집](../../docs/domain-glossary.md)
- [실제 이식 내용과 근거](../../docs/migration/rules-implementation.md)
- [입출력 계약](../../docs/migration/proposal-contract.md)
- [테스트 실행](../../tests/refresh/README.md)

클래스·함수 첫 문자열은 docstring이다. 책임과 사용 조건을 한국어로 설명하고 필요하면
Google 스타일 `Args:`·`Returns:`·`Raises:`를 사용한다.

```bash
.venv/bin/python -m pydoc app.refresh.service.RefreshService.run
```
