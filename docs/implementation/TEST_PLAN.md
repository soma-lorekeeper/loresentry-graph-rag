# GraphRAG 검증 계획

> **책임:** 기존 테스트가 검증하는 범위와 추가로 필요한 코드·운영 검증을 구분한다.
>
> **확인할 때:** 변경 후 검증할 시나리오를 고르거나 테스트 통과의 의미를 판단할 때.
>
> **관련 기준:** 응답은 [제공 API](../API.md), 호출 설정은 [호출 API](../API_CALLS.md), 실행 방법은 [프로젝트 README](../../README.md#test)를 본다.

검증은 순수 판단, 대체 구현으로 연결한 앱, 실제 HTTP IO를 나눈다.
로컬 통합 테스트는 임시 포트의 HTTP 서버를 사용하며 외부 네트워크·AWS 자격 증명·실제 Neptune은 필요하지 않다.

## 자동화 테스트

| 파일 | 확인 기준 |
|---|---|
| [test_graph.py](../../tests/test_graph.py) | 같은 입력은 같은 결과, 입력 불변, 누락은 unknown, 잘못된 구조·null·비문자열은 거절. FastAPI나 DB 없이 실행 |
| [test_main.py](../../tests/test_main.py) | 기본 라우트는 IO 없음, 성공 JSON 유지, 알려진 실패는 503, 코드 오류는 전파, 앱별 주입 의존성 독립 |
| [test_neptune_integration.py](../../tests/test_neptune_integration.py) | 실제 어댑터와 라우트 연결, GET /status 경로, JSON 정상·비정상, HTTP 실패, 읽기 타임아웃, 자동 재시도 없음, 반복 읽기 |

```bash
# 프로젝트의 Python 3.12 가상환경 사용
.venv/bin/python -m pytest tests/test_graph.py -q
.venv/bin/python -m pytest -m 'not integration' -q
.venv/bin/python -m pytest -m integration -q
```

전체 검증은 `.venv/bin/python -m pytest -q`로 실행한다.
[CI/CD](../../.github/workflows/ci-cd.yaml)의 기존 `pytest` 명령도 모든 테스트를 수집한다.
순수 테스트에는 서버가 없으며, 통합 테스트 실행 환경에는 루프백 소켓 사용 권한이 필요하다.

## 검증 한계

위 세 파일은 읽기 전용 운영 진단 검증이다. 갱신안 rules의 검증은 아래 별도 범위를 따른다.
DB 쓰기 트랜잭션·Kafka 전달 보장을 검증한 것은 아니다.
통합 테스트는 HTTP 어댑터의 실제 전송을 검증하지만 Neptune의 TLS·IAM 인증·그래프 연산은 검증하지 않는다.
라이브 서비스에 도달하지 않았다는 사실과 테스트용 HTTP 서버에서 IO가 실행됐다는 사실을 구분한다.

## 운영 환경에서 별도 확인할 항목

| 항목 | 확인 기준 |
|---|---|
| 실제 배포 | 배포 이미지가 의도한 커밋인지, Pod 준비 상태와 실제 probe 경로가 무엇인지 확인 |
| Neptune 접근 | 실행 환경의 DNS·네트워크·TLS·포트와 실제 `/health/db` 응답 확인 |
| 인증 요구 | 대상 Neptune의 인증 요구와 현재 서명 없는 HTTP 요청의 호환 여부 확인 |
| 접근 제한 | 진단 API와 FastAPI 문서 경로가 의도한 네트워크 범위에서만 접근 가능한지 확인 |

이 항목들은 이 저장소의 테스트로 검증되지 않는다. 실제 운영 상태는 배포·인프라 증거와
실행 결과를 별도로 확인해야 한다.

## 업무 기능 구현 시 추가할 범위

그래프 저장·검색·이벤트 소비가 추가되면 프로젝트 격리, 접근 권한, 휴지통 제외,
명시적 문서 참조만의 관계 생성, 이벤트 중복·순서·재처리와 AI Chat 연동을 검증해야 한다.
현재 갱신안 내부 판단은 구현했으며 실제 그래프 저장·검색·이벤트 소비는 미구현이다.


## 갱신안 rules와 fake IO

[Refresh 테스트 안내](../../tests/refresh/README.md)에 파일별 검증을 정리했다.
기본 RefreshRules로 입력 한도·manifest·프로젝트·상태·근거·후보 충돌과 다중 대상 호출을
검증한다. 소켓을 차단하고 JSON fake 저장소로 출처·결과 왕복·복구를 확인한다.
기존 ScriptedRules 테스트는 실행 순서·오류 전달 검증으로 별도 유지한다.
다양한 원고의 모델 추출 품질·운영 S3 내구성·Content 인가·Neptune 반영·Kafka 전달 보장은
후속 검증이다.

## 모델 호출과 응답 대체

`tests/refresh/test_llm_schema.py`·`test_llm.py`는 파서·엄격 타입·오류 분류와 호출 예산을,
`tests/integration/test_openai_http.py`는 실제 SDK의 로컬 HTTP 요청·무재시도·종료를 확인한다.
`test_evaluation_*.py`·`test_response_replay.py`는 시도 기록·실패·사용량 불명·응답 재생·
버전 불일치·잘못된 원문 위치 거절을 검증한다. 기본 pytest는 원격 API를 호출하지 않는다.

대화 작성 응답 6사례의 실행 결과는 [평가 기록](../migration/model-response-evaluation.md)에 있다.
실제 OpenAI 인증 성공·초기 생성 결과·사용량은
[품질 개선·검증 기록](../migration/refresh-quality.md)에 있다. 그 기록의 v7 실행과 당시
469개 pytest 통과를 현재 구조화 사실 계약의 검증 결과로 취급하지 않는다.

현재 live 어댑터는 `refresh-prompt-v11` / `refresh-candidate-v5`만 생성하며 엄격한
응답 스키마와 facts 검증을 적용한다. v1/v2 전체 근거, v3 마커 후보를 읽는 파서와
v4/p8·p9·p10의 `source:{marker,excerpt}` 후보 재생은 호환성 검사다.
재생은 입력 fingerprint·대상·허용된 버전 조합을
대조하며 원본 후보 버전으로 검증한다. 사실이 없는 과거 고정 후보도 이전 계약의 흐름
확인에만 사용한다. 이 경우 최종 결과에 실제 후보 버전을 남기고 현재 v5 사실 검증을
통과했다고 보고하지 않는다. live의 필수 facts·fact_ids 검증을 완화하는 경로는 아니다.

## 구조화 사실과 의미 검사

[test_semantic_relations.py](../../tests/refresh/test_semantic_relations.py)는 한 사실에
한 출처 마커를 선택하고 서버가 전체 quote·revision·구간을 복원하는 계약,
역할·행동 표현의 quote 포함, statement의 역할 표면 보존,
명시 제목·이름·별칭과 행위자·수령인 문서 참조의 일치를 검사한다. 모든 제안은 알려진
비어 있지 않은 `fact_ids`를 참조해야 한다. 관계의 설명·근거는 참조 사실로만 만들고
다른 주장을 덧붙이지 않는다. 알 수 없는 참조, 잘못된 역할 식별·구간·분류는 거절하며
유효한 미사용 중간 사실은 최종 결과에 남기지 않는다. 호출 간 사실 ID 정규화와
공유 참조, 집계 결과·저장 후 복원도 회귀 범위다.
현행 `source`는 마커만 허용하며 모델의 발췌·인용문·숫자 위치 추가는 거절한다.
사실 근거 크기는 요청의 `chunk_chars`(기본 1,000자), statement는 500자,
action·surface는 각각 200자 한도다. v4의 500자 원문 발췌는 명시적인 과거 읽기 경로로 보존한다.

[test_semantic_evaluation.py](../../tests/refresh/test_semantic_evaluation.py)는 등록한
행동·이름 표현, 문서 참조, 확실성·범위·시점과 사실을 참조하는 제안의 종류·대상을
비교한다. 사실이 응답에 있다는 것만으로 제안의 의미가 맞다고 판정하지 않는다.
알려진 역할·상태의 불일치는 실패이며 미등록 표현·추가 사실은 검토 대상으로 남긴다.
자유 본문의 바꿔 쓴 표현을 자동 실패시키거나, 구조화 기준 일치를 본문 의미 검증
완료로 표현하지 않는다. `source_entailment`와 자유 본문은 계속 사람 검토 대상이다.

독립 `semantic` 자료는 6사례·계획 호출 16회다. 승인자·운반자·수령인 구분을 이름·
문장 순서를 바꾸어 확인하고, 일부 반환·미반환, 과거 기록·현재 상태·미래 동의 조건,
물건별 속성, 익명 담당자의 잘못된 인물 귀속을 점검한다. Linux 호스트의 저장소 루트에서
새 출력 경로로 실행한다.

```bash
.venv/bin/python -m scripts.evaluate_refresh \
  --suite semantic --env-file .env.example --max-calls 16 \
  --trial semantic-check-1 --output .evaluation/semantic-check-1 --repeat-saved

.venv/bin/python -m scripts.evaluate_refresh \
  --suite semantic --live --env-file .env --max-calls 16 \
  --trial semantic-live-1 --output .evaluation/semantic-live-1
```

첫 명령은 고정 후보로 검증·집계·저장·재사용 경로를 확인하며 원격 요청은 없다.
두 번째는 실제 OpenAI를 최대 16회 호출한다. 예산은 실행 전에 검사하며 자동 재시도하지 않는다.
두 평가 모두 S3·Content·관계 조회·작업 상태·Kafka는 fake다. 보고서에는 제안별 근거를
표시하지 않고 결과 JSON에 보존한다. 종료 코드 `0`은 등록된 자동 검사 통과,
`1`은 실행·규칙·필수 내용·구조화 기준 실패, `2`는 미등록 설정·관계·사실·표현의
검토 필요다. `0`이어도 원문 의미와 자유 본문에 대한 사람 검토는 남는다.

프롬프트의 판단 예시는 원본 평가 소설의 특정 이름·정답을 복사하지 않은 별도 자료다.
독립 사례와 이름·순서 변경은 일부 오귀속을 점검하지만 일반적인 소설 추출 품질을
보증하지 않는다. 최신 실제 호출 결과와 회귀 검사 수는 완료 기록으로 별도 확인한다.

## 새 설정 생성과 원고 보존

[test_new_settings.py](../../tests/refresh/test_new_settings.py)는 기존 설정 없는 원고 한 편,
새 인물과 기존 인물 연결, 새 후보끼리 연결, 생성만 있는 PROPOSED와 JSON 왕복·결과 재사용을
검증한다. 첫 호출만 생성을 허용하는 범위, 잘못된 후보 ID·분류·revision·근거,
중복 이름·충돌 본문의 전체 실패도 확인한다. 원고 수정은
[test_document_validation.py](../../tests/refresh/test_document_validation.py)에서
원고 ID를 허용 대상으로 주입해도 거절하는지 확인한다.

평가의 `manuscript_content_preserved`는 최초 입력·저장된 실행 스냅샷·입력 저장소의 원고를
비교한다. 공백·줄바꿈·본문 변경 또는 원고 수정 제안이 있으면 false이고 평가 명령은 실패한다.
`expected_new_settings`는 필수 생성 후보의 분류·정규화 이름과 금지 후보 부재를 검사한다.
허용 후보를 별도로 지정하고 미등록 추가 후보는 검토 필요로 표시한다. 필수 수정·내용·보존·관계
검사도 CLI 실패에 반영하며 Markdown과 같은 판정을 사용한다.
이는 설정 내용의 의미적 정확성이나 프로젝트 전체 중복 부재를 보증하지 않는다.
조회하지 않은 설정의 중복·별칭·동명이인 판단과 Content 승인 시 ID 발급·관계 반영은 후속 검증이다.

저장소 루트에서 실행한다. 출력 디렉터리는 새 경로를 사용하며 원격 요청은 없다.

```bash
.venv/bin/python -m scripts.evaluate_refresh \
  --suite new-settings --env-file .env.example \
  --trial new-settings-v2 --output .evaluation/new-settings-v2 --repeat-saved
```

현재 생성 후보는 `refresh-candidate-v5`, 프롬프트는 `refresh-prompt-v11`, 결과는
`refresh-result-v3`다. 결과는 facts·fact_ids와 공통 근거 사전을 저장하고 과거 v1/v2
결과 읽기를 유지한다. 모델의 출처 마커를 전체 원문 구간으로 복원하고,
관계쌍별 소유 호출을 검사한다. 기본 `separate_discovery=False`는 대상별 호출을 유지하고
`True`는 기존 수정이 금지된 탐색 1회와 대상 수정 N회로 분리한다. 같은 호출 내 설명
충돌과 비소유 관계는 계속 실패한다. 상세 계약은
[갱신안 입력·결과 계약](../migration/proposal-contract.md#근거와-결과-검증)을 따른다.
[새 생성·연결 예시](../examples/refresh/result-new-settings.json)를 참고한다.

## 출처 표현 관찰 평가

`scripts.evaluate_refresh`의 `--observe-sources`는 정식 결과와 별도로 검증 전 후보를
관찰한다. 운영 rules·보완 정책·CLI 종료 판정은 그대로 유지한다. 후보가 최종 FAILED로
폐기돼도 각 최초·보완 응답의 모든 action·nonnull 역할 surface와 인용 구간 검사를
수집하고, 등록된 수정·생성·관계·사실·본문 기준을 검증 대상 후보에 적용한다. 서버 보존이 적용된
보완은 `candidate`에 모델 원본, `effective_candidate`에 실제 검증 후보를 따로 기록하고,
`raw_source_observation`에 모델 원본의 출처 검사를 추가한다.
이 비교에는 `rules_accepted` 합격 표시를 붙이지 않으며 미검증 후보라고 명시한다.

Linux 호스트의 저장소 루트에서 실행한다. 아래 첫 명령은 네트워크 없이 관찰 경로를
검증하고, 두 번째는 실제 모델 호출을 수행한다. 출력 디렉터리는 새 경로를 사용한다.

```bash
.venv/bin/python -m scripts.evaluate_refresh \
  --suite semantic --env-file .env.example --max-calls 16 \
  --observe-sources --trial source-observation-offline \
  --output .evaluation/source-observation-offline --repeat-saved

.venv/bin/python -m scripts.evaluate_refresh \
  --live --suite semantic --env-file .env --max-calls 32 \
  --observe-sources --trial source-observation-live \
  --output .evaluation/source-observation-live --repeat-saved
```

현행 v5에서 LLM은 `source.marker`만 반환하고 서버가 quote·위치를 복원한다.
`source.quote_exact`는 복원한 근거와 실행 스냅샷의 무결성 검사다. 모델이 직접 선택한
`action`·`surface`의 원문 포함 검사와 분리해 해석한다. 파싱·통신 실패는 표현 불일치
0건이 아니라 관찰 불가로 남긴다.

기존 case·attempt JSON과 `report.md` 외에 다음 관찰 산출물을 생성한다.

| 산출물 | 내용 |
|---|---|
| `source-observation.json` | 최초·보완별 관찰 가능/불가 후보 수, 검사별 분모·불일치·영향 후보 수, 모든 불일치와 미검증 후보의 기준 비교 |
| `source-observation.md` | 집계와 불일치 표현·사실 문장·선택 원문, 원래 후보 기록 링크 |

검사 분모는 호출 수가 아니라 검사한 원문·행동·역할 표현 수다. 보완은 실패한 호출만
대상으로 하므로 최초·보완 수치를 전체 표본의 전후 비교처럼 해석하지 않는다.
표기 차이 가능성·다른 마커의 표현 존재·문맥 전체에서 표현 미발견은 분류 힌트다.
정규화하거나 다른 마커로 자동 교체하지 않는다. 의미가 같은 의역인지, 사실을 잘못
연결했는지, 단순 표기 오류인지는 원문·제안 본문을 사람이 대조해야 한다.
불일치가 없는 후보도 역할·부정·범위·시점의 의미 정확성을 보장하지 않는다.

### 소설 전체를 호출 예산 3회 이내에서 관찰

기본 `novel-settings`는 신규 탐색 1회와 기존 설정 8개 수정으로 최초 9회가 필요하다.
`--single-call`은 평가에 한해 두 변경 원고 전체·기존 설정 8개·모든 수정 대상과
신규 생성을 한 호출에 묶는다. `separate_discovery=false`와 `call_strategy=single-call`을
기록하며 기본 서비스의 대상별 호출 전략을 바꾸지 않는다. 보완은 같은 전체 범위로
최대 2회이고 SDK 자동 재시도는 없다. `--max-calls`는 보완까지 포함한 전체 상한이다.

```bash
.venv/bin/python -m scripts.evaluate_refresh \
  --live --suite novel-settings --single-call --env-file .env --max-calls 3 \
  --observe-sources --trial source-observation-novel-single \
  --output .evaluation/source-observation-novel-single --repeat-saved
```

이 조건은 기본 9회 전략의 처리 성능·품질을 재현하는 평가가 아니다. 모든 자료를
유지하면서 적은 호출로 출처 표현 오류를 관찰하는 실험이며 출력 예산·불완전 응답도
별도로 확인한다. 보완 호출은 남은 예산 안에서만 실행한다.

[단일 호출 테스트](../../tests/refresh/test_single_call_evaluation.py)는 두 원고의 전량
포함, 대상 8개와 신규 관계 소유권, 보완 범위·호출 상한, 저장 결과 재사용과 기본 전략의
예산 검사를 확인한다.

[관찰 평가 테스트](../../tests/refresh/test_source_observation.py)는 다중 불일치 수집,
후보 무변경, 공식 FAILED 유지, 실패 후보의 품질 비교, 최초·보완 분리, 통합 후보가
마지막 호출 기록을 덮어쓰지 않는지와 보고서 재렌더를 검증한다.

## S3 어댑터

[test_s3_artifacts.py](../../tests/refresh/test_s3_artifacts.py)는 엄격 JSON·타입,
객체 크기·스트림 종료·SDK 설정을 검증한다.
[test_s3_http.py](../../tests/integration/test_s3_http.py)는 실제 boto3와 로컬 HTTP 서버로
입력·context·result 왕복, 불변 쓰기·요청 충돌, 해시 손상·권한·버킷·503/409 실패,
저장 뒤 응답 유실·새 어댑터 복구와 원고 보존을 검증한다. 실제 AWS는 호출하지 않는다.
설정·보존 조건은 [S3 저장소](S3_ARTIFACTS.md)를 따른다.

최신 검사와 실제 OpenAI 평가 결과는 [품질 개선·검증 기록](../migration/refresh-quality.md)을 따른다.
운영 S3·Content·Neptune·Kafka 연동은 미연결이다.


## 부분 보완의 정보 보존 관찰

`repair-v2`는 진단이 명시한 사실의 행동·역할 표현 오류에서 그 사실을 참조하지 않는
문서 수정·신규 설정·관계 제안을 이전 후보에서 보존한다. 해당 제안의 참조 사실도
복원하고 전체를 재검증한다. 파싱 오류·사실 ID 중복·출처 전역 오류처럼 안전하게
범위를 특정할 수 없는 오류에는 전체 후보 보완을 유지한다. 보완 대상 본문의 다른
정보와 최초 생성의 완전성을 서버가 의미적으로 보증하는 기능은 아니다.

평가에서 파싱된 보완 응답을 검증하면 `repair-observation.json`과
`repair-observation.md`를 생성한다. 이전 후보와 모델 원본·서버 보존 후 후보를 각각
비교하여 삭제된 제안, 바뀐 본문, 더 이상 그대로 나타나지 않는 이전 문장·사실을 기록한다.
재서술이나 정당한 오류 제거도 문자열 부재에 포함하므로 그 자체를 품질 실패로
판정하지 않는다. 보완이 없으면 이 두 파일을 생성하지 않는다.

각 attempt의 `instruction_fingerprint`는 실제 지시문의 SHA-256이다. 프롬프트 버전명이
같아도 문구가 달라진 실행을 구분하는 평가 자료이며 운영 결과 계약의 새 필드는 아니다.


## 사실별 confidence 관찰

목적은 모델 자기평가 점수가 실제 관측 오류와 관련이 있는지 조사하는 것이다.
`--confidence`가 v6 후보의 facts마다 0∼1 숫자를 요청하며 기본 실행은 v5다.
confidence의 추가 효과를 보려면 옵션 없는 실행과 옵션 있는 실행을 각각 새 trial로
수행하고 원문·기존 설정·모델·기본 프롬프트 버전·청크·호출 전략·토큰 예산을 맞춘다.
출처 오류뿐 아니라 기존 누락·낡은 내용·관계 검사, 지연·토큰도 함께 비교한다.
현재 A/B 도구는 같은 후보 스키마를 요구하므로 v5/v6 개별 실행 보고서로 비교한다.

아래는 Linux 호스트의 저장소 루트에서 실행할 실제 API 명령 예시다. 2026-10-13 실제
평가도 같은 조건에 새 trial·출력 경로를 사용했다. 이전 소설과 같은 통합 호출 조건에서
보완 포함 상한을 3회로 지정했다.

```bash
.venv/bin/python -m scripts.evaluate_refresh \
  --live --env-file .env --model gpt-6-luna \
  --suite novel-settings --single-call --prompt-version refresh-prompt-v11 \
  --confidence --observe-sources --repeat-saved \
  --timeout 300 --max-input-tokens 120000 --max-output-tokens 32768 \
  --context-window 152768 --max-calls 3 \
  --trial novel-confidence-01 --output .evaluation/novel-confidence-01
```

`--confidence`는 `--live` 또는 v6 후보의 `--responses-dir` 재생에서 사용한다.
고정 expected 후보에서 점수를 만들어 채워 넣지 않는다. 누락된 점수·파싱 실패·timeout은
점수 0이나 오류 0으로 계산하지 않는다. 반환 사실이 없는 경우에도 빈 사실 목록을
성능 합격으로 해석하지 않는다.

| 기록 | 내용 |
|---|---|
| facts[].confidence | 요청에서 생성한 모델 자기평가. 기본 v5 결과에는 없음 |
| confidence-observation.json | 최초·보완 × 모델 원본·실제 검증 후보의 사실별 점수·문자열 검사 오류·해당 사실의 엄격 진단 |
| confidence-observation.md | 점수 구간별 사실 수·평균 점수·문자열 오류 사실 수·엄격 진단 사실 수·미검사 수 |

점수 구간 `[0,0.5)`, `[0.5,0.8)`, `[0.8,0.95)`, `[0.95,1]`은 보고서 집계 구간이며
생성·채택 임계값이 아니다. 문자열 검사는 quote 원본 일치, action·surface의 원문 포함,
surface의 statement 포함 여부는 표현 차이로 별도 관찰하고 오류 수에 넣지 않는다. 검증 진단은 실제 보고된 오류이며
진단이 없다는 사실로 모든 검사가 완료됐다고 주장하지 않는다. 서버 보존 후 후보와
다른 모델 원본에는 서버 후보의 진단을 잘못 연결하지 않는다.

낮은 점수의 사실도 기존 rules를 통과하면 유지하고 높은 점수도 출처 오류를 통과하지
못한다. 누락은 출력 사실의 점수로 검사할 수 없으므로 기존 평가를 병행한다. 모든
사실의 의미적 정답 라벨이 없어 정답률 보정·ECE·Brier 점수는 계산하지 않는다.
반복되는 동일 사실도 후보별 관측 수로 명시하며 독립 표본으로 해석하지 않는다.

구현 검증은 모의 SDK 응답과 v6 고정 응답 재생으로 파싱·저장·보완·보고서 연결을
확인한다. 테스트의 고정 confidence 숫자는 모델의 실측 점수가 아니다. 2026-10-13에는 전체 소설 변경을 실제 API 생성·보완 2회로 평가했다. 높은 confidence
0.98의 원문 표현 오류를 보완했고 최종 평가는 검토 필요다. 단일 비교이며 낮은 점수의
후보가 없어 오류 감소 효과·저신뢰 오류 예측 성능은 확정하지 않는다. 상세 결과는
[실제 평가 기록](../migration/refresh-quality.md#confidence-소설-실제-api-평가-2026-10-13)을 따른다.

## 원문 표현 유사도 보정 검증

`tests/refresh/test_source_alignment.py`는 공백·조사·오타·활용형의 높은 유사도 복원,
동점 위치의 보정 거부, 높은 유사도에서도 이름·숫자·부정 변경 거부,
다른 마커와 위조 원문 거부, summary 표현 차이 허용 및 동기화,
관계 설명 동기화와 기존 잘못된 설명의 보존, 보정의 반복 적용,
서비스의 추가 호출 없는 처리, 보정 기록 저장·복구와 변조 거부를 확인한다.

2026-10-13 검증에서는 전체 936개 테스트가 통과했다. Ruff 검사·포맷과
`git diff --check`도 통과했다. 외부 LLM 호출은 포함하지 않았다.

5화 수정·6화 추가의 이전 실제 API 최초 응답 재생은 원문 표현 1건을 복원해
PROPOSED가 되었으며 보완 호출은 없었다. 품질 판정은 기존 본문 표현 미확인 2건 등으로
review_required다. [품질 기록](../migration/refresh-quality.md)의
「원문 표현 유사도 보정과 실제 응답 재생」에 조건과 결과를 기록했다.

## 계획·문단 작성 두 단계 검증

`tests/refresh/test_two_stage.py`는 다중 문서의 속성 교체, 문단 밖 원문·공백 보존,
문단 추가·삭제, 모르는 ID·revision·마커·중복 작업·계획 누락·무효 교체의 거절,
SDK의 계획·작성 스키마, 결과·계획 저장 복구, 최초·보완의 전체 3회 상한을 검증한다.
추가 변경 원고 마커의 사용은 기록하고, 기존 설정 마커를 새 변화의 근거로 사용하는 것은
거절한다. 계획 단계의 facts/점수 없음은 not_applicable이며 작성 통계에 합산하지 않는다.

2026-10-13 두 단계 최초 구현 후 전체 테스트는 963개 통과, Ruff·포맷·git diff --check 통과다.
테스트는 외부 모델을 호출하지 않았다. 별도 실제 소설 평가는 총 3회 API를 사용했다.
계획에서 재질 교체 대상 두 문서를 모두 찾고 작성 본문도 교체했지만, 최종 검증은 실패했다.
출처 범위 규칙 수정 후 저장 응답의 재생에서도 구조화 사실 오류가 남았다.
[품질 기록](../migration/refresh-quality.md)의 「계획·작성 두 단계 실제 소설 평가」에
실제 API 결과와 현재 코드의 저장 응답 재생을 구분했다.

추가 검증은 작성 응답의 다른 보존 문단 복사와 APPEND의 앵커 문단 복사를 제거하고,
원문 공백·문단 끝 개행을 유지하며, 제거 전 텍스트·문단 ID를 저장·복구하는지 확인한다.

문자 검사를 유사도 기반으로 확장한 뒤 전체 테스트 972개가 통과했다.
같은 실제 API 응답을 추가 호출 없이 재검증해 Refresh는 PROPOSED가 되었으며,
품질 평가의 강연오 경력 누락은 failed로 남았다. 요약의 문자 차이는 confidence 관찰에서도
오류 수에 포함하지 않는다. 재검증 기록은 [품질 기록](../migration/refresh-quality.md)의
「유사도 보정 범위 확장과 저장 응답 재검증」을 따른다.
