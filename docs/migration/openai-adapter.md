# OpenAI 호출 어댑터 이식 결과

현재 [S3 저장소 어댑터](../implementation/S3_ARTIFACTS.md)는 구현했고 실제 boto3의 로컬 HTTP로
검증했다. 기본 평가는 fake 저장소를 유지하며 운영 버킷·IAM·Content 입력 매핑과
Kafka 연결은 후속 작업이다.

동기 OpenAI 어댑터, 엄격한 후보 파서, 오류 분류, SDK 로컬 HTTP 통합과 평가 도구를
구현했다. 실제 생성 호출은 HTTP 401로 실패했고, 사용자 요청에 따라 이번 기능 평가는
대화 작성 응답으로 대체했다. [평가 기록](model-response-evaluation.md)에 확인 범위와
실행 결과를 분리했다. 실제 모델 품질과 나머지 다섯 운영 IO는 후속 검증이다.

기준은 [갱신안 우선 계획](refresh-mvp.md), [입출력 계약](proposal-contract.md),
[판단·IO 분리 원칙](../implementation/DECISION_AND_IO.md)이다.

## 기존 코드에서 가져온 부분

원본 경로는 `lorekeeper-ai/` 기준이다. 기존 파일 전체를 복사하지 않고 필요한 원칙을
현재 동기 포트·Content 용어와 계약에 맞춰 재작성했다.

| 원본 | 선별한 내용 | 실제 구현 |
|---|---|---|
| [openai_client.py](../../../lorekeeper-ai/src/common/openai_client.py)의 `_request`, `_is_quota_exhausted` | 연결·timeout·일시 제한과 할당량 소진 구분 | [llm.py](../../app/adapters/llm.py)의 `status_failure`, `generate` |
| 같은 파일의 `create_response`, `create_completion` | 호출 경계를 한곳에 모으기 | 동기 `OpenAIProposalModel`, Responses 경로 하나, 주입한 클라이언트 사용 |
| [graphrag.py](../../../lorekeeper-ai/src/common/graphrag.py)의 구조화 출력·응답 변환 | 외부 응답을 내부 값으로 변환 | [llm_schema.py](../../app/adapters/llm_schema.py)의 엄격 DTO, 불변 `ModelCandidate` 변환 |
| [usage.py](../../../lorekeeper-ai/src/common/usage.py)의 `from_response`, `merge` | 입력·출력 총량 추출, 캐시·추론 중복 합산 방지 | `response_usage`, 평가 시도별 원본 usage 기록 |
| [llm_limit.py](../../../lorekeeper-ai/src/common/llm_limit.py), [admission.py](../../../lorekeeper-ai/src/common/admission.py) | 운영 동시성·한도 제어 참고 | 이식 보류. 서비스의 대상별 호출 예산과 평가 전체 예산만 적용 |

원본의 비동기 전역 클라이언트·1,800초 timeout·최대 5회 자체 재시도는 가져오지 않았다.
LangGraph·LangChain·Neo4j LLM 클래스·KSS·Kiwi도 추가하지 않았다.

## 판단과 실행

