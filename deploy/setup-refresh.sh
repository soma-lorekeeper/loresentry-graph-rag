#!/usr/bin/env bash
# graph-rag 파드가 그래프 최신화 버킷의 refresh/ 아래를 읽고 쓰게 하는 IAM 역할과 Pod Identity를 만든다.
# 버킷과 content 쪽 권한은 loresentry-content 의 docs/aws/setup-refresh.sh 가 만든다. 그것을 먼저 실행한다.
# 다시 실행해도 안전하다: 정책은 새 기본 버전으로, 신뢰 정책과 Pod Identity 연결은 현재 값으로 갱신한다.
set -euo pipefail

export AWS_PROFILE="${AWS_PROFILE:-lorekeeper}"
export AWS_PAGER=""

CLUSTER=lore-sentry-k8s
NAMESPACE=prod
SERVICE_ACCOUNT=graph-rag-api
ROLE_NAME=lore-sentry-graph-rag-role
POLICY_NAME=lore-sentry-graph-rag-refresh-policy

DIR="$(cd "$(dirname "$0")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="loresentry-refresh-prod-${ACCOUNT}"

echo "account=${ACCOUNT} bucket=${BUCKET}"

echo "== 1. Refresh bucket"
if ! aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  echo "bucket ${BUCKET} is missing; run docs/aws/setup-refresh.sh in loresentry-content first" >&2
  exit 1
fi

echo "== 2. IAM role for the graph-rag pod"
sed "s/__BUCKET__/${BUCKET}/g" "${DIR}/iam-graph-rag-refresh-policy.json" > "${WORK}/policy.json"
POLICY_ARN="arn:aws:iam::${ACCOUNT}:policy/${POLICY_NAME}"
if aws iam get-policy --policy-arn "$POLICY_ARN" >/dev/null 2>&1; then
  VERSION="$(aws iam create-policy-version \
    --policy-arn "$POLICY_ARN" \
    --policy-document "file://${WORK}/policy.json" \
    --set-as-default \
    --query PolicyVersion.VersionId --output text)"
  echo "policy exists, new default version ${VERSION}"
  for v in $(aws iam list-policy-versions --policy-arn "$POLICY_ARN" \
      --query 'Versions[?IsDefaultVersion==`false`].VersionId' --output text); do
    aws iam delete-policy-version --policy-arn "$POLICY_ARN" --version-id "$v"
  done
else
  aws iam create-policy \
    --policy-name "$POLICY_NAME" \
    --policy-document "file://${WORK}/policy.json"
fi
if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  aws iam update-assume-role-policy \
    --role-name "$ROLE_NAME" \
    --policy-document "file://${DIR}/iam-graph-rag-pod-identity-trust.json"
else
  aws iam create-role \
    --role-name "$ROLE_NAME" \
    --assume-role-policy-document "file://${DIR}/iam-graph-rag-pod-identity-trust.json" \
    --description "graph-rag-api pod: read refresh inputs and write refresh artifacts in ${BUCKET}"
fi
aws iam attach-role-policy --role-name "$ROLE_NAME" --policy-arn "$POLICY_ARN"
ROLE_ARN="arn:aws:iam::${ACCOUNT}:role/${ROLE_NAME}"

echo "== 3. EKS Pod Identity association"
aws eks describe-addon --cluster-name "$CLUSTER" --addon-name eks-pod-identity-agent \
  --query 'addon.status' --output text
ASSOC_ID="$(aws eks list-pod-identity-associations \
  --cluster-name "$CLUSTER" --namespace "$NAMESPACE" --service-account "$SERVICE_ACCOUNT" \
  --query 'associations[0].associationId' --output text)"
if [ "$ASSOC_ID" = "None" ] || [ -z "$ASSOC_ID" ]; then
  aws eks create-pod-identity-association \
    --cluster-name "$CLUSTER" \
    --namespace "$NAMESPACE" \
    --service-account "$SERVICE_ACCOUNT" \
    --role-arn "$ROLE_ARN"
else
  aws eks update-pod-identity-association \
    --cluster-name "$CLUSTER" \
    --association-id "$ASSOC_ID" \
    --role-arn "$ROLE_ARN"
fi

cat <<SUMMARY

done.

  bucket  ${BUCKET}
  role    ${ROLE_ARN}
  pod     ${NAMESPACE}/${SERVICE_ACCOUNT}

The association applies to pods created after it. GitOps creates the
${SERVICE_ACCOUNT} service account and rolls the graph-rag Deployment.
SUMMARY
