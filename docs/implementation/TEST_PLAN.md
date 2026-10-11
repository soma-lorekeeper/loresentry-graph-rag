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

## S3 어댑터

[test_s3_artifacts.py](../../tests/refresh/test_s3_artifacts.py)는 엄격 JSON·타입,
객체 크기·스트림 종료·SDK 설정을 검증한다.
[test_s3_http.py](../../tests/integration/test_s3_http.py)는 실제 boto3와 로컬 HTTP 서버로
입력·context·result 왕복, 불변 쓰기·요청 충돌, 해시 손상·권한·버킷·503/409 실패,
저장 뒤 응답 유실·새 어댑터 복구와 원고 보존을 검증한다. 실제 AWS는 호출하지 않는다.
설정·보존 조건은 [S3 저장소](S3_ARTIFACTS.md)를 따른다.

최신 검사와 실제 OpenAI 평가 결과는 [품질 개선·검증 기록](../migration/refresh-quality.md)을 따른다.
운영 S3·Content·Neptune·Kafka 연동은 미연결이다.
