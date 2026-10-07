# 기존 AI 기능의 GraphRAG 이전 범위

> **책임:** `lorekeeper-ai`의 기능 중 GraphRAG로 옮길 대상과 변경 범위, 다른 서비스에 둘 기능을 구분한다.
>
> **확인할 때:** 이전할 코드를 선택하거나 구현 작업의 범위와 선행 결정을 정할 때.
>
> **상태:** 코드 조사에 따른 마이그레이션 제안. 구현 완료나 모든 설계 결정의 확정을 의미하지 않는다.

GraphRAG에는 문서 청킹·검색·근거 구성과 설정 문서 갱신안 생성에 필요한 추출·요약 로직을 옮긴다.
채팅 에이전트는 AI Chat의 책임으로 분리하고, Neo4j 저장·검색 및 기존 백엔드 DB 직접 접근은 새 구조에 맞게 교체한다.
설정 오류 탐지는 현재 제품 요구사항의 필수 기능으로 확인되지 않아 별도 결정 전까지 이전 범위에서 보류한다.

## 1. 조사 기준과 문서 경계

| 대상 | 확인 기준 |
|---|---|
| 원본 | `soma-lorekeeper/lorekeeper-ai`, 로컬 커밋 `553bd3e1e7c3a4e391be842a3cec09463a98d79b` |
| 대상 | `soma-lorekeeper/loresentry-graph-rag`, 로컬 커밋 `60d27a1aa0a3a6b0aa6c7497f2786d53cc841b89` |
| 조사일 | 2026-10-02 |
| 현재 구현 | [서버 구조](../ARCHITECTURE.md). 진단 API와 내부 설정 수정·새 설정 생성·관계 ADD 제안은 구현. 실제 그래프 저장·검색·이벤트 소비는 미연결 |
| 목표 기능 | [핵심 기능 요구사항](../../../docs/CORE_FEATURE_REQUIREMENTS.md)의 AI 챗·그래프·설정 문서 갱신안 |
| 서비스 경계 | [공통 프로젝트 문서](../../../docs/LORE_SENTRY_PROJECT_CONTEXT.md)의 AI Chat·GraphRAG·Content 책임 |
| 이벤트 연동 | [메시지 계약 초안](../reference/message-contract.md). 미결정 내용을 포함하며 운영 반영 여부와 구분 |

이 문서는 이전 범위와 차이를 관리한다. 이벤트 payload·topic·보존 기간은 메시지 계약에,
현재 HTTP API는 [API.md](../API.md)에 둔다. 원본 커밋의 기능을 현재 서버에 이미 존재하는 기능으로 기록하지 않는다.

이전 코드의 구조는 [판단·IO 분리 원칙](../implementation/DECISION_AND_IO.md)을 따른다.
청킹·정규화·근거 구성의 계산과 정책은 순수 함수로, LLM·저장소·메시지 접근은 실행 구현으로 나눈다.
서비스가 중간 IO 결과에 따라 다음 판단을 조율하며, 기능별 저장·재시도 경계는 별도로 결정한다.

## 2. 먼저 구분할 데이터와 책임

### 확정된 파일 관계와 AI 추출 결과

원본은 원고에서 인물·사건·상태를 추출해 지식 그래프에 직접 누적한다.
현재 제품에서 사용자에게 보이는 파일 관계는 Content에 명시적으로 저장된 참조가 기준이다.
따라서 원본의 추출 결과를 그대로 확정 관계로 쓰는 방식은 이전하지 않는다.

- **확정 데이터:** Content의 문서·속성·명시적 참조가 원본이다. GraphRAG는 이를 조회·검색용으로 반영한다.
- **AI 갱신안:** 원고를 근거로 여러 설정 문서의 내용 변경과 새로운 관계를 제안하여 S3에 보관하고, 결과 위치를 Kafka로 전달한다. 상세 범위는 [갱신안 계약](proposal-contract.md)을 따른다. 사용자가 확정하기 전에는 원본 문서나 확정 관계를 바꾸지 않는다.
- **검색용 파생 데이터:** 청크·임베딩·요약과 필요 시 추출 사실을 사용한다. 추출 사실을 별도 저장할지는 미결정이며, 사용자가 확정한 사실과 구분해야 한다.

