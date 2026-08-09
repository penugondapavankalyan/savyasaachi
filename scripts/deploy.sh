#!/usr/bin/env bash
# ============================================================
# deploy.sh — package and deploy to AWS Lambda
# ============================================================
set -euo pipefail

FUNCTION_NAME="kirana-agent"
REGION="${AWS_REGION:-ap-south-1}"
PACKAGE_DIR="package"
ZIP_FILE="kirana-agent.zip"

echo "📦 Installing dependencies into $PACKAGE_DIR/ ..."
rm -rf "$PACKAGE_DIR" "$ZIP_FILE"
mkdir "$PACKAGE_DIR"
pip install -r requirements.txt -t "$PACKAGE_DIR/" -q

echo "📂 Copying source ..."
cp -r src/ "$PACKAGE_DIR/"

echo "🗜️  Creating zip ..."
cd "$PACKAGE_DIR" && zip -r "../$ZIP_FILE" . -q && cd ..

ZIP_SIZE=$(du -sh "$ZIP_FILE" | cut -f1)
echo "   Size: $ZIP_SIZE"

echo "🚀 Deploying to Lambda ($FUNCTION_NAME, $REGION) ..."

# Check if zip exceeds 50 MB (Lambda direct-upload limit)
ZIP_BYTES=$(stat -f%z "$ZIP_FILE" 2>/dev/null || stat -c%s "$ZIP_FILE")
LIMIT=$((50 * 1024 * 1024))

if [ "$ZIP_BYTES" -gt "$LIMIT" ]; then
    echo "⚠️  ZIP > 50 MB — uploading via S3 ..."
    BUCKET="${DEPLOY_S3_BUCKET:?Set DEPLOY_S3_BUCKET env var}"
    aws s3 cp "$ZIP_FILE" "s3://$BUCKET/$ZIP_FILE" --region "$REGION"
    aws lambda update-function-code \
        --function-name "$FUNCTION_NAME" \
        --s3-bucket "$BUCKET" \
        --s3-key "$ZIP_FILE" \
        --region "$REGION" \
        --query "FunctionArn" --output text
else
    aws lambda update-function-code \
        --function-name "$FUNCTION_NAME" \
        --zip-file "fileb://$ZIP_FILE" \
        --region "$REGION" \
        --query "FunctionArn" --output text
fi

echo "⏳ Waiting for update to complete ..."
aws lambda wait function-updated \
    --function-name "$FUNCTION_NAME" \
    --region "$REGION"

echo "✅ Deployed successfully!"

# Cleanup
rm -rf "$PACKAGE_DIR" "$ZIP_FILE"
