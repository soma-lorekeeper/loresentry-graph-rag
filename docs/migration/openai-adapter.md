# OpenAI 호출 어댑터 이식 결과

현재 [S3 저장소 어댑터](../implementation/S3_ARTIFACTS.md)는 구현했고 실제 boto3의 로컬 HTTP로
검증했다. 기본 평가는 fake 저장소를 유지하며 운영 버킷·IAM·Content 입력 매핑과
Kafka 연결은 후속 작업이다.

동기 OpenAI 어댑터, 엄격한 후보 파서, 오류 분류, SDK 로컬 HTTP 통합과 평가 도구를
구현했다. 초기 생성 호출은 HTTP 401로 실패하여 대화 작성 응답으로 기능 흐름을 확인했다.
[당시 평가 기록](model-response-evaluation.md)은 그 범위를 보존한다. 2026-10-11에는
키 교체 뒤 실제 호출과 가상 소설 평가를 실행했다. [품질 개선·검증 기록](refresh-quality.md)에
새 결과와 한계를 정리한다. 운영 IO와 일반화된 모델 품질은 별도 검증 대상이다.

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
검증한다. DTO는 알 수 없는 필드·숫자 문자열·누락을 거절한다. 유효한 빈 배열 네 개만
NO_CHANGE 후보다. 모델은 서버가 제공한 출처 마커만 반환하고 어댑터가 해당 문맥의
전체 원문 인용·구간을 복원한다. 한 사실의 `source`는 `{"marker":"C0001"}`만 허용한다.
모델이 사실 근거를 발췌·합성·재작성하거나 위치를 덧붙이지 않는다. 역할 표면과 행동 표현은
선택한 마커의 quote에 있어야 하며 사실 근거 크기는 요청의 `chunk_chars`(기본 1,000자)를
따른다. statement는 의미를 보존해 요약할 수 있고 500자, action·surface는 각각 200자다.
실제 revision·허용 대상·인용 구간·중복·충돌 판단은 rules가 맡는다.
S3 결과용 `result_from_payload`를 모델 파서로 재사용하지 않는다.

조립 지점인 [evaluate_refresh.py](../../scripts/evaluate_refresh.py)가 환경 설정과 클라이언트를
소유하고 성공·실패 모두 종료한다. 모듈 import나 rules는 키·현재 시간·네트워크에 접근하지 않는다.
나머지 IO는 [evaluation/fakes.py](../../evaluation/fakes.py)로 조립하며 운영 app은 tests를 import하지 않는다.
기존 테스트도 같은 fake를 재사용한다.

## 설정·버전·예산

| 항목 | 구현 값 또는 규칙 |
|---|---|
| SDK·DTO | `openai==3.24.0`, `pydantic==2.13.5` |
| 선택 모델 | 기본 설정은 `gpt-6-luna`. `gpt-5.6-luna`도 지원하며 한도 registry에 명시된 모델만 허용 |
| 프롬프트·후보 버전 | 기본 요청은 v13·v7 두 단계; 작성 confidence는 v8. 기존 비교는 v11·v12 및 v5·v6 유지 |
| 청크·겹침 | `chunk_chars` 기본 1,000자, `chunk_overlap_chars` 기본 0; `0 <= 겹침 < 청크 크기`인 정수만 허용하며 bool은 거절 |
| S3 결과 외곽 버전 | `refresh-result-v3`; 저장된 v1/v2 읽기 호환 |
| 예시 timeout·출력 한도 | 240초, 32,768토큰. SDK 요청 timeout이며 전체 평가 시간 제한은 아님 |
| 예시 입력·문맥 예산 | 입력 240,000토큰, 입력+출력 272,768토큰 |
| 전체 평가 호출 상한 | 기본 예시·CLI fallback 18회. 기본 6사례는 최초 7회, 확장 소설은 신규 탐색 1회 + 기존 수정 8회. 독립 semantic 6사례는 최초 16회 필요 |

