# KEDA Scale-to-Zero GPU Inference

Queue-driven inference architecture: producers enqueue prompts in Redis, KEDA scales the GPU worker from zero based on queue depth, and the worker sends work to a local or remote vLLM-compatible endpoint.

## Architecture

```text
producer -> Redis list -> KEDA ScaledObject -> GPU worker -> vLLM endpoint
```

## Local learning mode

Run Redis locally, then enqueue example work:

```bash
docker run --rm -p 6379:6379 redis:7
REDIS_URL=redis://localhost:6379 python3 producer.py
```

The Kubernetes manifest is a cluster template: provide your image, GPU node pool, Redis host, and KEDA installation before applying it.
