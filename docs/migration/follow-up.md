# 갱신안 MVP 이후의 마이그레이션

현재 [S3 저장소 어댑터](../implementation/S3_ARTIFACTS.md)는 구현했고 실제 boto3의 로컬 HTTP로
검증했다. 기본 평가는 fake 저장소를 유지하며 운영 버킷·IAM·Content 입력 매핑과
Kafka 연결은 후속 작업이다.

> **책임:** 첫 갱신안 생성 목표 이후에 확장할 저장·동기화·검색·연동 기능을 정한다.
>
> **전제:** [갱신안 생성 우선 계획](refresh-mvp.md)의 내부 입출력과 판단·IO 경계를 유지한다.
>
> **상태:** 후속 계획. 저장소 선택이나 보류 기능의 제품 도입을 확정한 문서가 아니다.

후속 목표는 MVP에서 fake로 검증한 관계·문서 조회를 실제 저장소에 연결하고, 벡터·전문 검색까지 확장하는 것이다.
기존 설정 수정·새 설정 생성·임시 후보 관계 제안은 내부 구현했다. Content 연동에서는
승인한 후보의 실제 문서 ID 발급, body_text의 에디터 JSON 변환, 후보 ID 치환과 관계 확정을
구현해야 한다. 프로젝트 전체 설정 중복·별칭·동명이인 확인도 조회된 문맥만으로 보장되지 않는다.
현재 제안 규칙은 [제안 계약](proposal-contract.md)을 따른다.
이미 검증한 생성 유스케이스에 자료 공급·검색 어댑터를 연결하며, Kafka 구성을 다시 별도 선행 프로젝트로 시작하지 않는다.
기능별 원본 파일과 재사용 수준은 [전체 이전 범위](overview.md)에서 관리한다.

## 1. 후속 범위와 순서

| 단계 | 이전·신규 구현 | 선행 결정 | 완료 기준 |
|---|---|---|---|
| 1. 저장·검색 실증 | Neptune 관계 저장·조회, 청크·임베딩·전문 검색 어댑터 실증 | 그래프 모델, 검색 저장소, 문서·revision_no·청크 식별자 | 작은 실제 데이터에서 프로젝트 범위 검색과 원문 근거 연결 확인 |
| 2. 문서 동기화 | Content 파일·프로젝트 변경을 그래프·색인에 반영 | 파일 이벤트 순서 값, 삭제·복원·역순 처리, 그래프와 검색 저장소의 반영 상태 | 중복·역순·재시작 이후 원본 상태로 수렴. 휴지통·삭제 자료 제외 |
| 3. 검색 제공 | 원문 검색·명시적 관계 확장·근거 중복 제거 API | 입력·응답, 접근 검증, 최신성 목표, 검색 품질 기준 | 문서 ID·revision_no·근거 위치로 결과 추적, 다른 프로젝트 자료 비노출 |
| 4. 갱신안 자료 검색 확장 | MVP의 관계·Content 조회에 벡터·전문 검색 후보를 추가 | 관련 자료 선택 기준, 입력 예산, 검색 지연·자료 부재 시 정책 | 기존 평가 자료의 생성 품질 유지와 새 자동 선택 사례 검증 |
| 5. AI Chat 검색 연동 | 기존 에이전트의 검색 도구가 GraphRAG를 호출하도록 변경 | AI Chat과의 검색 계약·timeout·오류·근거 표현 | 대화 흐름에서 허용된 자료의 근거를 받아 사용. 생성·스트리밍 책임은 AI Chat 유지 |

저장·검색 실증은 이 후속 단계의 첫 작업이며 갱신안 MVP의 시작 조건이 아니다.
MVP의 관계·Content 조회 포트와 fake는 이미 구현할 범위다. 후속 단계에서 해당 로직을 다시 만들지 않는다.
실제 관계 조회·동기화가 먼저 준비되면 MVP 통합 단계에서 연결할 수 있다. 운영 자동 선택에는 이 연결과 검증이 필요하다.
직접 입력 방식은 테스트·명시적 자료 지정 경로로 유지한다.

## 1.1. 후속 기능의 출처와 활용 위치

