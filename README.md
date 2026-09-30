# KEDA Scale-to-Zero GPU Inference

An asynchronous inference example: the gateway accepts a prompt and returns a job ID, Redis buffers it, a CPU worker sends it to vLLM, and the result can be polled. KEDA watches both queued and in-flight jobs to scale the worker and GPU-backed vLLM Deployment from zero. A **separately configured** Kubernetes node autoscaler must provision and retire GPU nodes. This repository has not been benchmarked on GKE; timings and cost estimates from other projects are not measurements of this implementation.

```text
POST /generate -> gateway -> Redis inference-jobs -> CPU worker -> vLLM (GPU)
GET /result/{id} <- Redis result:{id}         KEDA -> pods -> node autoscaler
```

The worker atomically moves a job into `inference-processing` before inference. On restart it requeues unfinished jobs, and both KEDA ScaledObjects watch that list so a stranded job wakes the worker and vLLM again. Result storage and acknowledgment share a Redis transaction. Processing is **at least once**: a crash after inference can repeat a completion. Redis AOF and a PVC reduce, but do not eliminate, storage loss. The gateway is internal-only; do not expose it publicly without authentication, TLS and rate limits.

## Local API Mode

Requires Python 3.12+, Docker with a running daemon, and a reachable OpenAI-compatible `/v1/completions` endpoint. A macOS laptop without an NVIDIA GPU cannot run the provided vLLM GPU Deployment locally. Local mode runs the worker continuously; it does not exercise Kubernetes autoscaling.

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
docker run -d --name inference-redis -p 6379:6379 redis:7.4 --appendonly yes
.venv/bin/uvicorn gateway:app --host 127.0.0.1 --port 8080
```

In a second terminal, run the worker with a reachable inference endpoint:

```bash
REDIS_URL=redis://localhost:6379 VLLM_URL=http://localhost:8000/v1/completions .venv/bin/python worker.py
```

In a third terminal:

```bash
.venv/bin/python producer.py
curl http://localhost:8080/result/JOB_ID
.venv/bin/python load_test.py --requests 10 --concurrency 5 --timeout 1200
```

`POST /generate` accepts `{"prompt":"Explain autoscaling","max_tokens":80}` and returns HTTP 202 with `{"job_id":"..."}`. `GET /result/{job_id}` returns `pending`, `done` with `response`, or `error` with `error`; unknown or expired IDs return 404. Completed results expire after 300 seconds; the submission marker expires after one hour. `GET /health` checks Redis connectivity. An unavailable vLLM endpoint is retried for up to 15 minutes to accommodate GPU cold starts.

## Kubernetes / GKE

The manifest assumes an existing GPU-capable cluster with a default dynamic StorageClass, NVIDIA GPU drivers/device plugin, and KEDA installed. It creates one persistent Redis pod, a gateway, a CPU worker, a GPU vLLM pod, a Redis exporter, two PVCs, and two ScaledObjects in the **`llm-inference` namespace**. The CPU control plane, Redis, and persistent disks remain billed while GPU nodes are at zero. The model cache PVC is populated by vLLM on first start; it survives pod and node churn, but the first run still downloads the weights. Keep GPU nodes and zonal volumes in a compatible zone.

For a GKE example, first verify GPU quota, regional availability, cluster costs, storage provisioning, and permission to create Spot nodes. Use a CPU pool for KEDA, Redis and the gateway, plus an autoscaled T4 pool with minimum size zero:

```bash
gcloud container clusters create inference-demo --zone us-east1-d --machine-type e2-standard-4 --num-nodes 1
gcloud container node-pools create gpu-pool --cluster inference-demo --zone us-east1-d \
	--machine-type n1-standard-4 --accelerator type=nvidia-tesla-t4,count=1,gpu-driver-version=default \
	--spot --enable-autoscaling --min-nodes 0 --max-nodes 1 --num-nodes 0