### 서버별 담당

| 서버 | 이전 후 담당할 범위 |
|---|---|
| Content | 문서·revision_no·참조의 원본, 갱신안 검토·확정과 저장, 원본 변경 이벤트 |
| GraphRAG | 문서의 그래프·검색 투영, 관련 문서와 근거 검색, 갱신안 추출 계산과 결과 반환 |
| AI Chat | 대화·에이전트 실행, 검색 도구 호출, 응답 생성·스트리밍·중단·재시도 |
| Gateway 및 각 서비스 | 검증된 사용자 컨텍스트 전달과 서비스별 접근 범위 확인. 원본의 테넌트 필터만으로 인가 완료를 가정하지 않음 |

## 3. 이전할 기능과 변경 수준

‘정책 재사용’은 알고리즘·프롬프트·검증 아이디어를 가져온다는 뜻이다.
원본에 Neo4j 라이브러리 타입과 쿼리가 섞여 있으므로 파일 단위 무수정 복사를 뜻하지 않는다.

| 기능 | 원본 코드 | 판단 | 대상에서 필요한 변경 |
|---|---|---|---|
| 한국어 문장 분리·청킹 | [splitters.py](../../../lorekeeper-ai/src/service/index/splitters.py) | 정책 재사용 | KSS·Kiwi 분리와 원문 위치 보존을 평가하고, 청크 식별자를 프로젝트·문서·revision_no 기준으로 변경. Neo4j TextSplitter 타입 의존 분리 |
| 청크 저장·임베딩 | [chunk.py](../../../lorekeeper-ai/src/repository/neo4j/chunk.py) | 기능 이전, 저장 구현 교체 | `Chapter`·회차 번호 기반 저장을 문서 기반으로 변경. 임베딩 생성·검색 모델을 일치시키고 수정·삭제 시 옛 revision_no 처리 추가 |
| 원문·사실·엔티티 검색 | [retrieval.py](../../../lorekeeper-ai/src/repository/neo4j/retrieval.py), [retrieval_tools.py](../../../lorekeeper-ai/src/service/retrieval_tools.py) | 검색 전략 재사용 | 원문 하이브리드 검색과 식별자 기반 관계 조회부터 설계. `HybridCypherRetriever`·Neo4j 쿼리는 교체. 사실 검색은 추출 사실 저장 여부 결정 후 포함 |
| 근거 연결·중복 제거 | [evidence.py](../../../lorekeeper-ai/src/repository/neo4j/evidence.py), [docstore.py](../../../lorekeeper-ai/src/service/detect/docstore.py) | 정책 재사용 | 청크·사실·엔티티를 중복 없이 묶는 방식을 활용. 회차·Neo4j element ID 대신 문서 ID·revision_no·원문 위치를 반환 |
| 문맥 구성·요약 | [context_service.py](../../../lorekeeper-ai/src/service/index/context_service.py) | 부분 이전 | 관련 자료와 요약을 추출 문맥으로 사용. 회차 순서와 누적 Story 요약 전제를 문서 변경 모델에 맞게 수정하고, 근거가 바뀐 요약의 무효화 규칙 추가 |
| 구조화된 정보 추출 | [extractor.py](../../../lorekeeper-ai/src/service/index/extractor.py), [extraction_examples.py](../../../lorekeeper-ai/src/service/index/extraction_examples.py) | 갱신안 생성으로 전환 | 한국어 원고 추출 프롬프트·사례를 참고하되, 목표 출력은 설정 문서의 변경 제안과 근거. 추출 후 확정 그래프에 즉시 쓰는 단계 제거 |
| 추출 스키마·파이프라인 | [graph_schema.py](../../../lorekeeper-ai/src/service/index/graph_schema.py), [extraction_pipeline.py](../../../lorekeeper-ai/src/service/index/extraction_pipeline.py) | 의미 참고, 조립 재설계 | Character·Event·CharacterState 모델을 그대로 확정하지 않음. Content의 설정 문서·속성 모델에 맞춰 출력 스키마와 검증 구성 |
| 엔티티 후보 매칭·설명 병합 | [resolver.py](../../../lorekeeper-ai/src/service/index/resolver.py) | 조건부 이전 | 이름 정규화·유사도·설명 병합을 기존 설정 문서와의 매칭 후보 생성에 활용. 이름이 비슷하다는 이유로 사용자의 파일·확정 관계를 자동 병합하지 않음 |
| LLM 호출·사용량 집계 | [openai_client.py](../../../lorekeeper-ai/src/common/openai_client.py), [usage.py](../../../lorekeeper-ai/src/common/usage.py), [graphrag.py](../../../lorekeeper-ai/src/common/graphrag.py) | 호출 제어 정책 재사용 | 재시도·오류 처리·사용량 계량을 추출·임베딩 경로에 적용. `MeteredLLM`·`MeteredEmbedder`의 Neo4j 기반 인터페이스는 분리 |
| 처리량 제한·접수 제어 | [llm_limit.py](../../../lorekeeper-ai/src/common/llm_limit.py), [admission.py](../../../lorekeeper-ai/src/common/admission.py) | 정책 재사용, 적용 범위 재설계 | 프로세스 내 제한을 여러 Pod와 AI Chat을 포함한 공용 모델 한도로 오해하지 않도록 제한 단위를 결정. HTTP 429와 Kafka 작업 대기를 구분 |
| 작업 접수·진행·완료 | [index/job_service.py](../../../lorekeeper-ai/src/service/index/job_service.py), [detect/job_service.py](../../../lorekeeper-ai/src/service/detect/job_service.py) | 역할 이전, 실행 모델 교체 | 메모리 dict·Queue·create_task를 그대로 이전하지 않음. 요청 ID, 프로젝트별 동시성, 영속 상태, 재시작 복구와 완료 이벤트 재발행 설계 |
| 프로젝트 격리 | [tenant.py](../../../lorekeeper-ai/src/common/tenant.py), [kg_scope.py](../../../lorekeeper-ai/src/service/kg_scope.py) | 원칙 재사용 | 정수 `userId × workId`를 현행 UUID·프로젝트 범위로 변경. 검색·관계 확장·근거 반환·작업 상태 조회 모두 동일한 범위를 적용 |