대상 경로는 구현 예정 위치다. MVP에서 만든 `app/text/`의 청킹·근거 값과
`app/adapters/llm.py`의 호출 경계를 재사용하며, 기능별로 복사본을 만들지 않는다.

| 단계·기능 | 원본 코드·이전 단위 | 대상 위치·소비자 | 재사용·교체 및 검증 |
|---|---|---|---|
| 1. 청크·임베딩 저장 | [chunk.py](../../../lorekeeper-ai/src/repository/neo4j/chunk.py)의 `write_chunk_layer`, [graphrag.py](../../../lorekeeper-ai/src/common/graphrag.py)의 `MeteredEmbedder` | `app/indexing/service.py`, `app/adapters/search_index.py`, `app/adapters/embedding.py`: 문서 동기화가 생성한 청크를 검색용으로 저장 | 청킹은 MVP 부품 공유. `Chapter`·Cypher·Neo4j 인덱스 생성은 교체. 문서·revision_no별 교체와 삭제, 생성/검색 임베딩 일치 검증 |
| 1·3. 원문 검색·관계 확장 | [retrieval.py](../../../lorekeeper-ai/src/repository/neo4j/retrieval.py)의 `build_hybrid_cypher_retriever`, `_build_chunk_query`, 결과 formatter | `app/retrieval/service.py`, `app/adapters/search_index.py`, `app/adapters/graph_store.py`: 원문 후보 검색 후 허용된 관계 확장 | 하이브리드 검색 전략과 근거 반환 방식 재사용. `HybridCypherRetriever`·Cypher는 교체. 명시적 Content 참조를 확장하며 추출 그래프를 확정 관계로 사용하지 않음 |
| 3. 근거 응답 | [evidence.py](../../../lorekeeper-ai/src/repository/neo4j/evidence.py)의 `link_evidence`, [docstore.py](../../../lorekeeper-ai/src/service/detect/docstore.py)의 근거 중복 제거 | `app/text/evidence.py`, `app/retrieval/models.py`: AI Chat과 갱신안 자료 공급에 문서·revision_no·원문 위치 반환 | 근거 연결 의미를 재사용하고 DB 관계 쓰기는 교체. MVP 근거 모델을 공유해 검색 결과에서 원문을 추적 |
| 2. 동기화 조율 | [indexing_service.py](../../../lorekeeper-ai/src/service/index/indexing_service.py)의 `indexing`, [index/job_service.py](../../../lorekeeper-ai/src/service/index/job_service.py)의 작업 실행 역할 | `app/indexing/service.py`: Content 변경마다 그래프·검색 투영을 갱신. 전달 계층 소비자가 호출 | 기존 회차 누적 파이프라인을 대체. 명시적 참조 투영, revision_no 교체·삭제·역순 방지는 신규 구현. 저장마다 LLM 전체 추출하지 않음 |
| 3·5. 검색 인터페이스 | [retrieval_tools.py](../../../lorekeeper-ai/src/service/retrieval_tools.py)의 `build_openai_tools`, `format_tool_result`, [chat/tools.py](../../../lorekeeper-ai/src/service/chat/tools.py) | `app/retrieval/models.py`, `app/retrieval/service.py`: 내부 검색 계약. AI Chat의 도구 어댑터가 합의된 전송 계약으로 호출 | 검색 요청·근거 필드 의미 참고. OpenAI tool schema·도구 선택 루프는 AI Chat 소유. GraphRAG에서 기존 원고 SQL 조회 제거 |
| 4. 갱신안 문맥 검색 확장 | [context_service.py](../../../lorekeeper-ai/src/service/index/context_service.py)의 문맥 조립, 원문 검색 | `app/refresh/service.py`의 자료 공급 경계와 `app/retrieval/service.py`: 자동 선택된 자료를 MVP 입력 스냅샷 형식으로 변환 | 전역 그래프 덤프 대신 접근 가능한 관련 문서를 조회. 순수 문맥 조립은 MVP 구현 공유. 변경 후 생성 품질 회귀 검증 |
| 전체. 프로젝트 격리 | [tenant.py](../../../lorekeeper-ai/src/common/tenant.py), [kg_scope.py](../../../lorekeeper-ai/src/service/kg_scope.py) | 검색·색인 서비스 및 모든 저장소 어댑터: 검증된 범위를 필수 입력으로 전달 | UUID 기반 범위로 교체. 후보 검색뿐 아니라 관계 확장·근거 반환·삭제에도 적용하고 다른 프로젝트 비노출 검증 |