[prompt_versions.py](../../app/refresh/prompt_versions.py)가 실제 생성에 허용한 v11·v12를
정의한다. v11 지시문과 기본 선택은 유지한다. v12는 허용 대상의 사실을 먼저 구조화하고
source 원문과 대조한 뒤 본문·관계를 작성하도록 보강한다. 새로 추가하거나 수정하는
주장에서 행위자·수령인·소유자·부정·조건·범위·시점·확실성을 보존하고, 검증을 통과하려고
같은 장면의 다른 행동으로 바꾸지 않도록 지시한다. 그대로 보존한 기존 본문은 모두
재추출하지 않는다. 후보 스키마와 서버의 엄격 검증 조건은 바뀌지 않는다.

겹침 0은 과거 청크와 요청·문맥·실행 설정의 직렬화를 유지한다. 양수이면 이전 청크 끝의
완전한 문장을 다음 청크에 포함하되, 겹침 한도와 청크 크기에 들어오는 경우에만 적용한다.
넣을 여유가 없으면 겹침을 생략한다. 문장 경계는 문장부호·줄바꿈으로 나누며, 지나치게 긴
문장은 기존처럼 크기 상한에서 분할한다. 원문 위치·전체 덮기·전진을 보존하고, 겹친 원문도
문맥 예산에 포함한다. `--chunk-chars`, `--chunk-overlap-chars`로 평가 조건을 지정한다.