`prompts.py`는 신뢰한 지시문을 `ModelInput.instructions`에, 문서 JSON을 `prompt`에
분리한다. 어댑터는 구분자를 잘라 역할을 추측하지 않는다. OpenAI 요청은 별도
`instructions`와 user 메시지, strict JSON schema, `store=False`, `truncation=disabled`를
사용한다. [공식 구조화 출력 계약](https://developers.openai.com/api/docs/guides/structured-outputs)을 따른다.

`generate`는 지원 설정·토큰 예산을 확인하고 한 번 호출한 뒤 완료 상태·거절·본문·스키마를
검증한다. DTO는 알 수 없는 필드·숫자 문자열·누락을 거절한다. 유효한 빈 배열 세 개만
NO_CHANGE 후보다. 실제 revision·허용 대상·인용 구간·중복·충돌 판단은 기존 rules가 맡는다.
S3 결과용 `result_from_payload`를 모델 파서로 재사용하지 않는다.

조립 지점인 [evaluate_refresh.py](../../scripts/evaluate_refresh.py)가 환경 설정과 클라이언트를
소유하고 성공·실패 모두 종료한다. 모듈 import나 rules는 키·현재 시간·네트워크에 접근하지 않는다.
나머지 IO는 [evaluation/fakes.py](../../evaluation/fakes.py)로 조립하며 운영 app은 tests를 import하지 않는다.
기존 테스트도 같은 fake를 재사용한다.

## 설정·버전·예산

| 항목 | 구현 값 또는 규칙 |
|---|---|
| SDK·DTO | `openai==3.24.0`, `pydantic==2.13.5` |
| 선택 모델 | `gpt-5.6-luna`. 현재 지원 한도 registry에 명시된 모델만 허용 |
| 프롬프트·후보 버전 | `refresh-prompt-v3`, `refresh-candidate-v2` |
| S3 결과 외곽 버전 | `refresh-result-v2`; 저장된 v1 읽기 호환 |
| 예시 timeout·출력 한도 | 120초, 8,192토큰. SDK 요청 timeout이며 전체 평가 시간 제한은 아님 |
| 예시 입력·문맥 예산 | 입력 120,000토큰, 입력+출력 128,192토큰 |
| 전체 평가 호출 상한 | 8회. 현재 정상 6사례는 대상별 7회 필요 |

선택 모델의 [공식 한도](https://developers.openai.com/api/docs/models/gpt-5.6-luna)는
입력 922,000·출력 128,000·문맥 1,050,000토큰이다(2026-10-03 확인).
설정 예산은 그 안으로 제한한다. 기존 문자 예산과 별도로 UTF-8 바이트 길이와 스키마,
4,096토큰 프레이밍 여유로 입력량을 보수적으로 추정한다. 정확한 tokenizer 계수는 아니므로
실제 한도에 들어가는 입력도 거절할 수 있다. 초과 원문을 자르거나 자동 요약하지 않는다.

이전 `refresh-v1` / `refresh-result-v1` 모델 설정의 실행 스냅샷은 새 계약으로
묵시적으로 바꾸지 않고 실제 어댑터에서 거절한다. 이미 저장된 결과는 그대로 복구한다.
새 계약으로 평가하려면 새 요청 ID·모델 설정을 사용한다. 재생 응답은 입력 fingerprint·
대상을 대조하고 알려진 v1 후보/v2 프롬프트 또는 현재 v2 후보/v3 프롬프트 조합만 허용한다.
과거 응답 재생은 신규 생성 기능의 모델 품질을 검증하지 않는다.

## 실행 모드와 산출물

[.env.example](../../.env.example)을 참고해 `.env`를 준비한다. `.env`는 Git 제외이며
키는 실제 `--live` 모드에서만 필요하다. 프로세스 환경변수가 파일보다 우선한다.

```bash
# 대화 작성 응답 재생: 원격 호출 없음
.venv/bin/python -m scripts.evaluate_refresh \
  --env-file .env.example --responses-dir evaluation/responses/assistant-v1 \
  --trial replay-2 --output .evaluation/replay-2 --repeat-saved

# 실제 OpenAI 호출: 유효한 키 필요, 유료 호출 가능
.venv/bin/python -m scripts.evaluate_refresh \
  --live --env-file .env --trial live-2 --output .evaluation/live-2
```

두 모드 옵션을 모두 생략하면 고정 기대 후보를 반환하는 오프라인 기준선이다.
`--case document`처럼 사례를 제한할 수 있다. 출력 경로가 이미 있으면 덮어쓰지 않는다.
`--repeat-saved`는 같은 프로세스의 fake 저장 결과 복구이며 프로세스 간 영속 재개는 아니다.

시도별 원본 API 응답 또는 재생 텍스트, 파싱 후보, 실제 응답 모델·요청 ID(있는 경우),
SDK·입력 버전·설정·지연·사용량을 로컬 JSON에 보존한다. 최종 사례 파일에는 스냅샷·결과와
rules 판정을 둔다. 파싱·모델·결과 저장 실패도 기록한다. 산출물은 Git 제외이며 일반 로그에는
요약만 출력한다. 인증 실패 원문에는 키 일부가 들어갈 수 있으므로 로컬 산출물도 공유하지 않는다.

CLI 종료 코드 0은 실행·rules 통과이며 의미상 품질 합격은 아니다. 누락·추가·기존 내용
보존·관계 비교를 별도로 읽는다. 캐시·추론 토큰은 총량에 다시 더하지 않는다.
usage가 없는 API 응답은 실패시키고 시도 기록의 사용량은 null로 남긴다. 현재 내부 결과의
`Usage`만으로 실패한 시도의 실제 비용을 복원할 수 없다.

## 실패와 재실행

SDK `max_retries=0`과 자체 재시도 없음으로 `generate`당 원격 요청을 최대 한 번으로 제한한다.
공급자 예외 원문은 서비스 오류 메시지에 복사하지 않는다. 예상하지 못한 코드 결함은 전파한다.

| 실패 | 내부 코드 | retryable |
|---|---|---|
| 연결·timeout | `MODEL_CONNECTION_FAILED`, `MODEL_TIMEOUT` | true |
| 일시 제한·서버 오류 | `MODEL_RATE_LIMITED`, `MODEL_UNAVAILABLE` | true |
| 인식한 할당량 소진 | `MODEL_QUOTA_EXHAUSTED` | false |
| 인증·권한·요청·미지원 설정 | `MODEL_AUTH_FAILED`, `MODEL_REQUEST_INVALID` | false |
| 거절·미완성·빈 출력 | `MODEL_REFUSED`, `MODEL_INCOMPLETE`, `MODEL_EMPTY_RESPONSE` | false |
| JSON·DTO·usage 오류 | `MODEL_INVALID_RESPONSE` | false |

`retryable`은 자동 재시도 명령이 아니다. 평가 도구는 공통 인증·할당량 실패 시 나머지 사례도
중단한다. timeout은 원격 계산·과금이 없었다는 뜻이 아니다. 결과 저장 전 실패의 재실행은
이미 성공한 대상까지 다시 호출할 수 있다. 결과가 저장됐다면 기존 서비스는 모델을 재호출하지 않는다.

## 검증과 남은 범위

[SDK HTTP 테스트](../../tests/integration/test_openai_http.py)는 실제 SDK를 로컬 서버에 연결해
요청 JSON·사용량·오류·timeout·무재시도·자원 종료를 검증한다. 기본 pytest/CI는 실제 키와
원격 OpenAI 호출을 사용하지 않는다. 전체 회귀·Ruff 통과와 대체 응답 평가를 완료했다.

실제 모델의 생성·품질·사용량, S3·Content·Neptune 관계 조회·영속 상태·Kafka와 FastAPI
갱신안 진입점은 이번 완료 범위 밖이다. [후속 계획](follow-up.md)을 따른다.

2026-10-03 당시 검증: 전체 pytest 267개 통과, Ruff lint·format 통과,
변경 문서의 로컬 링크 170개 확인(누락 없음). 기본 HTTP 제공 경로는 그대로 유지했다.

새 설정 생성·임시 ID 관계 제안은 [제안 계약](proposal-contract.md)의 생성 규칙을 따른다.
과거 v1 평가 응답은 명시적으로 v1 파서로 읽어 재생할 수 있으나, v2 생성 품질의 검증 근거가 아니다. 실제 OpenAI 생성 요청은 v2 후보·v3 프롬프트만 사용한다.