### 조건을 충족한 뒤 가져올 부품

| 기능·원본 | 사용하려는 위치 | 도입 조건과 제외할 구현 |
|---|---|---|
| 사실 검색: [fact.py](../../../lorekeeper-ai/src/repository/neo4j/fact.py)의 `ensure_fact_layer`, [retrieval.py](../../../lorekeeper-ai/src/repository/neo4j/retrieval.py)의 `build_fact_search_retriever`, `EntitySearchRetriever` | 검색 저장 어댑터와 검색 서비스에서 사실·엔티티 후보를 원문 근거와 함께 반환 | 추출 사실 저장을 채택한 경우. Neo4j 라벨·인덱스·쿼리는 교체하고 확정 참조와 분리 |
| 후보 매칭: [resolver.py](../../../lorekeeper-ai/src/service/index/resolver.py)의 `NormalizedExactMatchResolver`, `CombiningFuzzyResolver`, `OpenAIEmbeddingResolver` | `app/refresh/matching.py`: 기존 설정 문서 후보를 순위화. 임베딩 IO는 어댑터에 위임 | 자동 대상 선택이 필요한 경우. DB 노드 병합·description 자동 덮어쓰기는 제외. 동명이인·애매한 후보 테스트 |
| 요약: [context_service.py](../../../lorekeeper-ai/src/service/index/context_service.py)의 `summarize_episode`, `update_global_summary` | `app/context/service.py`: 문맥 예산을 위한 문서 요약 생성과 캐시 조율 | 입력 예산 때문에 필요할 때. Chapter/Story 쓰기를 문서 revision_no별 요약으로 교체하고 수정·삭제 시 무효화 |
| 설명 통합: [resolver.py](../../../lorekeeper-ai/src/service/index/resolver.py)의 `collapse_merged_descriptions` | 갱신안의 설명 변경 제안 또는 파생 요약 | 제품상 필요가 확인된 경우만 프롬프트를 선별. 사용자 문서 자동 병합의 대체 수단으로 쓰지 않음 |

원본 `test_index_api.py`의 처리 실패 사례는 동기화 서비스 테스트로 바꾸고,
`test_metered_embedder.py`는 임베딩 어댑터 테스트로 옮긴다. Neo4j 응답이나 기존 HTTP 경로를
새 구현의 정답으로 고정하지 않는다. 검색·동기화 통합 테스트에는 새 저장소의 실제 읽기·쓰기,
수정·삭제·역순 이벤트와 근거 연결 검증을 추가한다.

## 2. 그래프·검색 모델의 경계

Content의 명시적 문서 참조를 확정 관계로 반영한다.
원본 AI의 인물·사건·상태 추출 그래프를 사용자 확정 관계에 자동으로 합치지 않는다.
추출 사실을 검색용으로 저장할 필요가 확인되면 확정 데이터와 구분한 모델·근거·수명 정책을 먼저 정한다.

Neo4j의 쿼리·인덱스·retriever를 그대로 가져오지 않고 저장소별 어댑터로 교체한다.
임베딩 생성과 검색의 모델·차원을 맞추고, 문서 수정 시 이전 revision_no의 청크·요약·임베딩을 어떻게 폐기하거나 교체할지 정한다.
서로 다른 저장소에 반영되는 경우 하나의 트랜잭션처럼 설명하지 않고 재시도·재구축·최신성 경계를 둔다.

## 3. 동기화와 복구

병렬 Kafka 작업이 제공하는 수신·발행 구조를 사용하되 파일 변경 소비자의 처리 규칙은 이 기능에서 구현한다.
이벤트 계약은 [메시지 계약 초안](../reference/message-contract.md), Neptune 반영은
[Inbox 설계](../../../docs/GRAPH_INBOX_PATTERN.md)를 참고한다.

중복 이벤트와 순서가 뒤바뀐 서로 다른 이벤트를 구분하고, 오래된 FileChanged가 삭제된 프로젝트나 파일을 다시 만들지 않게 한다.
기존 요청 ID·갱신 기준 revision_no와 파일 동기화의 순서 값을 혼용하지 않는다.
보존 기간을 넘긴 장애에는 이벤트 재생만으로 복구할 수 있는지 확인하고 Content 원본 기반 재구축 경로를 설계한다.

