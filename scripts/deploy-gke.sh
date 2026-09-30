#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
PROJECT_ID=${PROJECT_ID:?Set PROJECT_ID to the GCP project you will be billed in}
ZONE=${ZONE:-us-east1-d}
REGION=${ZONE%-*}
CLUSTER=${CLUSTER:-inference-demo}
REGISTRY=${REGISTRY:-inference-demo}
IMAGE_TAG=$(git rev-parse --short HEAD)
GATEWAY_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REGISTRY}/gateway:${IMAGE_TAG}"
WORKER_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REGISTRY}/worker:${IMAGE_TAG}"
GPU_POOL=gpu-pool
if [[ -n ${DISK_IMAGE:-} ]]; then
  [[ $DISK_IMAGE =~ ^[a-z][a-z0-9-]*$ ]] || { echo 'DISK_IMAGE must be a disk image name in this project' >&2; exit 1; }
  [[ ${#DISK_IMAGE} -le 30 ]] || { echo 'DISK_IMAGE must be 30 characters or fewer for the node pool name' >&2; exit 1; }
  GPU_POOL="gpu-cache-$DISK_IMAGE"
fi

if [[ ${CONFIRM_BILLING:-} != yes ]]; then
  echo "This creates billed GKE, CPU/GPU nodes, Artifact Registry and disks in $PROJECT_ID. Set CONFIRM_BILLING=yes to continue." >&2
  exit 1
fi
for tool in gcloud kubectl helm docker; do
  command -v "$tool" > /dev/null || { echo "Missing $tool" >&2; exit 1; }
done
gcloud services enable container.googleapis.com artifactregistry.googleapis.com --project "$PROJECT_ID"
if [[ -n ${DISK_IMAGE:-} ]]; then
  gcloud services enable compute.googleapis.com containerfilesystem.googleapis.com --project "$PROJECT_ID"
  gcloud compute images describe "$DISK_IMAGE" --project "$PROJECT_ID" > /dev/null
fi
if ! gcloud artifacts repositories describe "$REGISTRY" --project "$PROJECT_ID" --location "$REGION" > /dev/null 2>&1; then
  gcloud artifacts repositories create "$REGISTRY" --project "$PROJECT_ID" --location "$REGION" --repository-format docker
fi
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet
docker build -t "$GATEWAY_IMAGE" gateway/
docker build -t "$WORKER_IMAGE" worker/
docker push "$GATEWAY_IMAGE"
docker push "$WORKER_IMAGE"

if ! gcloud container clusters describe "$CLUSTER" --project "$PROJECT_ID" --zone "$ZONE" > /dev/null 2>&1; then
  cluster_options=()
  if [[ -n ${DISK_IMAGE:-} ]]; then cluster_options+=(--enable-image-streaming); fi
  gcloud container clusters create "$CLUSTER" --project "$PROJECT_ID" --zone "$ZONE" \
    --machine-type e2-standard-4 --num-nodes 1 "${cluster_options[@]}"
elif [[ -n ${DISK_IMAGE:-} ]]; then
  gcloud container clusters update "$CLUSTER" --project "$PROJECT_ID" --zone "$ZONE" --enable-image-streaming
fi
if ! gcloud container node-pools describe "$GPU_POOL" --cluster "$CLUSTER" --project "$PROJECT_ID" --zone "$ZONE" > /dev/null 2>&1; then
  disk_options=()
  if [[ -n ${DISK_IMAGE:-} ]]; then
    disk_options+=(--enable-image-streaming "--secondary-boot-disk=disk-image=global/images/$DISK_IMAGE,mode=CONTAINER_IMAGE_CACHE")
  fi
  gcloud container node-pools create "$GPU_POOL" --cluster "$CLUSTER" --project "$PROJECT_ID" --zone "$ZONE" \
    --machine-type n1-standard-4 --accelerator type=nvidia-tesla-t4,count=1,gpu-driver-version=default \
    --spot --enable-autoscaling --min-nodes 0 --max-nodes 1 --num-nodes 0 "${disk_options[@]}"
fi
gcloud container clusters get-credentials "$CLUSTER" --project "$PROJECT_ID" --zone "$ZONE"
helm repo add kedacore https://kedacore.github.io/charts --force-update
helm repo update
helm upgrade --install keda kedacore/keda --namespace keda --create-namespace --wait
kubectl create namespace llm-inference --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -k k8s/
kubectl set image -n llm-inference deployment/gateway "gateway=$GATEWAY_IMAGE"
kubectl set image -n llm-inference deployment/inference-worker "worker=$WORKER_IMAGE"
if [[ -n ${DISK_IMAGE:-} ]]; then
  kubectl patch -n llm-inference deployment/vllm --type merge \
    -p "{\"spec\":{\"template\":{\"spec\":{\"nodeSelector\":{\"cloud.google.com/gke-nodepool\":\"$GPU_POOL\"}}}}"
fi
kubectl rollout status -n llm-inference deployment/redis --timeout=5m
kubectl rollout status -n llm-inference deployment/gateway --timeout=5m
printf 'Gateway: kubectl port-forward -n llm-inference service/gateway 8080:8080\nGateway image: %s\nWorker image: %s\n' "$GATEWAY_IMAGE" "$WORKER_IMAGE"