#!/bin/bash
# ============================================================
# Deploy ZettaBrain Trial Proxy to Google Cloud Run
# ============================================================
# Prerequisites:
#   1. gcloud CLI installed and authenticated
#   2. A GCP project with billing enabled
#   3. Your Gemini API key
#
# Usage:
#   ./deploy.sh
# ============================================================

set -e

PROJECT_ID=$(gcloud config get-value project 2>/dev/null)
REGION="us-central1"
SERVICE_NAME="zettabrain-trial-proxy"
IMAGE="gcr.io/${PROJECT_ID}/${SERVICE_NAME}"

if [ -z "$PROJECT_ID" ]; then
  echo "Error: No GCP project set. Run: gcloud config set project YOUR_PROJECT_ID"
  exit 1
fi

echo ""
echo "Deploying ZettaBrain Trial Proxy"
echo "  Project:  $PROJECT_ID"
echo "  Region:   $REGION"
echo "  Service:  $SERVICE_NAME"
echo ""

# Enable required APIs
echo "Enabling Cloud Run and Container Registry APIs..."
gcloud services enable run.googleapis.com containerregistry.googleapis.com --quiet

# Build and push container
echo "Building container image..."
gcloud builds submit --tag "$IMAGE" --quiet

# Deploy to Cloud Run
echo "Deploying to Cloud Run..."
gcloud run deploy "$SERVICE_NAME" \
  --image "$IMAGE" \
  --region "$REGION" \
  --platform managed \
  --allow-unauthenticated \
  --memory 256Mi \
  --cpu 1 \
  --min-instances 0 \
  --max-instances 3 \
  --set-env-vars "MAX_REQUESTS=5" \
  --quiet

# Get the service URL
SERVICE_URL=$(gcloud run services describe "$SERVICE_NAME" --region "$REGION" --format "value(status.url)")

echo ""
echo "============================================"
echo "  Deployed successfully!"
echo "  URL: ${SERVICE_URL}"
echo "============================================"
echo ""
echo "Next steps:"
echo "  1. Set your Gemini API key:"
echo "     gcloud run services update $SERVICE_NAME --region $REGION --set-env-vars GEMINI_API_KEY=your-key-here"
echo ""
echo "  2. Update TRIAL_PROXY_URL in zettabrain_lite/trial.py:"
echo "     TRIAL_PROXY_URL = \"${SERVICE_URL}/v1\""
echo ""
echo "  3. Test it:"
echo "     curl ${SERVICE_URL}/health"
echo ""
