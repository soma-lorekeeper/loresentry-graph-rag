# 소설 5화 수정·6화 추가의 fake S3 입력

이번 소설 평가에서는 기존의 짧은 유나 원고 대신 [input.json](input.json)을 읽는다.
실제 S3에 업로드하지 않고, 로컬 JSON을 내부 `InputSnapshot`으로 변환해
`FakeJsonArtifacts`에 넣는다. 입력 파일은 평가용 `evaluation-input-v1` 형식이며
Content와 합의된 운영 S3 JSON Schema는 아니다.

## 입력과 기존 설정

| 파일 | 공급 경계·내용 |
|---|---|
| [input.json](input.json) | S3 fake. `chapter-05` revision 2와 `chapter-06` revision 1, 본문 전체와 명시 관계 |
| [context.json](context.json) | Content·관계 조회 fake. 기존 도윤·해주·검지 의지 설정과 도윤–의지 관계 |
| [expected.json](expected.json) | 비교용 대체 후보. 입력 원문이나 기존 설정에 적용하지 않음 |
| [responses](responses) | 대화에서 작성한 후보를 대상별로 나눈 재생 파일. 실제 API 응답이 아님 |

5화는 도윤의 의지 덮개를 황동에서 검게 산화시킨 강철로 수정했다. 6화는 도윤의
기억 반환소 방문 전달 역할과 민해주와의 업무 관계를 추가한다. 5화는 4,974자,
6화는 4,955자다. 입력 문서는 2개이며 요청 manifest에도 같은 ID·revision·상태를 넣는다.

기존 설정 fake에는 수정 전의 황동 의지와 기존 배달 업무를 남겨 두었다.
추가 조회한 설정 3개는 최종 결과의 `sources.origin=CONTENT`, 변경 회차 2개는
`INPUT`으로 표시된다. 6화의 새 등장인물 강연오는 이번 기존 설정 목록에 없으므로
새 인물 문서 생성은 제안하지 않는다. 현재 계약은 기존 문서 갱신과 관계 ADD만 지원한다.

## 실행

저장소 루트에서 실행한다. 새 출력 디렉터리를 지정해야 하며 키는 필요 없다.
기존 작은 회귀 자료는 `--suite basic`으로 유지하고, 소설 평가는 `--suite novel`을 선택한다.

```bash
.venv/bin/python -m scripts.evaluate_refresh \
  --suite novel --env-file .env.example \
  --responses-dir docs/examples/refresh/novel/responses \
  --trial novel-refresh-v2 --output .evaluation/novel-refresh-v2 \
  --repeat-saved
```

연결은 [novel_case](../../../../evaluation/novel.py)가 JSON을 읽고,
[assemble](../../../../evaluation/runner.py)이 S3·Content·관계 조회 fake에 주입하는 순서다.
입력과 기존 문맥을 함께 해시해 재생 응답의 fingerprint와 대조한다.
문서를 바꾼 뒤 기존 응답을 그대로 쓰면 불일치로 거절한다.

## 확인한 결과

2026-10-04 실행 경로는 `.evaluation/novel-refresh-v1/`이다.
`novel-refresh.json`의 `snapshot`에서 실제 실행 입력을, `result`에서 최종 제안을,
`assessment`에서 비교 결과를 본다. 대상별 `novel-refresh-attempt-*.json`에는
모델 입력·재생 원문·파싱 후보·실패 여부가 있다.

- 대체 호출 3회, 최종 PROPOSED.
- 도윤·해주·의지 설정에 대한 문서 변경 제안 3개.
- 도윤–해주의 업무 관계 ADD 1개. 대상별 중복 후보는 하나로 합쳐짐.
- 원문 구간·revision·대상·기존 내용 보존 검사 통과.
- 같은 결과로 재실행해도 추가 모델 호출 없음.

이 결과는 대화에서 작성한 고정 후보의 파싱·판단·실행 흐름 검증이다. 기대 후보와
재생 응답은 같은 작성본을 사용하므로 독립적인 모델 품질 평가는 아니다.
실제 모델 호출·사용량 측정은 수행하지 않았고 원문·기존 설정·확정 그래프도 바꾸지 않았다.

원문을 편집할 때는 해당 Markdown의 제목과 파일 끝 개행을 제외한 본문을 `input.json`에
동일하게 반영한다. 관련 revision·관계·기대 근거와 재생 응답도 함께 갱신해야 한다.
[전용 테스트](../../../../tests/refresh/test_novel_fixture.py)가 원문과 JSON 일치, 5,000자 한도,
manifest, 기존 설정 조회, 제안 저장과 재실행을 확인한다.
