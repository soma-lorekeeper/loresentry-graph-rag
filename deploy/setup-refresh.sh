#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
cluster_name="${CLUSTER_NAME:-lore-sentry-k8s}"
content_role_name="${CONTENT_ROLE_NAME:-}"
if [[ -z "$content_role_name" ]]; then
  association_id="$(aws eks list-pod-identity-associations --cluster-name "$cluster_name" \
    --namespace prod --service-account content-api --query 'associations[0].associationId' --output text)"
  if [[ -z "$association_id" || "$association_id" == None ]]; then
    echo 'Content Pod Identity association is missing; set CONTENT_ROLE_NAME explicitly.' >&2
    exit 1
  fi
  content_role_arn="$(aws eks describe-pod-identity-association --cluster-name "$cluster_name" \
    --association-id "$association_id" --query 'association.roleArn' --output text)"
  content_role_name="${content_role_arn##*/}"
fi
aws cloudformation deploy --stack-name loresentry-refresh-artifacts \
  --template-file refresh-infrastructure.yaml --capabilities CAPABILITY_IAM \
  --parameter-overrides "ClusterName=$cluster_name" "ContentRoleName=$content_role_name" \
  --no-fail-on-empty-changeset
