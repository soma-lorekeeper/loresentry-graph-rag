# OpenAI 호출 어댑터 마이그레이션 계획

> 상태: 구현 계획. 이번 문서 작업에서는 SDK 설치·어댑터 구현·유료 API 호출을 수행하지 않았다.

목표는 `ProposalModel`만 실제 OpenAI 호출로 교체해 고정 문서·관계 자료에 대한
원본 모델 응답과 rules 검증 결과를 확인하는 것이다. 나머지 다섯 IO 포트는 fake로
유지한다. FastAPI 라우트나 Kafka 소비자 없이 실행 스크립트로 검증한다.
전체 MVP의 실제 LLM 평가 단계이며, 운영 IO 연결 완료를 뜻하지 않는다.

기준은 [갱신안 우선 계획](refresh-mvp.md), [입출력 계약](proposal-contract.md),
[판단·IO 분리 원칙](../implementation/DECISION_AND_IO.md)이다.

## 1. 기존 코드에서 가져올 부분

원본 경로는 `lorekeeper-ai/`, 신규 대상 경로는 이 저장소 기준이다.
아래 신규 파일은 아직 존재하지 않는 구현 예정 위치다.

| 원본 | 가져올 내용 | 새 위치·변경 사항 |
|---|---|---|
| [openai_client.py](../../../lorekeeper-ai/src/common/openai_client.py)의 `_request`, `_is_quota_exhausted` | 연결·timeout·요청 제한·잔액 부족·서버 오류를 구분하는 원칙 | `app/adapters/llm.py`: `OpenAIProposalModel.generate`에서 내부 `RefreshFailure`로 변환. SDK 오류 구조에 맞춰 판별과 테스트를 재작성 |
| 같은 파일의 `create_response`, `create_completion` | 호출을 한 경계로 모으는 방식 | `ProposalModel`의 동기 인터페이스에 맞춰 동기 클라이언트 주입. API 두 종류를 모두 지원하지 않고 Responses 경로 하나로 시작 |
| [graphrag.py](../../../lorekeeper-ai/src/common/graphrag.py)의 구조화 출력·응답 변환 | 외부 응답을 내부 값으로 바꾸는 원칙 | `app/adapters/llm_schema.py`: 제안 응답 DTO·스키마와 `ModelCandidate` 변환. Neo4j `LLMBase`·`LLMResponse` 의존 제거 |
| [usage.py](../../../lorekeeper-ai/src/common/usage.py)의 `from_response`, `merge` | 토큰 사용량 추출, 캐시·추론 토큰의 중복 합산 방지 | 어댑터에서 Responses 사용량을 현재 `Usage`로 변환. 기존 Chat Completions 필드 파서를 그대로 복사하지 않음 |
| [llm_limit.py](../../../lorekeeper-ai/src/common/llm_limit.py), [admission.py](../../../lorekeeper-ai/src/common/admission.py) | 운영 동시성·한도 관리의 참고 자료 | 첫 연결에서 이식 보류. 요청별 호출 예산은 현재 서비스에서 유지 |

원본은 비동기 전역 클라이언트, 1,800초 timeout, 최대 5회 자체 재시도와 전역 동시성
제어가 결합돼 있다. 이를 복사하면 현재 호출 예산·동기 서비스와 운영 정책까지 묶인다.
따라서 작은 어댑터를 새로 작성하고 오류 분류·계량 원칙을 선별 이식한다.
LangGraph·LangChain·neo4j-graphrag는 이 연결에 추가하지 않는다.

## 2. 호출과 판단의 경계

실행 스크립트가 API 키·모델·timeout·출력 한도·SDK 클라이언트를 구성하고
`RefreshService(model=OpenAIProposalModel(...), ...)`로 주입한다.
모듈 import나 rules 실행에서 환경변수를 읽거나 클라이언트를 만들지 않는다.
클라이언트는 조립 지점에서 소유하고 성공·실패 모두 종료한다.

1. 기존 서비스가 스냅샷을 저장하고 대상별 `ModelInput`을 만든다.
2. 어댑터가 지원하는 `schema_version`, 모델 설정과 전송 한도를 확인한다.
3. OpenAI를 한 번 호출하고 완료 상태·거절·본문 존재·출력 구조를 확인한다.
4. 유효한 응답을 불변 `ModelCandidate`와 `Usage`로 변환한다.
5. 기존 rules가 프로젝트·대상·revision·근거·중복·충돌을 판정한다.
6. 기존 서비스가 전체 결과를 fake JSON 저장소에 저장하고 fake 완료 발행을 수행한다.