현행 후보의 구체적 검증 오류는 서비스가 호출 범위별 최대 두 번 보완한다.
보완은 원래 자료와 실패 후보·진단을 함께 제공하는 `refresh-validation-repair-v2` 경로이며
SDK 자체 재시도와 별개다. 특정 사실의 행동·역할 표현 오류에서는 해당 사실을 참조하지
않는 제안·근거를 이전 후보에서 보존하고 전체를 다시 검증한다. 파싱·전역 오류는 전체
후보를 보완한다. 요청의 `max_model_calls`와 평가 `--max-calls`에 추가 호출도
포함된다. 긴 원고에서 보완 여유를 두려면 `--max-calls 36`, 독립 의미 6사례는
`--max-calls 64`를 사용할 수 있다. 최초 호출만 허용하는 9회·16회 설정에서는 보완 여유가
소진되면 실패한다. 보완 입력에도 문맥·어댑터 토큰 예산을 적용한다. 실제 결과와 한계는
[품질 검증 기록](refresh-quality.md#검증-오류-보완-repair-v1)을 참고한다.

기본 모델 `gpt-6-luna`의 [공식 한도](https://developers.openai.com/api/docs/models/gpt-6-luna)는
입력 922,000·출력 128,000·문맥 1,050,000토큰이다(2026-10-11 확인).
설정 예산은 그 안으로 제한한다. 기존 문자 예산과 별도로 UTF-8 바이트 길이와 스키마,
4,096토큰 프레이밍 여유로 입력량을 보수적으로 추정한다. 정확한 tokenizer 계수는 아니므로
실제 한도에 들어가는 입력도 거절할 수 있다. 초과 원문을 자르거나 자동 요약하지 않는다.

`gpt-6-luna`로 모델 설정을 변경하면서 Responses API·구조화 출력과 기존 토큰·시간
예산은 처음 유지했다. 이후 긴 원고 반복 비교의 출력 한도 실패를 확인하고 예시·로컬
출력 예산을 16,384, 문맥 예산을 136,384토큰으로 늘렸다. 별도 v12 실행에서 13회 호출·
보완 4회 성공 후 PROPOSED를 확인했으며 프롬프트 효과와 별개인 예산 진단이다.
기본 v11의 같은 예산 단건은 응답 잘림 없이 완료됐지만 잘못된 출처 선택을 보완하지
못해 최종 FAILED였다. 출력 여유만으로 출처·의미 품질을 해결한 것은 아니다.
`reasoning.effort`는 기존처럼 생략하며 공식 기본값은 `medium`이다.
Responses API에는 seed 설정을 제공하지 않으므로 seed로 같은 답변을 보장하지 않는다.
2026-10-11 `gpt-6-luna` 실제 smoke는 v11의 `semantic-record-and-consent` 한 사례에서
4회 호출·보완 1회 성공 뒤 `PROPOSED`를 반환했다. 실제 응답의 model도 `gpt-6-luna`다.
의미 판정은 `review_required`이고 원문 함의·자유 본문은 `human_review_pending`이다.
[품질 평가 기록](refresh-quality.md)의 이전 `gpt-5.6-luna` 실행과 구분한다.
이후 의미 6사례 A/B 각 2회는 모두 PROPOSED였지만 v12는 최초 검증 실패·보완이
2회 필요했고 등록 구조화 불일치도 더 많아 기본 v11을 유지했다. 본문 패턴 통과가
전체 의미 합격을 뜻하지 않으며, 일반 품질의 승자는 정하지 않았다.

이전 `refresh-v1` / `refresh-result-v1` 모델 설정의 실행 스냅샷은 새 계약으로
묵시적으로 바꾸지 않고 실제 어댑터에서 거절한다. 이미 저장된 결과는 그대로 복구한다.
새 계약으로 평가하려면 새 요청 ID·모델 설정을 사용한다. 재생 응답은 입력 fingerprint·
대상을 대조하고 알려진 과거 후보/프롬프트 조합(v1/v2, v2/v3, v3/v4, v3/v5, v3/v6,
v3/v7, v4/v8, v4/v9, v4/v10) 또는 입력과 일치하는 현재 v5/v11·v5/v12 조합만 허용한다.
v4의 `source:{marker,excerpt}`는 마커 원문에서 유일하게 일치하는 500자 이하 발췌를
복원하는 과거 읽기 계약이다. 현행 v5의 생성·검증에 발췌 필드를 허용하는 예외는 아니다.
관계쌍 소유권이 없던 v1/v2·v2/v3·v3/v4 응답은 전체 후보를 기존 규칙으로 검증한 뒤 소유 호출에 분배하고,
원본 후보·재생 텍스트와 `legacy_relation_scope_adapted`를 기록한다. 이 재생은 현재 프롬프트의
생성 품질을 검증하지 않는다.

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

# v12와 문장 겹침을 명시한 단일 평가
.venv/bin/python -m scripts.evaluate_refresh \
  --live --env-file .env --suite semantic --max-calls 32 \
  --prompt-version refresh-prompt-v12 --chunk-chars 1000 --chunk-overlap-chars 200 \
  --trial semantic-v12 --output .evaluation/semantic-v12
```

두 모드 옵션을 모두 생략하면 고정 기대 후보를 반환하는 오프라인 기준선이다.
`--case document`처럼 사례를 제한할 수 있다. 출력 경로가 이미 있으면 덮어쓰지 않는다.
`--repeat-saved`는 같은 프로세스의 fake 저장 결과 복구이며 프로세스 간 영속 재개는 아니다.

시도별 원본 API 응답 또는 재생 텍스트, 파싱 후보, 실제 응답 모델·요청 ID(있는 경우),
SDK·입력 버전·설정·지연·사용량을 로컬 JSON에 보존한다. 최종 사례 파일에는 스냅샷·결과와
rules 판정을 둔다. 파싱·모델·결과 저장 실패도 기록한다. 산출물은 Git 제외이며 일반 로그에는
요약만 출력한다. 인증 실패 원문에는 키 일부가 들어갈 수 있으므로 로컬 산출물도 공유하지 않는다.

CLI와 Markdown은 같은 판정을 사용한다. 종료 코드 0은 등록된 자동 검사 통과,
1은 실행·규칙·필수 후보·내용·구조화 사실 기준 실패, 2는 미등록 추가 설정·관계·사실·
표현의 검토 필요다. 0이어도 원문이 사실을 의미적으로 뒷받침하는지와 자유 본문의 정확성은
사람 검토 대상으로 남는다.
필수·허용·금지 후보를 구분하고 신규 설정은 임시 ID 대신 유형·정규화 이름으로 비교한다.
본문 어구·기존 내용 보존·관계쌍 검사는 의미상 품질 합격을 보증하지 않는다.
캐시·추론 토큰은 총량에 다시 더하지 않는다.
usage가 없는 API 응답은 실패시키고 시도 기록의 사용량은 null로 남긴다. 현재 내부 결과의
`Usage`만으로 실패한 시도의 실제 비용을 복원할 수 없다.

### 프롬프트와 청크 겹침 반복 비교

[evaluate_refresh_ab.py](../../scripts/evaluate_refresh_ab.py)는 동일 자료·등록 기준·모델·
예산에서 A(v11, 겹침 0)와 B(v12, 겹침 0)를 각각 독립 요청으로 실행한다. 각 조건은
`--repeats`로 최소 2회 반복하고 순서를 번갈아 배치한다. 선택적인 C는 v12에 양수 겹침을
추가한다. 입력 자료와 평가 기준 fingerprint도 대조하여 서로 다른 조건의 결과를 섞지 않는다.

```bash
# 실제 비교: 2조건 × 2반복 × 자식 전체 상한 64회 = 최대 256회 예약
.venv/bin/python -m scripts.evaluate_refresh_ab \
  --live --env-file .env --suite semantic --repeats 2 --max-calls 64 \
  --max-total-calls 256 --trial semantic-ab --output .evaluation/semantic-ab

# 이미 저장된 실험만 비교: 모델 호출 없음, 새 출력 디렉터리 사용
.venv/bin/python -m scripts.evaluate_refresh_ab \
  --reports-dir .evaluation/semantic-ab --output .evaluation/semantic-ab-summary
```

실제 호출 모드는 `--max-total-calls`가 필수이며 보완까지 포함한 각 자식 `--max-calls`의
합계를 실행 전에 예약한다. 위 예시에 `--include-overlap --overlap-chars 200`을 추가하면
3조건이므로 전체 상한도 최소 384회로 늘려야 한다. 겹침은 청크 크기보다 작아야 하며
`--chunk-chars`를 지정하면 모든 조건에 같은 크기를 적용한다. 실제 호출 수는 예약보다
작을 수 있다. 인증·할당량 소진 시 나머지 실행은 중단한다.

`experiment.json`에 실행 조건을, `comparison.json`·`comparison.md`에 최초 모델·검증 실패,
보완 성공, 최종 제안·필수 누락, 등록 의미 기준, 토큰·시간의 요약을 저장한다. 비교 요약에는
원문·제안 본문·공급자 응답·오류를 복사하지 않고 파일 권한은 0600으로 둔다. 개별 실행의
JSON과 `report.md`에는 본문이 있으므로 별도로 보호한다. 실패한 결과의 누락은 미평가이며,
실패까지의 일부 시간과 완료 실행 시간을 직접 비교하지 않는다. 사용량 미확인은 0이 아니다.
자동 요약은 `inconclusive`를 유지하며, 등록 검사 통과나 `PROPOSED`를 사람의 의미 승인으로
취급하지 않는다. 오프라인 고정 기대값 실행도 가능하지만 독립 모델 생성의 품질 근거가 아니다.
각 시도의 자동 평가 판정과 조건별 평가·실행 실패도 표시한다. 실제 실행과 저장 결과
재집계 모두 실행·등록 검사 실패나 비교 불완전은 종료 코드 1, 검토가 남은 완결 비교는 2다.

### Markdown으로 결과 검토

평가 CLI는 출력 디렉터리에 `report.md`를 자동 생성한다. VS Code에서 파일을 열고
Markdown 미리보기를 사용하면 사례별 판정, 기존 문서의 변경 diff,
신규 설정·관계 제안, 시도별 응답 시간과 토큰 사용량을 함께 볼 수 있다.
제안별 근거 인용·ID·본문 위치는 보고서에 표시하지 않고 JSON 원본에 보존한다.
기존 문서의 제안 본문은 diff와 중복해서 표시하지 않는다. 이전 값이 없어 diff를
만들 수 없을 때만 제안 본문을 표시하며, 신규 설정의 본문은 그대로 보여준다.
입력과 기존 설정의 본문·속성도 펼쳐 볼 수 있어 제안이 없는 사례의 누락을 검토할 수 있다.
보고서는 저장된 JSON을 읽으며 추가 모델 호출은 하지 않는다.

기존 실행 결과에도 보고서를 붙이거나 다시 만들 수 있다. Linux 호스트의 저장소
루트에서 실행하며 아래 경로를 실제 평가 결과 디렉터리로 바꾼다.

```bash
.venv/bin/python -m scripts.report_refresh .evaluation/live-2
```

이 명령은 JSON 원본을 보존하고 해당 디렉터리의 `report.md`를 교체한다.
보고서는 실제 API·고정 응답 재생·고정 기대값을 구분하고, 저장된 자동 검사 중
하나라도 불일치하면 표시한다. CLI도 같은 검사 항목으로 종료 코드를 결정하며
의미상 품질의 합격 판정은 아니다. 응답 시간은 모델 어댑터 시도
시간이며 전체 refresh 시간은 아니다. 사용량이 없으면 0 대신 미확인으로 표시한다.
공급자 원본 응답·오류는 보고서에 복사하지 않지만 제안과 입력 본문을 포함하므로
보고서도 JSON과 같은 0600 권한으로 저장한다. 운영 IO는 여전히 fake 평가 범위다.

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

가상 소설의 실제 모델 생성·사용량은 [품질 검증 기록](refresh-quality.md)을 따른다.
S3·Content·Neptune 관계 조회·영속 상태·Kafka와 FastAPI 갱신안 진입점의 운영 연동,
다른 원고·모델에 대한 품질 일반화는 후속 검증이다. [후속 계획](follow-up.md)을 따른다.

2026-10-03 당시 검증: 전체 pytest 267개 통과, Ruff lint·format 통과,
변경 문서의 로컬 링크 170개 확인(누락 없음). 기본 HTTP 제공 경로는 그대로 유지했다.

새 설정 생성·임시 ID 관계 제안은 [제안 계약](proposal-contract.md)의 생성 규칙을 따른다.
과거 v1/v2 평가 응답은 명시적 읽기 호환으로 재생할 수 있으나 현재 생성 품질의 검증 근거가 아니다.
실제 OpenAI 생성 요청은 v5 후보에 기본 v11 또는 명시 선택한 v12 프롬프트를 사용한다. 개별 사실의 source에서
전체 마커 원문을 복원하며 관계 설명·근거는 fact_ids에서 조립한다.
과거 응답과 사실이 없는 고정 기대값의 오프라인 실행은 명시적인 이전 계약 검증이며,
현재 v5의 사실 필수 검증을 우회하는 live 모드는 제공하지 않는다.

## 계획·문단 작성 두 단계 평가

실제 Refresh의 새 기본 요청은 v13·v7이다. 별도 평가의 과거 기대 fixture와 기본 비교는
한 단계 계약에 고정하며 `--two-stage`는 실제 모델 또는 저장된 두 단계 응답을 요구한다.
`--single-call` 없이도 두 단계가 모든 대상을 함께 처리한다. 입력·출력 예산은 호출마다
적용하고, --max-calls는 계획·작성·보완 전체에 공유한다. 보완은 두 단계 합쳐 한 번이다.

```bash
.venv/bin/python -m scripts.evaluate_refresh --live --env-file .env \
  --model gpt-6-luna --suite novel-settings --two-stage \
  --prompt-version refresh-prompt-v13 --confidence --observe-sources --repeat-saved \
  --timeout 300 --max-input-tokens 120000 --max-output-tokens 32768 \
  --context-window 152768 --max-calls 3 \
  --trial novel-two-stage --output .evaluation/novel-two-stage
```

계획 응답은 `refresh-plan-v1`, 작성 응답은 v7 또는 v8이다. 호출별 기록에 PLAN·WRITE 단계와
원본 응답·사용량을 보존한다. 최종 report.md는 계획, 본문 작성, 보완을 구분한다.
계획이 지목한 기존 문단을 서버가 조립해 최종 v3 결과의 본문 전체 교체 제안으로 반환하므로,
Content의 실제 저장·승인 계약을 바꾸지는 않는다.
