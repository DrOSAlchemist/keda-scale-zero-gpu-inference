# Scale-to-Zero Inference: Maintainer Guide

This is the DrOSAlchemist queue-driven GPU inference example. Keep the implementation, README, and public claims aligned. Use `README.md` for setup, API examples, dashboard installation, and current deployment commands; this guide records design constraints and verification expectations.

## System Boundary

- `gateway.py`: FastAPI accepts a nonempty prompt at `POST /generate`, stores a job in Redis, and returns HTTP 202 with a `job_id`. `GET /result/{job_id}` polls for `pending`, `done`, or `error`; `GET /health` checks Redis.
- `worker.py`: moves jobs from `inference-jobs` to `inference-processing`, calls vLLM's `/v1/completions`, stores results for 300 seconds, and acknowledges work. Failed inference returns an error result. A failed Redis write leaves the item in the processing list for recovery.
- `manifests.yaml`: deploys the always-on gateway, Redis with an AOF-backed PVC, Redis exporter, model-cache PVC, and KEDA-scaled CPU worker and GPU vLLM pod. The GKE node pool is **not** created by Kubernetes manifests.
- `monitoring/`: Prometheus values and a 12-panel Grafana dashboard. The NVIDIA DCGM exporter is opt-in; see the README before installing one on GKE, which may already collect DCGM metrics.
- `scripts/`: guarded GKE deployment/teardown and an event/log/queue-based cold-warm-full-zero capture. Raw captures under `data/` are ignored by Git.

## Scaling And Recovery Rules

- KEDA watches both lists. `listLength: "5"` is a scaling target, **not** an activation floor: one queued prompt must wake the system. The processing-list trigger keeps an in-flight prompt from scaling the model down.
- Worker and vLLM each have `minReplicaCount: 0`, `maxReplicaCount: 1`. The single worker overlaps at most two HTTP completions so vLLM can batch them. Do not raise the worker replica ceiling without replacing the shared-list startup replay with recovery ownership that is safe across pods.
- The worker is at-least-once. A crash after inference but before acknowledgment can regenerate a response. Redis AOF and persistent storage reduce loss but are not an exactly-once or zero-loss guarantee.
- `result:{job_id}` expires after five minutes and the submitted-job marker after one hour. Clients must handle expiry and retries. The gateway has no public ingress, authentication, or admission control; do not expose it to untrusted traffic as-is.
- GKE Cluster Autoscaler reacts to the pending vLLM pod's `nvidia.com/gpu: 1` request when a compatible GPU pool with min size zero exists. The CPU node, control plane, disks, and monitoring still have costs at GPU zero.

## Cold-Start Experiments

Run the phases separately: an unseeded model cache, the optional PVC seed Job in `k8s-cloud/gcp/`, then optionally a GKE Secondary Boot Disk built using Google's documented tooling. `DISK_IMAGE` in the guarded deployment script attaches an **existing** image to a distinct GPU pool; it does not build the image. Rebuild an image and use a new pool when the vLLM image tag changes.

Use `scripts/full-cycle-run.sh` only after confirming zero worker/vLLM replicas and zero nodes in the selected GPU pool. Capture queue arrival, pod scheduling, GPU node creation, image pull, vLLM readiness, cold/warm completion, pod cooldown, and node removal. Compare runs with the same model, workload, zone, GPU and PVC state. Spot eviction is a confounder and should be reported, not hidden.

No live GKE full-cycle benchmark or cold-start improvement has been established in this repository. Do not publish the reference project's timings, throughput, prices, uptime, dashboard screenshot, or Spot recovery event as results of this project. An empty dashboard panel is not evidence of a measured zero; verify scrape targets and exporter availability first.

## Workflows And Verification

1. Local tests: `PYTHONPATH=. .venv/bin/python -m unittest discover -s tests -v`. Python 3.12+ and the dependencies in `requirements.txt` plus `httpx` are required.
2. Before changing manifests, parse all YAML documents and check KEDA resource names, queue keys, GPU requests, namespace DNS, and PVC mounts. Run `bash -n scripts/*.sh` after shell edits. Use `git diff --check` before publishing.
3. The cloud scripts require explicit `PROJECT_ID` and `CONFIRM_BILLING=yes` or `CONFIRM_DELETE=<cluster>`. Never run or relax those guards as part of a local code check. GKE, Docker/GPU inference, DCGM collection, and cold-start performance need a real cluster to validate.
4. If code changes affect queue semantics, update both the focused tests and README architecture diagram. Keep the project on the existing FastAPI + Redis + KEDA + vLLM path unless a change has a clear, tested reason.

Never put cloud project credentials, access tokens, private prompts, unreviewed performance numbers, or captured runtime logs in source control. The upstream example is an architectural reference, not a source of measured results or text to reproduce.