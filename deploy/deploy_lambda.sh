#!/usr/bin/env bash
# Create or update the Lambda function and its public function URL.
#
# Requires, beyond the Bedrock-only policy used for development:
#   lambda:CreateFunction, lambda:UpdateFunctionCode,
#   lambda:UpdateFunctionConfiguration, lambda:CreateFunctionUrlConfig,
#   lambda:AddPermission, lambda:GetFunction
#   iam:CreateRole, iam:AttachRolePolicy, iam:PassRole
#
# Those are deploy-time only. The function's own role stays Bedrock-only, so
# the thing exposed to the internet cannot touch anything else in the account.
set -euo pipefail

cd "$(dirname "$0")/.."

FN=${FN:-unsay-demo}
REGION=${AWS_REGION:-us-east-1}
ROLE=${ROLE:-unsay-lambda-role}
ZIP=deploy/unsay-lambda.zip

[ -f "$ZIP" ] || { echo "run deploy/build_lambda.sh first"; exit 1; }
[ -n "${UNSAY_CLOUD_DSN:-}" ] || { echo "UNSAY_CLOUD_DSN is required"; exit 1; }

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
ROLE_ARN="arn:aws:iam::${ACCOUNT}:role/${ROLE}"

if ! aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  echo "creating execution role $ROLE"
  aws iam create-role --role-name "$ROLE" \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
  aws iam attach-role-policy --role-name "$ROLE" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
  # Bedrock only. The demo reads and writes CockroachDB over the network with
  # credentials in the DSN, so it needs no AWS data permissions at all.
  aws iam attach-role-policy --role-name "$ROLE" \
    --policy-arn arn:aws:iam::aws:policy/AmazonBedrockFullAccess
  echo "waiting for role propagation"; sleep 12
fi

ENV_VARS="Variables={UNSAY_DSN=${UNSAY_CLOUD_DSN},AWS_REGION_NAME=${REGION},UNSAY_POOL_MIN=0,UNSAY_POOL_MAX=2}"

if aws lambda get-function --function-name "$FN" --region "$REGION" >/dev/null 2>&1; then
  echo "updating $FN"
  aws lambda update-function-code --function-name "$FN" --region "$REGION" \
    --zip-file "fileb://$ZIP" >/dev/null
  aws lambda wait function-updated --function-name "$FN" --region "$REGION"
  aws lambda update-function-configuration --function-name "$FN" --region "$REGION" \
    --environment "$ENV_VARS" --timeout 120 --memory-size 512 >/dev/null
else
  echo "creating $FN"
  aws lambda create-function --function-name "$FN" --region "$REGION" \
    --runtime python3.12 --role "$ROLE_ARN" \
    --handler unsay.lambda_handler.handler \
    --zip-file "fileb://$ZIP" \
    --timeout 120 --memory-size 512 \
    --environment "$ENV_VARS" >/dev/null
  aws lambda wait function-active --function-name "$FN" --region "$REGION"
fi

# Public function URL. AuthType NONE because judges must be able to open it
# without AWS credentials; the function's own role grants nothing but Bedrock.
if ! aws lambda get-function-url-config --function-name "$FN" --region "$REGION" >/dev/null 2>&1; then
  aws lambda create-function-url-config --function-name "$FN" --region "$REGION" \
    --auth-type NONE >/dev/null
  aws lambda add-permission --function-name "$FN" --region "$REGION" \
    --statement-id public-url --action lambda:InvokeFunctionUrl \
    --principal '*' --function-url-auth-type NONE >/dev/null
fi

URL=$(aws lambda get-function-url-config --function-name "$FN" --region "$REGION" \
  --query FunctionUrl --output text)
echo "deployed: $URL"
echo "health:   ${URL}api/health"
