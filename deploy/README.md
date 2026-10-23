# Kafka 업무 배포

앱 main push는 기존 GitHub Actions가 테스트·이미지 빌드·GitOps 이미지 태그 갱신을 수행하고,
Argo CD가 Deployment를 반영한다. AWS 자격증명을 앱 코드나 Git에 저장하지 않는다.
비공개 S3 버킷·IAM 역할·EKS Pod Identity는 Kubernetes manifest와 별도로 준비한다.

## 배포 순서

1. Content와 GraphRAG의 CI/CD를 성공시켜 새 이미지를 준비한다.
2. AWS 자원을 만든다(아래 "AWS 자원과 권한"). 먼저 Content 저장소의 `docs/aws/setup-refresh.sh`로
   버킷과 content 권한을, 이어서 이 저장소의 `deploy/setup-refresh.sh`로 graph-rag 역할과 Pod Identity를 만든다.
3. 성공 후 GitOps의 `workload/base/refresh` ConfigMap(버킷·리전)과 graph-rag Deployment의
   `graph-rag-api` service account·refresh env(`KAFKA_REFRESH_REQUESTED_ENABLED=true` 포함),
   content Deployment의 `GRAPH_REFRESH_REQUEST_BUCKET`·`GRAPH_REFRESH_COMPLETED_BUCKET`을 반영한다.
4. Content/GraphRAG Pod 준비 상태와 로그, 실제 프로젝트의 요청 → S3 `result.json`을 확인한다.
   Content의 완료 처리(READY/FAILED 갱신)는 아직 없어 Content 쪽 상태는 바뀌지 않는다.

변경 이벤트 consumer는 독립적인 `KAFKA_PROJECT_CHANGED_ENABLED=true`로 동작하며
refresh 버킷 활성화 전에도 투영을 처리할 수 있다.
기존 샘플 fake polling은 업무 모드와 동시에 실행하지 않는다.

## AWS 자원과 권한

다른 AWS 자원(media 버킷 등)과 같이 IAM·S3 권한이 있는 사람이 작업 PC에서 aws-cli 스크립트로 만든다.
CI 역할에는 이 권한이 없다. 두 스크립트 모두 다시 실행해도 안전하다.

| 스크립트 | 만드는 것 |
| --- | --- |
| Content `docs/aws/setup-refresh.sh` | `loresentry-refresh-prod-<AWS_ACCOUNT_ID>` 버킷(공개 차단, AES256, `refresh/` 3일 만료, 미완료 multipart 1일 정리), Content 역할 `lore-sentry-content-role`에 붙이는 `lore-sentry-content-refresh-policy` |
| 이 저장소 `deploy/setup-refresh.sh` | 정책 `lore-sentry-graph-rag-refresh-policy`, 역할 `lore-sentry-graph-rag-role`, `prod/graph-rag-api` Pod Identity 연결 |

```bash
AWS_PROFILE=lorekeeper bash deploy/setup-refresh.sh
```

GraphRAG 정책([iam-graph-rag-refresh-policy.json](iam-graph-rag-refresh-policy.json))은 `refresh/*`의
GetObject·PutObject와 `refresh/` prefix로 제한한 ListBucket을 허용한다. ListBucket이 없으면 아직 없는
request·context·batch·result 객체를 읽을 때 S3가 404 대신 403을 돌려줘 모든 요청이 `S3_ACCESS_DENIED`로 끝난다.
역할의 신뢰 정책([iam-graph-rag-pod-identity-trust.json](iam-graph-rag-pod-identity-trust.json))은
Content 역할과 같이 EKS Pod Identity만 허용하고, 어떤 service account가 받는지는 Pod Identity 연결이 정한다.
스크립트는 버킷이 없으면 멈추고, Kubernetes API는 바꾸지 않는다. service account와 Deployment는 GitOps가 관리한다.
기존 Neptune IAM 인증을 켠 환경에서는 별도로 해당 DB resource의 neptune-db:connect 권한을 공급한다.
현재 기본 구성은 기존 NEPTUNE_IAM_AUTH=false를 유지한다.

## 런타임 설정과 Secret

GitOps는 graph-rag 전용 `graph-rag-openai` Secret의 OPENAI_API_KEY를 참조한다. 챗과 비용을 따로 보도록
`ai-chat-openai`와 나누며, Deployment보다 먼저 만든다. 값을 Git에 추가하지 않는다.
요청 버킷(`GRAPH_REFRESH_REQUEST_BUCKET`)과 결과 버킷(`GRAPH_REFRESH_COMPLETED_BUCKET`)은 Content와 GraphRAG 모두
같은 `refresh` ConfigMap에서 공급한다.
CONTENT_BASE_URL은 http://content-api이며 신뢰한 user_id는 요청마다 Content 이벤트에서 공급한다.

GraphRAG Pod grace period는 420초, runtime join 예산은 360초다.
모델은 GitOps의 `OPENAI_MODEL`로 정하고 프롬프트·후보 스키마 버전은 코드 기본값을 쓴다.
기본 호출 timeout은 240초다. 종료 시 다음 모델 호출은 시작하지 않는다.
모델을 바꿀 때는 refresh 소비를 끄고 진행 중인 요청을 비운 뒤 바꾼다(같은 요청의 설정이 바뀌면 DLQ).
/health는 worker의 실행 실패를 반영한다. broker 연결·할당·lag는 별도로 확인한다.

## 복구

업무별 enabled를 끄면 소비를 중단한다. 이미 받은 요청의 처리·중복·강제 종료는
[송수신 계약](../docs/implementation/KAFKA_TRANSPORT.md)을 따른다.
DLQ 재투입은 원인 수정 후 같은 event ID·payload·key를 유지한다.
3일이 지난 S3 입력이 없으면 과거 요청을 조작하지 않고 새 요청을 만든다.
Content에는 아직 완료 처리와 요청 timeout이 없어, 완료되지 않은 요청이 그 프로젝트의 다음 요청을 막는다.
순서/삭제 fence가 있는 운영 이벤트를 사용하고 sequence 없는 fixture CLI를 운영 재투입에 쓰지 않는다.
