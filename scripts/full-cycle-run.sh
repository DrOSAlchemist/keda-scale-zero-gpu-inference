#!/usr/bin/env bash
set -euo pipefail

NAMESPACE=llm-inference
GATEWAY_URL=${1:-http://localhost:8080}
GPU_POOL=${GPU_POOL:-gpu-pool}
TIMEOUT_SECONDS=${TIMEOUT_SECONDS:-2400}
PAUSE_SECONDS=${PAUSE_SECONDS:-60}
PYTHON=${PYTHON:-.venv/bin/python}
RUN_DIR="data/run-$(date -u +%Y%m%d-%H%M%S)"
mkdir -p "$RUN_DIR"
START=$(date +%s)

replicas() {
  kubectl -n "$NAMESPACE" get deployment "$1" -o jsonpath='{.spec.replicas}'
}

gpu_nodes() {
  kubectl get nodes -l "cloud.google.com/gke-nodepool=$GPU_POOL" -o name | wc -l | tr -d ' '
}

queue_depth() {
  kubectl -n "$NAMESPACE" exec deployment/redis -- redis-cli LLEN "$1"
}

record() {
  printf 'T+%ss | %s | %s\n' "$(( $(date +%s) - START ))" "$(date -u +%FT%TZ)" "$1" | tee -a "$RUN_DIR/timeline.log"
}

if [[ $(replicas vllm) != 0 || $(replicas inference-worker) != 0 || $(gpu_nodes) != 0 ]]; then
  echo 'Expected zero vLLM/worker replicas and zero GPU nodes; wait for cooldown before a cold run.' >&2
  exit 1
fi
curl --fail --silent --show-error "$GATEWAY_URL/health" > /dev/null
queue_depth inference-jobs > /dev/null

kubectl -n "$NAMESPACE" get events --watch -o wide > "$RUN_DIR/events.log" 2>&1 &
events_pid=$!
kubectl -n "$NAMESPACE" get pods --watch -o wide > "$RUN_DIR/pod-events.log" 2>&1 &
pods_pid=$!
kubectl get nodes --watch -o wide > "$RUN_DIR/node-events.log" 2>&1 &
nodes_pid=$!
kubectl -n "$NAMESPACE" logs -f deployment/inference-worker --pod-running-timeout=15m > "$RUN_DIR/worker-output.log" 2>&1 &
worker_log_pid=$!
kubectl -n "$NAMESPACE" logs -f deployment/vllm --pod-running-timeout=15m > "$RUN_DIR/vllm-output.log" 2>&1 &
vllm_log_pid=$!
(
  last_ready=0
  last_nodes=0
  while true; do
    ready=$(kubectl -n "$NAMESPACE" get deployment vllm -o jsonpath='{.status.readyReplicas}')
    node_count=$(gpu_nodes)
    printf '%s worker=%s vllm=%s gpu_nodes=%s\n' \
      "$(date -u +%FT%TZ)" "$(replicas inference-worker)" "$(replicas vllm)" "$node_count" >> "$RUN_DIR/lifecycle.log"
    printf '%s waiting=%s processing=%s\n' \
      "$(date -u +%FT%TZ)" "$(queue_depth inference-jobs)" "$(queue_depth inference-processing)" >> "$RUN_DIR/redis-queue.log"
    if [[ $ready == 1 && $last_ready != 1 ]]; then record 'vLLM READY'; fi
    if [[ $node_count != "$last_nodes" ]]; then record "GPU NODES = $node_count"; fi
    last_ready=${ready:-0}
    last_nodes=$node_count
    sleep 15
  done
) &
sampler_pid=$!
cleanup() {
  kill "$events_pid" "$pods_pid" "$nodes_pid" "$worker_log_pid" "$vllm_log_pid" "$sampler_pid" 2>/dev/null || true
  wait "$events_pid" "$pods_pid" "$nodes_pid" "$worker_log_pid" "$vllm_log_pid" "$sampler_pid" 2>/dev/null || true
}
trap cleanup EXIT

record 'COLD START - submitting 10 requests'
"$PYTHON" load_test.py --gateway "$GATEWAY_URL" --requests 10 --concurrency 5 --timeout 1200 | tee "$RUN_DIR/cold-results.json"
record 'COLD DONE'
record "BASELINE PAUSE - ${PAUSE_SECONDS}s"
sleep "$PAUSE_SECONDS"
record 'WARM START - submitting 30 requests'
"$PYTHON" load_test.py --gateway "$GATEWAY_URL" --requests 30 --concurrency 5 --timeout 1200 | tee "$RUN_DIR/warm-results.json"
record 'WARM DONE - waiting for pods and node to scale down'

deadline=$(( $(date +%s) + TIMEOUT_SECONDS ))
while (( $(date +%s) < deadline )); do
  worker_count=$(replicas inference-worker)
  vllm_count=$(replicas vllm)
  node_count=$(gpu_nodes)
  if [[ $worker_count == 0 && $vllm_count == 0 && $node_count == 0 ]]; then
    record 'FULL ZERO - pods and GPU node removed'
    exit 0
  fi
  sleep 15
done

record 'TIMEOUT - full zero not observed'
exit 1