갱신안의 중복 요청·계산 결과 복구·완료 재발행은 [우선 계획](refresh-mvp.md)의 책임이다.
이 후속 문서는 장기 그래프·검색 투영의 복구를 추가하며 MVP의 실패 처리를 뒤로 미루지 않는다.

## 4. 기존 데이터 이전

원본 데이터의 보존 필요·양·식별자 대응은 아직 확인하지 않았다.
코드를 옮기는 것과 기존 Neo4j 데이터를 옮기는 것을 별도 작업으로 관리한다.

| 선택 | 적합한 조건 | 준비할 내용 |
|---|---|---|
| Content 원본에서 재색인 | 원본이 남아 있고 파생 데이터 재생성이 허용됨 | 대상 문서·revision_no 고정, 비용·시간 평가, 새 색인 검증 |
| 기존 그래프 변환 | 보존해야 하는 파생 사실·근거·평가 데이터가 있음 | 정수 작품·회차와 UUID 문서 매핑, 데이터 변환, 근거·관계 검증 |

전환 전에 기존·신규 결과를 비교할 자료와 복구 방법을 마련한다.
새 색인 검증과 전환이 끝나기 전에 기존 데이터를 삭제하거나 원본 DB의 소유권을 GraphRAG로 옮기지 않는다.

## 5. 별도 결정이 필요한 확장

- **사실 검색·엔티티 매칭:** 저장할 추출 사실과 품질·근거 기준을 정한 경우 확장한다. 이름 유사도로 사용자 문서를 자동 병합하지 않는다.
- **누적 요약:** 갱신안이나 검색의 문맥 예산에 실제로 필요한 경우 도입한다. 원본 문서 수정·삭제에 따른 무효화와 재생성 규칙을 포함한다.
- **설정 오류 탐지:** 주장 추출·검색·모순 판정의 제품 범위를 별도로 정한다. 첫 MVP나 검색 이전이 완료됐다는 이유로 자동 포함하지 않는다.
- **다중 Pod 호출 한도:** 처리량 확대 시 프로세스 단위 제한을 전체 한도로 오해하지 않도록 공유 모델 예산·접수 제한을 조정한다.

AI Chat의 대화·스트리밍 구현과 Content의 사용자 확정·저장은 각 서비스의 별도 작업이다.
GraphRAG의 검색 연동 완료를 전체 채팅·편집 기능 완료로 보고하지 않는다.

## 6. 완료 검증

실제 저장소 어댑터로 프로젝트 격리·관계 조회·색인 갱신·삭제·중복·역순·재시작을 검증한다.
검색 품질은 고정 자료의 기대 근거와 비교하고, 갱신안 자료 공급을 바꾼 뒤에는 MVP 평가를 다시 수행한다.
기존 데이터 이전을 선택했다면 문서·근거 ID 대응과 결과 비교, 전환·복구 절차도 검증한다.
실제 Kafka·Neptune·검색 저장소 연결 범위와 대체 구현으로만 확인한 범위를 구분해 기록한다.

## OpenAI 실제 생성 평가 후속

OpenAI 어댑터와 SDK 로컬 HTTP 검증은 구현했다. 실제 호출은 401 invalid_api_key로
실패했고, 사용자가 이번 검증을 [대화 작성 응답 재생](model-response-evaluation.md)으로
대체했다. 유효한 키·모델 접근 권한을 준비한 뒤 같은 고정 자료로 실제 생성 응답·사용량을
확인해야 한다. 긴 원문 근거 위치, 누락·추가·기존 설정 손실과 관계 품질을 별도로 평가한다.
재생 결과나 HTTP 통합 테스트를 이 검증의 성공으로 계산하지 않는다.

현재 UTF-8 바이트 기반의 보수적 입력 추정은 정확한 토큰 계수가 아니다. 대규모 입력에서
과도한 거절이 관측되면 모델별 tokenizer와 프레이밍 계수를 검증해 개선한다.
새 공급자 할당량·잔액·지출 제한 오류 코드도 영구 실패 분류에 추가 검증해야 한다.
