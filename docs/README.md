# GraphRAG 문서 안내

> **책임:** 현재 구현 범위와 필요한 작업별 기준 문서를 안내한다.
>
> **확인할 때:** 구현된 기능과 아직 없는 기능을 구분하거나 읽을 문서를 고를 때.

현재 GraphRAG는 서비스 식별·프로세스 상태·Neptune 연결 확인 API를 제공한다.
그래프 저장, RAG/GraphRAG 검색, Content 변경 이벤트 소비는 아직 구현하지 않았다.
진단 유스케이스에는 순수 판단·애플리케이션 조율·HTTP 어댑터 분리를 적용했다.
내부 갱신안 생성은 기존 설정 본문 수정, 원고에서 발견한 새 설정 문서 생성, 관계 ADD를 제안한다.
원고 본문은 읽기 전용이며 최초 입력 보존을 평가한다. 생성 후보와 관계는 임시 ID로 연결하고
실제 문서·그래프 반영은 Content의 사용자 승인 이후 작업이다. 순수 rules·결과 JSON 변환은 fake IO로 검증했다.
OpenAI·S3 어댑터와 로컬 HTTP 검증도 구현했다. S3는 서비스 주입으로 선택하며
기본 평가 CLI는 fake 저장소를 유지한다. [모델 응답 대체 평가](migration/model-response-evaluation.md)는
대화 작성 응답으로 확인한 기능 흐름을 설명한다. 실제 원격 생성·품질과 운영 IO는 미검증이다.
문서는 로컬 코드·테스트·배포 설정을 설명하며, 운영 서버의 실시간 상태나 배포 성공을 뜻하지 않는다.

## 필요한 작업별 문서

Content와 사용하는 말부터 맞출 때는 [Content 기준 도메인 용어집](domain-glossary.md)을 읽는다. 문서·본문·관계·revision·제안·확정의 의미와 현재 계약 차이를 한곳에 정리했다.

| 필요한 작업·질문 | 기준 문서 | 담당 내용 |
|---|---|---|
| 서버는 어떤 구조이고 어디까지 구현됐는가? | [서버 구조](ARCHITECTURE.md) | 모듈 책임·의존 관계·실행 구성·미구현 범위 |
| 판단과 IO를 어떻게 분리하는가? | [판단·IO 분리 원칙](implementation/DECISION_AND_IO.md) | 순수 함수·작업 데이터·조율·주입·트랜잭션·재시도의 구현 기준 |
| 다른 서버가 GraphRAG를 어떻게 호출하는가? | [제공 API](API.md) | 현재 HTTP 경로·응답·오류와 진단 결과의 의미 |
| GraphRAG가 Neptune에 무엇을 요청하는가? | [호출 API](API_CALLS.md) | 주소·환경변수·타임아웃·응답 사용·실패 처리 |
| 현재 코드의 어디부터 읽어야 하는가? | [코드 안내](code-guide.md) | 요청 흐름·시작 구성·테스트와 배포 파일 위치 |
| S3 입력·스냅샷·결과를 어떻게 저장하는가? | [S3 저장소](implementation/S3_ARTIFACTS.md) | 설정·엄격 입력·해시·불변 저장·재사용·운영 검증 한계 |
| 변경 후 무엇을 검증해야 하는가? | [검증 계획](implementation/TEST_PLAN.md) | 기존 테스트 범위·추가 테스트·운영 확인 항목 |
| 기존 AI 기능을 어떤 순서로 이전하는가? | [마이그레이션 안내](migration/README.md) | 전체 이전 범위, 갱신안 생성 우선 계획, 후속 마이그레이션 |
| 문서·관계 제안은 어디에 저장하고 어떻게 전달하는가? | [갱신안 입력·결과 계약](migration/proposal-contract.md) | 변경 입력 한도, 수정·생성·연결 제안, 임시 ID와 결과 버전, S3/Kafka 전달 경계 |

## 현재 제안 계약과 검증

후보는 `refresh-candidate-v2`, 프롬프트는 `refresh-prompt-v3`, 결과는
`refresh-result-v2`다. 저장된 v1 결과와 과거 대화 응답은 읽기 호환하며, 과거 응답
재생을 새 설정 생성 품질 평가로 해석하지 않는다. 상세 규칙은 [제안 계약](migration/proposal-contract.md)을 따른다.

[새 인물 생성·연결 예시](examples/refresh/result-new-settings.json)와
`--suite new-settings` 오프라인 평가로 원고 한 편에서 생성하는 경우와 기존 인물에
연결하는 경우를 확인할 수 있다. 실행 방법과 검증 한계는 [검증 계획](implementation/TEST_PLAN.md)을 본다.

## 학습·설계 참고 자료

[Kafka 참고 자료](reference/README.md)는 개념·내부 처리 흐름·메시지 계약 초안·Outbox/Inbox 구현 예시를 안내한다. 현재 서버 구현과 구분해서 읽는다.

## 문서 책임과 상태

`API.md`는 제공 API, `API_CALLS.md`는 호출 API를 관리한다. 각 동작의 상세 설명은
담당 문서에 두고 다른 문서는 링크로 연결한다. 실행 방법은 [프로젝트 README](../README.md)를 따른다.
테스트 실행 결과와 배포 상태는 문서의 구현 설명과 구분한다.

서비스의 예정된 책임은 프로젝트 창작 자료의 그래프 구성과 AI Chat에 필요한 검색이다.
프로젝트 README에 명시된 문서 참조 기반 관계 구성과 프로젝트·접근 권한·휴지통 제외 조건은
운영 조회 어댑터에서 검증해야 할 요구사항이다. 내부 rules는 전달된 자료의 프로젝트·상태를
검증하지만 실제 사용자 인가나 Neptune 조회를 수행하지 않는다.

## 테스트용 소설 원문

[열세 번째 종 — 6화](examples/novel/the-thirteenth-bell/README.md)은 회차당 약 5,000자의
가상 소설이다. 수정된 5화와 새 6화를 [fake S3 입력](examples/refresh/novel/README.md)으로
연결했으며 `--suite novel`로 갱신안 흐름을 실행한다.