원본 [indexing_service.py](../../../lorekeeper-ai/src/service/index/indexing_service.py)는 위 작업을 한 회차 처리로 묶는다.
대상에서는 **저장된 문서의 검색 색인 갱신**과 **사용자가 요청한 AI 갱신안 생성**을 별도 흐름으로 나눈다.
문서를 저장할 때마다 전체 LLM 추출·설명 병합·누적 요약을 실행하는 방식으로 복제하지 않는다.

## 4. 다른 서비스로 분리하거나 보류할 기능

| 기능 | 원본 | 처리 |
|---|---|---|
| 대화 에이전트·답변·제목 생성 | [chat/agent.py](../../../lorekeeper-ai/src/service/chat/agent.py), [chat_controller.py](../../../lorekeeper-ai/src/controller/chat_controller.py) | AI Chat 이전 대상. GraphRAG에는 대화 이력이나 답변 스트리밍 책임을 넣지 않음 |
| 에이전트 검색 도구 | [chat/tools.py](../../../lorekeeper-ai/src/service/chat/tools.py) | 도구 선택·호출 루프는 AI Chat, 검색 구현은 GraphRAG로 분리. 원고 직접 SQL 조회는 Content 연동으로 교체 |
| 설정 오류 탐지 전체 | [detect/job_service.py](../../../lorekeeper-ai/src/service/detect/job_service.py), [extract_service.py](../../../lorekeeper-ai/src/service/detect/extract_service.py), [judge_service.py](../../../lorekeeper-ai/src/service/detect/judge_service.py) | 보류 제안. 주장 추출·근거 검색·모순 판정은 갱신안 생성과 다른 제품 기능. 근거 구성 같은 공통 부품만 선별 재사용 |
| 기존 백엔드 DB 접근 | [postgres/detection.py](../../../lorekeeper-ai/src/repository/postgres/detection.py), [chat/tools.py](../../../lorekeeper-ai/src/service/chat/tools.py) | 이전하지 않음. `works`·`episodes` 조회와 `detection_jobs`·`detection_findings` 쓰기를 새 서비스에 복사하지 않음 |
| Neo4j 저장·인덱스·병합 구현 | [repository/neo4j](../../../lorekeeper-ai/src/repository/neo4j), [resolver.py](../../../lorekeeper-ai/src/service/index/resolver.py) | Neptune 및 선택한 검색 저장소의 어댑터로 교체. 드라이버 주소만 바꾸는 이전은 대상이 아님 |
| 기존 HTTP 계약·배포 | [controller](../../../lorekeeper-ai/src/controller), [원본 README](../../../lorekeeper-ai/readme.md) | `/api/index`·`/api/detect`·`/api/chat`, camelCase·정수 ID, EC2 동거 배포를 그대로 이전하지 않음. 현재 서버의 실행·배포 구성을 유지하며 새 계약 설계 |

