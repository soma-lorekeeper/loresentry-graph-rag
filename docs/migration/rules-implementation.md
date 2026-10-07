# 갱신안 rules 이식 결과

[LOREKEEPER-637](https://lorekeepers.atlassian.net/browse/LOREKEEPER-637)에서 순수 rules,
다중 대상 모델 호출 조율, 결과 JSON 변환을 구현했다. 실제 rules와 여섯 fake IO를
연결해 검증한다. 이식 경위와 과거 검증 기록을 보존하며 아래 동작 설명은 현재 구현 기준이다. 이후 [OpenAI 어댑터와 응답 대체 평가](openai-adapter.md)를
추가했다. 실제 모델 품질·S3·Content·Neptune·Kafka 운영 연결은 후속이다.
입출력 한도와 결과 형식의 기준은 [제안 계약](proposal-contract.md)이다.

## 무엇을 어디에 가져왔는가

| 기존 AI 부품 | 현재 GraphRAG 구현 | 이식 판단 |
|---|---|---|
| `index/splitters.py`의 원문 구간·긴 문장 분할·문장 묶기 | [chunking.py](../../app/text/chunking.py) | Neo4j 타입과 KSS 실행기를 제거하고 구간 계산을 재작성. 공백·반복 문장도 빠짐없이 보존 |
| `index/indexing_service.py`의 청크 출처 마커 | [context.py](../../app/refresh/context.py) | 문서 ID·revision·원문 위치를 정렬해 C0001 마커 부여. 청크 저장·임베딩 제외 |
| `index/context_service.py`의 배경 문맥 구성 | [context.py](../../app/refresh/context.py) | 주입된 문서·관계만 JSON으로 구성. DB 조회·전역 그래프 덤프·누적 요약 제외 |
| `index/extractor.py`, `extraction_examples.py`의 원문·스키마·근거 제약 | [prompts.py](../../app/refresh/prompts.py) | 기존 그래프 추출 프롬프트를 문서 교체·관계 ADD 제안으로 재작성. 문서 속 명령은 분석 자료로 구분 |
| `detect/docstore.py`의 근거 중복 제거 | [evidence.py](../../app/refresh/evidence.py), [serialization.py](../../app/refresh/serialization.py) | Neo4j element ID 대신 문서·revision·구간·인용을 자연키로 사용 |
| `tenant.py`, `kg_scope.py`의 범위 일치 원칙 | [selection.py](../../app/refresh/selection.py) | 프로젝트·문서 상태 검증으로 재작성. 실제 인증·인가는 어댑터 책임 |
| `graph_schema.py`, `extraction_pipeline.py`의 출력 제약·검증 순서 | [validation.py](../../app/refresh/validation.py) | Content 분류 키·두 방향 관계·기준 revision·근거 검증을 신규 작성. writer·resolver·자동 병합 제외 |

기존 AI에는 이 갱신안 유스케이스 자체가 없다. 코드 전체를 복사한 것이 아니라
원문 위치·출처·문맥·검증 원칙을 선별 이식하고 Content 계약에 맞춰 다시 구현했다.
KSS는 이번 단계에 추가하지 않았다. 문장부호·줄바꿈과 크기 기준으로 모든 원문을
보존하는 것이 현재 검증 목표이며, 한국어 문장 경계의 품질 평가는 실제 모델 평가와 분리한다.

## 판단과 실행의 위치

`RefreshRules`는 전달된 값으로 검증·선택·분할·문맥·후보 판정을 수행한다.
`RefreshService`는 입력 읽기, 관계 조회, 부족한 본문 조회, 스냅샷 저장, 모델 호출,
결과 저장, 완료 발행을 조율한다. `ports.py`의 Protocol 여섯 개에 실제 어댑터나 fake를
주입한다. 일반 함수와 dataclass로 이 경계를 표현할 수 있어 Free Monad나 범용 실행기를
추가하지 않았다. 규칙은 IO 포트를 호출하지 않는다.

변경 입력과 갱신 대상은 다르다. 모든 변경 문서를 보존하고, seed에서 명시적 관계로
도달한 문서와 요청의 명시 대상을 선택한다. 연결되지 않은 그래프 조회 ID는 무시한다.
명시 대상이 없으면 선택된 ACTIVE 설정 문서(MANUSCRIPT 외 분류)를 대상으로 삼는다.
추가 조회가 누락되거나 비활성이면 실패하고, 원본 입력을 최신 조회 값으로 덮어쓰지 않는다.
입력에 포함된 비활성 변경은 개수에 포함하지만 문맥·대상·관계 확장에는 사용하지 않는다.

요청의 `changed_documents`가 있으면 S3 문서 목록·revision·상태와 정확히 대조한다.
기존 내부 호출과 골격 fixture는 빈 manifest를 허용한다. Kafka 어댑터는 이벤트의
`files`를 이 값으로 전달하고 S3 객체 fingerprint를 검증해야 한다. 빈 manifest를
외부 요청 검증 완료로 취급해서는 안 된다.

대상별 한 번의 모델 호출에 선택된 모든 ACTIVE 문서의 원문을 제공한다. 여러 변경
원고가 같은 설정에 영향을 주는 자료를 분리하지 않기 위한 보수적 MVP 정책이다.
대상이 없어도 ACTIVE 변경 자료가 있으면 생성·관계 제안 호출을 한 번 수행한다.
원고 한 편만 있는 경우도 포함한다. 첫 호출만 새 설정 생성과 그 후보 연결을 허용하고
후속 호출은 기존 문서만 다룬다. ACTIVE 변경이 없으면 모델 호출 없이 NO_CHANGE다.
전체 프롬프트와 호출 수 예산을 호출 전에 검증하며 잘라내서 성공시키지 않는다.

후보는 대상별 검사 후 전체 요청에서 다시 통합 검증한다. 같은 대상·필드의 다른 값,
같은 새 연결의 다른 설명은 충돌 실패다. 동일 후보는 근거를 합치고, 기존 본문과 같은 값과
기존 연결은 제외한다. 인용 일치는 사실이 제안을 의미적으로 뒷받침하는지까지 증명하지 않는다.

## 새 설정 생성과 원고 보호

`NewDocumentProposal`은 분류·이름·본문·원고 근거와 임시 후보 ID를 담는다.
관계는 기존 문서 또는 검증된 새 후보를 참조하며 새 후보의 기준 revision은 null이다.
원고 수정·생성, 없는 참조, 잘못된 분류 키·근거, 이름 중복·본문 충돌은 요청 전체를 실패 처리한다.
중복 검사는 조회된 설정의 name/title 범위이며 프로젝트 전체 검색이나 별칭 판정은 구현하지 않았다.
이름 정규화는 중복 검사에만 적용한다. 원고 본문·공백·줄바꿈은 보존하고 평가에서 최초 입력과 대조한다.
현재 후보·프롬프트·결과 버전은 [제안 계약](proposal-contract.md)을 따른다.

## 실패와 재실행

실행 스냅샷은 모델 호출 전에 저장한다. 저장 후 재실행은 원문·관계를 다시 조회하지 않는다.
요청에는 모델·프롬프트·후보 스키마와 선택 정책 버전이 포함된다. 코드 배포 시에도 같은 버전의
규칙·프롬프트를 유지해야 재현할 수 있으며, 버전 문자열만으로 과거 코드가 복원되지는 않는다.

입력·후보 오류나 영구 모델 실패는 전체 FAILED다. 성공한 일부 제안을 남기지 않는다.
재시도 가능한 IO 오류는 호출자에게 전달한다. 결과 저장 전 실패는 이전 모델 호출도 반복할 수
있고, 결과 저장 후 실패는 저장 결과를 재사용한다. 발행 확인이나 완료 기록이 유실되면 같은
완료 이벤트가 중복 전달될 수 있다. 서비스 자체는 자동 재시도나 단일 분산 트랜잭션을 제공하지 않는다.

현재 JSON fake는 결과 변환과 왕복을 검증한다. S3 내구성·객체 잠금·DB 동시 접수·lease 회수·
Kafka offset·DLQ는 검증하지 않는다. 저장 불가 완료 실패 값은 전달 계층이 재시도 소진을
판정한 뒤 만들 수 있으며, 일반 저장 오류를 성공 완료로 발행하지 않는다.

## 검증과 다음 작업

[테스트 안내](../../tests/refresh/README.md)에 실행 명령과 사례를 정리했다.
순수 규칙과 실제 rules 서비스 회귀, JSON 고정 예시, 기존 진단 API와 로컬 HTTP 어댑터
통합 테스트를 함께 실행한다. refresh 테스트는 소켓을 차단하며 외부 서버 없이 동작한다.

호출 코드의 추가 이식 결과와 평가 방법은 [OpenAI 어댑터 이식 결과](openai-adapter.md)에 정리했다.
다음 작업은 같은 고정 자료를 통한 실제 LLM 평가, Content와 입력·결과 JSON Schema 확정,
S3 키·권한·만료, 추가 본문 API, 그래프 반영 기준, 영속 상태와 Kafka 어댑터 연결이다.
원본 Content 문서·확정 관계에 쓰는 포트는 이번 생성 경로에 없다.

rules 이식 시점의 검증 기록: 전체 pytest 190개 통과(그중 refresh 164개), Ruff lint·format 검사
통과, 문서의 로컬 파일 링크 검사 통과. 기존 Starlette/httpx 사용 중단 예정 경고 1개는
남아 있다. 이 결과는 배포 환경 검증이나 실제 LLM 평가 결과가 아니다.
