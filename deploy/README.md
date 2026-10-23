# Kafka 업무 배포

앱 main push는 기존 GitHub Actions가 테스트·이미지 빌드·GitOps 이미지 태그 갱신을 수행하고,
Argo CD가 Deployment를 반영한다. AWS 자격증명을 앱 코드나 Git에 저장하지 않는다.
비공개 S3 버킷·IAM 역할·EKS Pod Identity는 Kubernetes manifest와 별도로 준비한다.

## 배포 순서

1. Content와 GraphRAG의 CI/CD를 성공시켜 새 이미지를 준비한다.
2. GraphRAG 저장소의 **Refresh infrastructure** workflow를 main에서 실행한다.
   기존 Content Pod Identity의 역할을 찾아 [CloudFormation 템플릿](refresh-infrastructure.yaml)을 적용한다.
3. 성공 후 GitOps의 `workload/base/refresh` ConfigMap(버킷·리전)과 graph-rag Deployment의
   `graph-rag-api` service account·refresh env(`KAFKA_REFRESH_REQUESTED_ENABLED=true` 포함),
   content Deployment의 `GRAPHRAG_S3_BUCKET`을 반영한다.
4. Content/GraphRAG Pod 준비 상태와 로그, 실제 프로젝트의 요청 → S3 `result.json`을 확인한다.
   Content의 완료 처리(READY/FAILED 갱신)는 아직 없어 Content 쪽 상태는 바뀌지 않는다.

변경 이벤트 consumer는 독립적인 `KAFKA_PROJECT_CHANGED_ENABLED=true`로 동작하며
refresh 버킷 활성화 전에도 투영을 처리할 수 있다.
기존 샘플 fake polling은 업무 모드와 동시에 실행하지 않는다.

## AWS 자원과 권한

템플릿은 공개 접근이 차단되고 AES256 기본 암호화가 적용된
`loresentry-refresh-prod-<AWS_ACCOUNT_ID>` 버킷을 만든다. `refresh/` 객체는 3일, 미완료 multipart는
1일에 정리한다. 버킷 삭제·교체 때는 Retain으로 자료를 보존한다. CloudFront 미디어 버킷을 사용하지 않는다.

GraphRAG 역할은 이 버킷 refresh/의 GetObject·PutObject만 허용한다.
Content의 기존 역할에는 같은 prefix의 GetObject·PutObject·DeleteObject와 prefix로 제한한 ListBucket을 추가한다.
GraphRAG의 Pod Identity는 prod/graph-rag-api service account에 연결하고 trust policy의 session tag로
cluster·namespace·service account를 제한한다.
[Pod Identity association](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-eks-podidentityassociation.html)과
[역할 trust policy](https://docs.aws.amazon.com/eks/latest/userguide/pod-id-role.html)는 AWS 공식 계약을 따른다.

workflow는 기존 `vars.AWS_CI_ROLE_ARN`의 OIDC 역할을 사용한다. 이 역할에 CloudFormation stack 관리와
해당 S3/IAM/EKS Pod Identity 자원을 생성·수정할 권한이 필요하다. 기존 이미지 배포 권한만 있다면
AccessDenied로 끝나며 앱 배포 성공을 인프라 준비 성공으로 해석하지 않는다.
관리 권한을 가진 실행 환경에서는 아래 스크립트를 직접 실행할 수도 있다.

```bash
AWS_REGION=ap-northeast-2 bash deploy/setup-refresh.sh
```

자동 발견이 불가능하면 기존 Content IAM 역할의 **이름**을 CONTENT_ROLE_NAME으로 지정한다.
이 스크립트는 Kubernetes API를 변경하지 않는다. service account와 Deployment는 GitOps가 관리한다.
기존 Neptune IAM 인증을 켠 환경에서는 별도로 해당 DB resource의 neptune-db:connect 권한을 공급한다.
현재 기본 구성은 기존 NEPTUNE_IAM_AUTH=false를 유지한다.

## 런타임 설정과 Secret

GitOps는 graph-rag 전용 `graph-rag-openai` Secret의 OPENAI_API_KEY를 참조한다. 챗과 비용을 따로 보도록
`ai-chat-openai`와 나누며, Deployment보다 먼저 만든다. 값을 Git에 추가하지 않는다.
Content의 GRAPHRAG_S3_BUCKET과 GraphRAG의 입력/출력 버킷은 같은 `refresh` ConfigMap에서 공급한다.
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
