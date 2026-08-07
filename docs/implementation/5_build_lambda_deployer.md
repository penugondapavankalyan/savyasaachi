# Implementation Guide 5: Build Lambda Deployer

**Order:** Fifth — deploy to AWS after local testing is complete.  
**Reference Docs:** `docs/infrastructure/lambda.md`

---

## Prerequisites

- All previous guides complete and locally tested
- AWS CLI installed and configured (`aws configure`)
- AWS account with Lambda + IAM permissions

---

## Step 1: Final Project Structure

```
savysaachi/
├── src/
│   ├── handler.py
│   ├── agent/
│   ├── mcp/
│   ├── db/
│   ├── redis/
│   ├── telegram/
│   └── utils/
├── migrations/
├── docs/
├── scripts/
│   ├── register_webhook.py
│   ├── deploy.sh
│   └── test_agent_local.py
├── tests/
├── requirements.txt
└── README.md
```

---

## Step 2: `requirements.txt`

```
pydantic-ai>=0.0.14
pydantic>=2.0
supabase>=2.0.0
httpx>=0.27.0
python-pptx>=0.6.21
# PDF library: add chosen library here (e.g., fpdf2>=2.7.0 or reportlab>=4.0)
```

Freeze exact versions after testing:
```bash
pip freeze > requirements.txt
```

---

## Step 3: Create IAM Role for Lambda

```bash
# Create Lambda execution role
aws iam create-role \
  --role-name kirana-agent-lambda-role \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"Service": "lambda.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }]
  }'

# Attach basic Lambda execution policy (CloudWatch logs)
aws iam attach-role-policy \
  --role-name kirana-agent-lambda-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
```

---

## Step 4: Create Lambda Function

```bash
# Package dependencies and source code
mkdir -p package
pip install -r requirements.txt -t package/
cp -r src/ package/
cd package && zip -r ../kirana-agent.zip . && cd ..

# Create Lambda function
aws lambda create-function \
  --function-name kirana-agent \
  --runtime python3.12 \
  --role arn:aws:iam::YOUR_ACCOUNT_ID:role/kirana-agent-lambda-role \
  --handler handler.lambda_handler \
  --zip-file fileb://kirana-agent.zip \
  --timeout 29 \
  --memory-size 512 \
  --ephemeral-storage '{"Size": 512}' \
  --region ap-south-1
```

---

## Step 5: Set Environment Variables

```bash
aws lambda update-function-configuration \
  --function-name kirana-agent \
  --environment "Variables={
    TELEGRAM_BOT_TOKEN=your_token_here,
    SUPABASE_URL=https://your-project.supabase.co,
    SUPABASE_SERVICE_ROLE_KEY=your_service_role_key,
    UPSTASH_REDIS_REST_URL=https://your-redis.upstash.io,
    UPSTASH_REDIS_REST_TOKEN=your_redis_token,
    GROQ_API_KEY=your_groq_key,
    LLM_PROVIDER=groq,
    LLM_MODEL=llama-3.1-70b-versatile,
    LAMBDA_ENV=prod,
    DRAFT_BILL_TTL_HOURS=4,
    MAX_HISTORY_MESSAGES=20
  }" \
  --region ap-south-1
```

> **Security note:** For production, use AWS Secrets Manager instead of plain environment variables for sensitive keys.

---

## Step 6: Create Lambda Function URL

```bash
# Create Function URL (public, no auth — Telegram sends webhooks here)
aws lambda create-function-url-config \
  --function-name kirana-agent \
  --auth-type NONE \
  --region ap-south-1

# Note the FunctionUrl from the response
# Example: https://abc123xyz.lambda-url.ap-south-1.on.aws/

# Allow public invocation
aws lambda add-permission \
  --function-name kirana-agent \
  --action lambda:InvokeFunctionUrl \
  --principal "*" \
  --function-url-auth-type NONE \
  --statement-id AllowPublicInvoke \
  --region ap-south-1
```

---

## Step 7: Register Telegram Webhook

```bash
LAMBDA_FUNCTION_URL="https://abc123xyz.lambda-url.ap-south-1.on.aws/"

curl "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
  --data "url=${LAMBDA_FUNCTION_URL}" \
  --data "allowed_updates=[\"message\",\"callback_query\"]" \
  --data "drop_pending_updates=true"

# Verify
curl "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo"
```

---

## Step 8: Deploy Script

```bash
#!/bin/bash
# scripts/deploy.sh

FUNCTION_NAME="kirana-agent"
REGION="ap-south-1"

echo "📦 Packaging dependencies..."
rm -rf package/ kirana-agent.zip
mkdir package
pip install -r requirements.txt -t package/ -q
cp -r src/ package/

echo "🗜️ Creating zip..."
cd package && zip -r ../kirana-agent.zip . -q && cd ..

echo "🚀 Deploying to Lambda..."
aws lambda update-function-code \
  --function-name $FUNCTION_NAME \
  --zip-file fileb://kirana-agent.zip \
  --region $REGION \
  --query 'FunctionArn' \
  --output text

echo "⏳ Waiting for update..."
aws lambda wait function-updated \
  --function-name $FUNCTION_NAME \
  --region $REGION

echo "✅ Deployed!"

# Clean up
rm -rf package/ kirana-agent.zip
```

Make executable: `chmod +x scripts/deploy.sh`

---

## Step 9: Verify Deployment

```bash
# Invoke function manually with test payload
aws lambda invoke \
  --function-name kirana-agent \
  --payload '{"body": "{\"update_id\": 1, \"message\": {\"from\": {\"id\": 99999, \"first_name\": \"Test\"}, \"chat\": {\"id\": 99999}, \"text\": \"hello\"}}"}' \
  --region ap-south-1 \
  response.json

cat response.json
# Expected: {"statusCode": 200, "body": "OK"}
```

---

## Step 10: CloudWatch Logs

Monitor Lambda execution:
```bash
# View recent logs
aws logs tail /aws/lambda/kirana-agent --follow --region ap-south-1
```

---

## Zip Size Consideration

If `kirana-agent.zip` exceeds 50MB (Lambda direct upload limit), use S3:

```bash
# Upload zip to S3
aws s3 cp kirana-agent.zip s3://your-bucket/kirana-agent.zip

# Deploy from S3
aws lambda update-function-code \
  --function-name kirana-agent \
  --s3-bucket your-bucket \
  --s3-key kirana-agent.zip \
  --region ap-south-1
```

Alternatively, use a **Lambda container image** (supports up to 10GB):
```dockerfile
FROM public.ecr.aws/lambda/python:3.12
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY src/ ${LAMBDA_TASK_ROOT}/
CMD ["handler.lambda_handler"]
```

---

## Validation Checklist

- [ ] Lambda function created with correct runtime and handler
- [ ] All environment variables set
- [ ] Function URL created and publicly invocable
- [ ] Telegram webhook registered and verified (`getWebhookInfo` shows no errors)
- [ ] Test message from Telegram reaches Lambda (check CloudWatch logs)
- [ ] Agent responds within 29 seconds (Lambda timeout)
- [ ] Deploy script works for subsequent deployments