설정 오류 탐지를 보류하는 것은 기능 삭제 결정이 아니다. 제품 범위에 포함하기로 결정되면
입출력·결과 저장 주체·평가 기준을 별도로 정하고 이 문서의 범위를 갱신한다.

## 5. 원본에 없어서 새로 구현해야 할 연결부

| 연결부 | 필요한 내용 |
|---|---|
| Content 변경 수신 | 메시지 계약에 따라 파일·프로젝트 변경을 소비하고, 중복·오래된 이벤트·삭제 후 늦은 이벤트를 처리 |
| 명시적 참조 그래프 | 사용자 확정 참조를 Neptune에 반영. 프로젝트·휴지통·삭제 조건이 검색과 관계 확장 모두에 적용되도록 구성 |
| 문서 조회 계약 | Content에서 접근 범위가 확인된 문서·revision_no를 가져오는 방법과 오류·삭제·revision_no 변경 처리 정의 |
| 검색 응답 계약 | AI Chat이 사용할 검색 요청과 문서 ID·revision_no·근거 위치를 포함한 응답 정의. Neo4j 라이브러리 타입을 외부에 노출하지 않음 |
| 최신화 입출력 | `GraphRefreshRequested` 입력과 S3 자료로 갱신안을 계산하고 결과·실패를 `GraphRefreshCompleted`로 반환하는 흐름. 상세 필드는 메시지 계약 참조 |
| 작업 복구 | 계산 중 재시작, 결과 저장 후 완료 이벤트 발행 실패, 입력 객체 만료, 중복 요청에 대한 재개·재발행·종료 처리 |

[Neptune Inbox 설계](../../../docs/GRAPH_INBOX_PATTERN.md)는 짧은 그래프 반영의 참고 자료다.
장시간 LLM 작업, S3 쓰기, Kafka 완료 이벤트까지 하나의 Neptune 트랜잭션으로 묶였다고 간주하지 않는다.
각 단계의 저장·재시도 경계를 별도로 정해야 한다.

## 6. 결정 사항의 적용 시점