구조화 출력은 JSON 형식 제약이고 업무 검증의 대체가 아니다. 거절 응답과 출력 한도에
걸린 미완성 응답은 정상 후보로 처리하지 않는다. Responses와 Pydantic을 사용하는
구조화 응답 방식은 [OpenAI 공식 문서](https://developers.openai.com/api/docs/guides/structured-outputs)를 기준으로 구현한다.

현재 `ModelInput.prompt`에는 지시문과 자료가 함께 들어 있다. 연결 단계에서는
`ModelInput`에 별도 지시문 필드를 추가하고 `prompts.py`가 신뢰한 지시문과 문서 JSON을
분리해 반환하도록 보완한다. 어댑터가 문자열 구분자를 잘라 역할을 추측하지 않는다.
이 변경은 fake·직렬화·프롬프트 버전 회귀에 함께 반영하며 본문 내용은 명령으로 승격하지 않는다.

## 3. 응답 스키마와 설정

모델 응답은 `document_proposals`, `relation_proposals` 두 배열이다. 각 후보는 현재
[models.py](../../app/refresh/models.py)의 문서·관계·근거 값으로 변환한다.
빈 배열 두 개는 유효한 NO_CHANGE 후보지만, 누락 필드·파싱 실패·거절을 빈 배열로 바꾸지 않는다.
알 수 없는 필드와 잘못된 타입을 거절하고 숫자 문자열 등을 묵시적으로 보정하지 않는다.

모델 응답 스키마와 S3 결과 스키마는 별개다. 전자는 후보와 인라인 근거이고 후자는
요청 정보·출처·실행 기록·검증된 제안·근거 참조를 포함한다. S3 결과용
`result_from_payload`를 모델 응답 파서로 재사용하지 않는다.
현재 기본 후보 스키마 이름도 `refresh-result-v1`이므로 첫 연결에서 후보용 버전을
명확히 분리하고 프롬프트·고정 예시·스냅샷 호환성을 함께 갱신한다.

모델은 실행 시 명시하며 `unconfigured`이면 네트워크 호출 전에 실패한다.
SDK 버전은 구현 시 호환성을 확인해 정확한 버전으로 고정한다. 이번 계획에서 모델·가격을
확정하지 않는다. timeout과 최대 출력 토큰도 실행 설정으로 명시하고 평가 기록에 남긴다.
기존 문자 예산은 토큰 한도가 아니므로 선택한 모델의 입력·출력 한도와 별도로 대조한다.
초과 시 원문을 자르거나 자동 요약하지 않고 오류를 반환한다.

현재 `Usage`는 호출 수·입력 토큰·출력 토큰만 보존한다. 캐시·추론 세부 사용량과 API
응답 ID·실제 응답 모델·지연 시간은 평가 기록에 보존하되 입력·출력 총량에 다시 더하지 않는다.
재시도 시도들의 전체 비용은 현재 결과의 사용량으로 복원할 수 없으므로 시도별로 기록한다.

## 4. 실패·재시도 정책

첫 구현은 SDK 자동 재시도를 명시적으로 끄고 어댑터 안에서도 재시도하지 않는다.
`generate` 한 번에 원격 요청 최대 한 번으로 호출 수를 해석할 수 있게 한다.
오류의 `retryable`은 상위 실행자의 판단 자료이며 자동 재실행 명령이 아니다.
아래 코드는 새 어댑터의 구현 예정 오류 계약이다.

| 실패 | 예정 코드 | retryable |
|---|---|---|
| 연결 실패·timeout | `MODEL_CONNECTION_FAILED`, `MODEL_TIMEOUT` | true |
| 일시적 요청 제한·서버 오류 | `MODEL_RATE_LIMITED`, `MODEL_UNAVAILABLE` | true |
| 잔액·할당량 소진 | `MODEL_QUOTA_EXHAUSTED` | false |
| 인증·권한·잘못된 요청·미지원 설정 | `MODEL_AUTH_FAILED`, `MODEL_REQUEST_INVALID` | false |
| 거절·출력 미완성·출력 없음 | `MODEL_REFUSED`, `MODEL_INCOMPLETE`, `MODEL_EMPTY_RESPONSE` | false |
| JSON·스키마·DTO 변환 실패 | `MODEL_INVALID_RESPONSE` | false |

공급자 예외 전체를 서비스 오류 메시지로 복사하지 않고 필요한 분류만 전달한다.
예상하지 못한 코드 결함은 일반 실패로 숨기지 않는다. timeout은 원격 계산·과금이
없었다는 뜻이 아니며, 결과 저장 전 요청 재실행은 이전 대상 호출도 반복할 수 있다.
저장된 결과가 있으면 기존 복구 흐름대로 LLM을 다시 호출하지 않는다.

## 5. 구현 순서와 완료 기준

| 순서 | 작업 | 완료 기준 |
|---|---|---|
| 1 | 후보 스키마·엄격한 파서·사용량 매핑 | 문서만·관계만·둘 다·변경 없음, 누락·잘못된 타입 테스트 통과 |
| 2 | 동기 OpenAI 어댑터·설정 주입·오류 변환 | 가짜 클라이언트로 호출 인자·한 번 호출·timeout·거절·오류 분류 검증 |
| 3 | 실제 SDK와 로컬 HTTP 테스트 서버 연결 | 외부 자격증명 없이 요청·응답 직렬화, 자원 종료, 자동 재시도 없음 검증 |
| 4 | `scripts/evaluate_refresh.py`와 고정 평가 자료 | 실제 LLM만 교체해 원본 응답·파싱 후보·rules 결과·사용량 파일 생성 |
| 5 | 명시적 실제 모델 실행과 평가 기록 | 호출 성공과 업무 검증 통과, 제안 품질을 각각 기록. 기존 회귀·lint·format 통과 |

위 경로와 실행 옵션은 구현 예정이며 현재 실행 가능한 명령으로 안내하지 않는다.
평가 스크립트는 고정 입력과 fake 조립을 명시적으로 로드하고 실행하며, 운영 코드가
`tests`를 import하지 않도록 평가용 조립을 `scripts` 또는 별도 평가 자료 경계에 둔다.
실제 API 테스트는 기본 pytest/CI에서 실행하지 않는다. 현재 `tests/refresh/conftest.py`는
소켓을 차단하므로 HTTP 통합 테스트는 `tests/integration/`에 별도로 둔다.

원본 API 응답, DTO 파싱 결과, 최종 결과 JSON, 호출 설정·시도별 사용량·실패 코드와
평가 판정을 로컬 산출물로 남긴다. 산출물은 일반 로그나 Git에 자동 포함하지 않는다.
HTTP 성공, rules 수락, 사람이 기대한 변경의 정확성은 서로 다른 평가 항목이다.

고정 자료로 기대 수정의 누락·근거 없는 변경·기존 내용 손실·대상 오류·근거 위치를
확인한다. 특히 현재 모델은 코드 포인트 위치를 직접 반환하므로 긴 원문·반복 문장·이모지에서
오류가 나는지 별도 측정한다. 잘못된 위치를 임의로 고쳐 성공시키지 않는다.
품질 합격선은 첫 평가 결과와 기대 사례를 바탕으로 합의하며 API 연결만으로 품질 완료를 선언하지 않는다.

## 6. 기존 계획 준수 점검

2026-10-03 로컬 코드 대조 기준이다. 상세 구현은 [rules 이식 결과](rules-implementation.md)를 따른다.

| 계획 | 현재 확인 결과 | 남은 작업·차이 |
|---|---|---|
| 순수 판단과 IO 분리 | rules는 값만 사용, 서비스는 여섯 포트로 IO 조율 | 실제 모델 어댑터에도 같은 경계 적용 |
| 변경 20개·본문 5,000자, 다중 대상 | 선택·한도 검증과 대상별 호출 구현 | 실제 모델 토큰·출력 예산은 미구현 |
| 원문·revision 보존 | 실행 스냅샷 고정, 정확한 구간·인용 검증 | 요청 manifest는 내부 호환 때문에 선택적. 외부 진입점에서 필수 전달·fingerprint 검증 필요 |
| 기존 AI 선별 이식 | 구간·출처·문맥·후보 검증을 Content 계약으로 재작성 | KSS·Kiwi는 보류. 기존 few-shot 품질 평가와 세부 사용량 계량은 미완료 |
| 결과에 제안만 저장·저장 후 완료 전달 | 서비스 순서와 JSON 변환, fake 복구 검증 | S3·Kafka 실제 IO·영속 상태·lease는 미연결 |
| 서버 없는 규칙 테스트와 별도 IO 통합 | refresh 순수·fake 테스트 존재, 기존 진단 HTTP 통합 존재 | 진단 통합 테스트가 refresh 실제 IO 검증을 대신하지 않음 |

따라서 구현 구조와 fake 기반 단계는 계획에 맞지만 전체 MVP가 끝난 상태는 아니다.
원본 표의 파일명·부품을 모두 그대로 옮긴 것도 아니다. 이번 OpenAI 연결은 다음 미완료
단계를 수행하는 계획이며, 이후 Content·S3·관계 저장소·Kafka 통합은 별도로 진행한다.
