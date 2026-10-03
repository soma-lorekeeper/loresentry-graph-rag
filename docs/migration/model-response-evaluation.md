# 모델 응답 대체 평가 결과

2026-10-03 사용자의 요청에 따라 실제 API 생성 평가를 **대화에서 작성한 응답 JSON
재생**으로 대체했다. 원본 응답은 [assistant-v1](../../evaluation/responses/assistant-v1)에
있다. 기대 후보를 반환하는 기존 오프라인 모드와 별도로, 이 파일을 실제 후보 파서와
rules·서비스에 전달했다. 원격 모델 품질 평가나 독립적인 블라인드 평가는 아니다.
응답 작성 시 입력과 기존 기대 사례를 모두 확인했다.

## 실행과 결과

저장소 루트에서 다음 명령을 실행한다. API 키를 읽을 필요가 없다.
출력 디렉터리는 매번 새 경로를 사용한다.

```bash
.venv/bin/python -m scripts.evaluate_refresh \
  --env-file .env.example \
  --responses-dir evaluation/responses/assistant-v1 \
  --trial assistant-v1-verified \
  --output .evaluation/assistant-v1-verified \
  --repeat-saved
```

실행 결과는 `live=false`, 6개 사례, 7회 대체 호출, `failed=false`였다.
모델 설정의 `gpt-5.6-luna`는 원격 실행을 위한 요청 설정일 뿐 이번 응답을 생성한
API 모델이 아니다. 실제 응답 출처는 `conversation-assistant`다.
후보 스키마는 `refresh-candidate-v1`, 프롬프트는 `refresh-prompt-v2`, 설치 SDK는
`openai==3.24.0`이다. 재생 경로는 SDK 클라이언트를 만들지 않는다.

| 사례 | 대체 호출 수 | 최종 결과 | 확인한 내용 |
|---|---:|---|---|
| document | 1 | PROPOSED | 망토 색 변경, 기존 고향 서울 보존 |
| relation | 1 | PROPOSED | 두 원고의 이어지는 사건에 대한 관계 ADD |
| no-change | 1 | NO_CHANGE | 이미 일치하는 설정에 제안 없음 |
| both | 2 | PROPOSED | 인물·아이템 본문 제안, 중복 관계 후보 하나로 정리 |
| existing | 1 | NO_CHANGE | 이미 저장된 원고·인물 연결을 추가하지 않음 |
| long-span | 1 | PROPOSED | 4,852자 원문의 `[4800:4815]` 인용 일치, 반복·이모지·줄바꿈 보존 |

여섯 사례 모두 rules 수락, 기대 대상 누락·추가 없음, 필수 내용 보존과 기대 관계 비교를
통과했다. `no-change`와 `existing`은 현재 같은 자료를 다른 관점으로 확인하는 사례다.
이 수치를 서로 독립적인 여섯 종류의 품질 표본으로 해석하지 않는다.
긴 원문의 악성 지시는 후보에 반영하지 않았지만 실제 모델의 지시 공격 내성을 증명하지 않는다.
잘못된 start=4799를 주입한 별도 테스트에서는 위치를 보정하지 않고 전체 FAILED로 처리했다.

평가의 의미상 발명 검토 필드는 `manual_review_required`로 유지한다. 사람이 품질에
합격 판정을 내린 것으로 표시하지 않았다. 같은 저장 결과를 재실행할 때 추가 모델
호출이 없는 것도 확인했다. 프로세스 재시작 뒤의 영속 복구는 fake 범위 밖이다.

## 산출물과 실제 API 상태

`.evaluation/assistant-v1-verified/`의 사례별 JSON에는 최종 결과·스냅샷·rules 판정을,
`*-attempt-*.json`에는 대체 응답 원문·파싱 후보·버전·시간을 기록했다. 산출물은 Git에서
제외하며 파일 권한은 0600이다. 이번에 커밋한 응답 파일은 합성 자료로 직접 작성한
재현 fixture이며 실제 프로젝트 문서나 공급자 원본 응답이 아니다.

재생 시 원격 응답·사용량은 null이고 `remote_requested=false`, `usage_known=false`다.
내부 `Usage`의 토큰 0은 타입을 만족시키는 대체 값이며 실제 모델 비용 측정치가 아니다.
`--live`와 `--responses-dir`는 함께 사용할 수 없다. 입력 fingerprint·대상·버전이
다른 응답 파일은 거절한다. 파싱 실패를 NO_CHANGE로 바꾸지 않는다.

앞선 실제 호출 `lorekeeper-665-live-1`은 6회 모두 HTTP 401 `invalid_api_key`로
실패했다. 사용량은 확인되지 않았고 생성된 모델 후보도 없다. 이후 도구는 공통 인증·할당량
실패가 나오면 나머지 사례 실행을 중단하도록 보완했다. 실제 인증 성공·gpt-5.6-luna의
생성 품질·사용량 검증은 [후속 작업](follow-up.md)의 미완료 항목으로 남긴다.

## 소설 입력으로 확장한 평가

2026-10-04 [소설 5화 수정·6화 추가](../examples/refresh/novel/README.md)를
별도 `novel` suite로 연결했다. 원래 여섯 `basic` 사례와 분리해 실행하며,
변경 문서 2개와 기존 설정 3개를 사용한 결과·명령은 해당 안내에 기록했다.