gcloud container clusters get-credentials inference-demo --zone us-east1-d
helm repo add kedacore https://kedacore.github.io/charts
helm repo update
helm upgrade --install keda kedacore/keda --namespace keda --create-namespace
kubectl create namespace llm-inference
```

Check `kubectl get nodes -o json` for `nvidia.com/gpu` allocatable when a GPU node is present. The vLLM pod requests one GPU and tolerates the common GKE GPU taint; adjust machine sizing, tolerations and storage class for other clusters. Kubernetes manifests do **not** create node pools, GPU drivers, or the node autoscaler.

Build and publish the application image, replace `ghcr.io/your-org/scale-zero-inference:latest` in both Deployments with that accessible image, then apply:

```bash
docker build -t ghcr.io/YOUR_ORG/scale-zero-inference:YOUR_TAG .
docker push ghcr.io/YOUR_ORG/scale-zero-inference:YOUR_TAG
kubectl apply -n llm-inference -f manifests.yaml
kubectl rollout status -n llm-inference deployment/redis
kubectl rollout status -n llm-inference deployment/gateway
kubectl port-forward -n llm-inference service/gateway 8080:8080
```

The example is limited to one CPU worker and one vLLM pod because the shared processing-list recovery assumes one worker replica. vLLM supports continuous batching, but this sequential consumer does not saturate it; multi-worker throughput requires per-worker recovery ownership or a proper consumer-group queue. `maxReplicaCount: 1` is intentional, not a capacity benchmark. The gateway does not impose queue-depth limits, so add admission controls before exposing it to untrusted traffic.

## Exercise the Scale Cycle

In another terminal, while port-forwarding:

```bash
.venv/bin/python load_test.py --gateway http://localhost:8080 --requests 10 --concurrency 5 --timeout 1200
kubectl get pods -n llm-inference -w
kubectl get nodes -w
kubectl get scaledobjects -n llm-inference
kubectl get events -n llm-inference --sort-by=.lastTimestamp
```

Watch for KEDA activation, the pending GPU pod, node provisioning, vLLM readiness, results, the 300-second KEDA cooldown, and eventual node removal. Load-test output reports completed, errored, timed-out and mean/max end-to-end durations; it does not claim to measure token throughput or time to first token. Spot eviction can cause a job to run again after restart; inspect `kubectl logs -n llm-inference deployment/inference-worker` and Redis list lengths when debugging.

For a timestamped cold/warm/cooldown run, leave the gateway port-forward active and start at zero worker and vLLM replicas **and** zero GPU nodes:

```bash
bash scripts/full-cycle-run.sh http://localhost:8080
```

The script writes namespace events, node watch output, pod/node lifecycle snapshots throughout the run, two load summaries and a timestamped timeline under `data/run-*/` (ignored by Git). `GPU_POOL` selects the GKE pool label (default `gpu-pool`); `TIMEOUT_SECONDS` bounds the scale-down wait (default 2400). It exits nonzero if the cluster is not initially at zero, any job fails, or it never returns to zero. It does not provision or tear down cloud resources. Avoid concurrent deployments or other workloads on the GPU pool during the measurement.

## Metrics And Cold Starts

The Redis exporter exposes queue key sizes on port 9121; vLLM exposes its built-in Prometheus endpoint on port 8000. With the Prometheus Operator installed, an example kube-prometheus-stack configuration is provided:

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
helm upgrade --install monitoring prometheus-community/kube-prometheus-stack \
	--namespace monitoring --create-namespace -f monitoring/prometheus-values.yaml
kubectl port-forward -n monitoring service/monitoring-grafana 3000:80
```

In Grafana, inspect `redis_key_size{key="inference-jobs"}`, `redis_key_size{key="inference-processing"}`, `vllm:num_requests_running` and kube-state-metrics deployment/node metrics. Confirm actual exported metric names in Prometheus before building panels; exporter versions may differ. DCGM GPU utilization/power/VRAM metrics require an additional compatible NVIDIA DCGM exporter installation on GPU nodes. The example chart config retains metrics for 24 hours in ephemeral storage; use durable remote storage for long-running monitoring.

The model PVC avoids downloading weights after the first successful start, but the large vLLM image must still be pulled on a new GPU node. GKE Secondary Boot Disk image caching is a possible **optional** optimization; building and attaching a cache image is not implemented here. Record queue-to-ready, image-pull and model-load times separately before claiming cold-start gains. For the disposable cluster created above, remove it after the experiment (including its node pools); check for any retained volumes or external monitoring resources that still incur charges:

```bash
gcloud container clusters delete inference-demo --zone us-east1-d
```

## Tests

```bash
uv pip install --python .venv/bin/python 'httpx>=0.27,<1'
PYTHONPATH=. .venv/bin/python -m unittest discover -s tests -v
```