| 구분 | 결정할 내용 | 담당 계획 |
|---|---|---|
| 우선 MVP | 갱신 대상 유형·필드, 입력 자료와 기준 revision_no, 결과 스키마, 근거·품질 판정 기준 | [갱신안 생성 우선 계획](refresh-mvp.md) |
| 병렬 연동 | 요청·결과 전달 계약, 작업 ID, 상태 저장·중복·재시도·offset commit·완료 재발행의 책임 경계 | [MVP의 Kafka 연동 경계](refresh-mvp.md#3-kafka-병렬-작업과의-경계) |
| 후속 검색 | Neptune 그래프 모델, 벡터·전문 검색 저장소, 추출 사실 저장 여부, 문서 동기화 순서 값 | [후속 마이그레이션](follow-up.md) |
| 후속 데이터 | 기존 데이터 보존 필요·양 확인, ID 변환 또는 재인덱싱 선택 | [후속 마이그레이션](follow-up.md) |

저장·검색 방식을 먼저 확정해야 갱신안 생성을 시작할 수 있는 것은 아니다.
우선 MVP는 S3 입력과 명시적 관계 조회·Content 추가 조회를 포트로 연결해 fake로 검증한다.
실제 관계 조회·동기화는 준비된 시점에 통합 검증하며, 벡터·전문 검색과 범용 검색 API는 후속 범위다.
메시지 계약의 `version`·`outbox_id` 메모는 파일 동기화 설계 시 해결하며, 갱신 요청의 기준 revision_no와 혼동하지 않는다.

## 7. 실행 계획과 우선순위

첫 목표는 **갱신안 생성과 품질 검증**이다. Kafka는 별도 병렬 작업으로 진행하며,
GraphRAG 기능 개발은 요청 전달·결과 발행 인터페이스가 제공된다는 전제로 진행한다.
이는 Kafka가 실제로 구현·배포·검증됐다는 상태 보고가 아니다.

1. [갱신안 생성 우선 계획](refresh-mvp.md)에 따라 입출력 계약, 생성 흐름, 대체 IO 테스트와 실제 LLM 평가를 수행한다.
2. 병렬 Kafka 작업이 준비되면 같은 계약으로 실제 전달·중복·실패·복구를 통합 검증한다.
3. [후속 마이그레이션](follow-up.md)에 따라 저장·검색 실증, 문서 동기화, 검색 제공과 AI Chat 연동을 확장한다.

전체 기능의 이전 여부와 서비스 경계는 이 문서가 관리한다. 함수·클래스별 선별 내용, 대상 모듈과 활용 지점, 이전 검증은
[MVP 코드 매핑](refresh-mvp.md#11-원본-기능을-가져와-사용할-위치)과
[후속 코드 매핑](follow-up.md#11-후속-기능의-출처와-활용-위치)에서 관리한다.

## 8. 이전할 테스트와 평가 기준

- [test_splitters.py](../../../lorekeeper-ai/tests/test_splitters.py): 문장 경계·청크·원문 위치 검증을 새 문서 입력에 맞춰 이전한다.
- [test_graph_schema.py](../../../lorekeeper-ai/tests/test_graph_schema.py): 원본 그래프 스키마 검증은 목표 갱신안 스키마에 맞게 다시 작성한다.
- [test_openai_gateway.py](../../../lorekeeper-ai/tests/test_openai_gateway.py), [test_llm_limit.py](../../../lorekeeper-ai/tests/test_llm_limit.py), [test_metered_llm.py](../../../lorekeeper-ai/tests/test_metered_llm.py), [test_metered_embedder.py](../../../lorekeeper-ai/tests/test_metered_embedder.py): 호출·제한·계량 규칙을 분리한 인터페이스에 맞춰 이전한다.
- [test_index_api.py](../../../lorekeeper-ai/tests/test_index_api.py): 처리 순서·중복·실패 시나리오는 참고하되, 기존 HTTP 경로와 메모리 큐를 정답으로 고정하지 않는다.
- [test_detect_pipeline.py](../../../lorekeeper-ai/tests/test_detect_pipeline.py), [test_harness_parity.py](../../../lorekeeper-ai/tests/test_harness_parity.py): 재사용하는 근거 구성 부분을 선별한다. 모순 탐지 성능을 갱신안 품질의 지표로 대신하지 않는다.

새 검증에는 프로젝트 격리·인가 실패·삭제 자료 제외·revision_no 추적·중복/역순 이벤트·작업 복구를 포함한다.
검색과 갱신안 품질은 동일한 입력 자료·근거 기대값으로 평가하고, 모델·프롬프트·청킹·검색 설정을 기록한다.
원본 README의 과거 평가 수치를 새 저장소·모델·제품 기능의 성능으로 승계하지 않는다.
이 문서 작성에서는 애플리케이션 테스트·LLM 호출·운영 데이터 이전을 실행하지 않았다.
