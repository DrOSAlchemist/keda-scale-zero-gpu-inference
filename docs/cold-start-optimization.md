# Cold-Start Optimization: Research, Plan And Results

**Status:** Implemented and benchmarked on GKE T4 Spot in `us-east1-d` on 2026-04-05.

**Measured result:** queue spike to first token fell from 659 seconds (11 minutes) to 338 seconds (5.6 minutes), a 48% reduction.

This record explains why the vLLM image was large, which approaches were tested, and why persistent model storage plus a GKE Secondary Boot Disk were selected.

## The bottleneck

The original cold start was dominated by image transfer and extraction:

| Phase | Approximate time |
| --- | ---: |
| GKE provisions the GPU VM (`n1-standard-4` with a T4 Spot GPU) | 2.5 min |
| GPU node pulls the 11 GB image from Artifact Registry | 6.5 min |
| vLLM starts and loads model weights into VRAM | 2 min |
| **Total** | **659 s / 11 min** |

The image pull measured about 28 MB/s. The limiting factor was containerd's default three-layer download concurrency combined with CPU decompression on a four-vCPU node, rather than the node's advertised network bandwidth. GKE does not expose a configuration knob for `max_concurrent_downloads`.

The image was large because the custom vLLM image included both the vLLM/PyTorch/CUDA runtime, approximately 8 GB, and the approximately 3.5 GB Qwen2.5-1.5B weights. Baking the model into the image avoided a roughly 29-second Hugging Face download but added much more time to every new-node image pull.

## Chosen design

### Persistent model cache

The model is stored on a 10 Gi persistent volume instead of being baked into the image. The repository's `k8s/vllm-pvc.yaml` mounts the cache at `/models`, and `k8s-cloud/gcp/model-cache-seed.yaml` can populate it before a cold-start comparison. The vLLM deployment uses `HF_HOME=/models` and the stock `vllm/vllm-openai:v0.10.2` image.

The first seeded deployment downloads the model from Hugging Face. Later pod and node churn reuses the weights from the PVC. This changes the image that must be cached from roughly 11 GB to roughly 8 GB; it does not make model loading into GPU memory free.

### GKE Secondary Boot Disk

The measured run used a GKE Secondary Boot Disk image named `vllm-node-cache-20260405`, built with the GKE disk-image-builder workflow. The disk contains the vLLM container layers in the container image cache. A GPU node pool attaches the image in `CONTAINER_IMAGE_CACHE` mode, so containerd reads the cached image locally during boot rather than pulling all layers from Artifact Registry.

This differs from GKE Image Streaming. Image Streaming can make container startup appear fast while Python and CUDA imports continue to trigger many lazy remote reads. The secondary disk supplies the cached data from an attached local disk instead.

The operational tradeoff is version maintenance: build a new disk image and create a new GPU pool when the vLLM image changes, then drain and remove the old pool.

## Measured result

The combined PV plus Secondary Boot Disk run was measured on a T4 Spot GPU with `n1-standard-4` in `us-east1-d`.

| Phase | Baseline: 11 GB baked image | PV + Secondary Boot Disk |
| --- | ---: | ---: |
| GPU node provisioning | ~2.5 min | ~2.5 min |
| Image availability | ~6.5 min pull | ~30 s container start and scheduling |
| vLLM boot and model load | ~2 min | ~2.5 min |
| **Queue spike to first token** | **659 s** | **338 s** |

The measured improvement was approximately 321 seconds, or 48%. The PV-only column is not an isolated benchmark; it is an estimate derived from the smaller image and the observed model-cache load behavior. The two optimizations were measured together.

The remaining 5.6 minutes are mainly GPU VM bring-up and loading the 3.5 GB model from network-attached persistent storage into VRAM. Reducing that further would require keeping a GPU node warm or adding another local model-storage layer, both of which add cost or operational complexity.

## Approaches evaluated

| Approach | Decision | Reason |
| --- | --- | --- |
| Persistent volume for model weights | Build | Removes model weights from the image and survives node deletion |
| GKE Secondary Boot Disk | Build | Preloads image layers locally on new GPU nodes |
| eStargz or Stargz Snapshotter | Reject | Managed GKE containerd does not expose the required custom plugin path |
| GKE Image Streaming | Reject | Lazy remote reads slowed Python and CUDA initialization |
| DaemonSet pre-pull | Reject | The cache disappears with a scale-to-zero node |
| Artifact Registry tuning or layer splitting | Reject | Did not address the node-lifetime cache problem |
| Minimum one GPU node | Reject | Eliminates cold start but carries continuous GPU cost |
| Ollama with GGUF | Reject | Changes the vLLM-focused serving design |

## Historical L4 context

Before moving to the T4 Spot pool, two unoptimized runs on `g2-standard-4` with an NVIDIA L4 in `us-central1-a` took approximately 9 to 9.5 minutes. No optimized L4 run was measured. A Prometheus trace from the later L4 baseline showed node availability at 23:50:38, image pull completion and vLLM initialization around 23:58:58, model VRAM allocation around 23:59:28, and first tokens around 23:59:38.

The short inference burst did not line up with a 15-second GPU utilization scrape. Power draw and VRAM allocation confirmed that the GPU was active; a zero utilization sample was not evidence of an idle GPU.

## Reproduction notes

The repository includes the application and Kubernetes pieces needed to reproduce the architecture:

```bash
kubectl apply -k k8s/
kubectl apply -n llm-inference -f k8s-cloud/gcp/model-cache-seed.yaml
bash scripts/full-cycle-run.sh http://localhost:8080
```

The guarded GKE deployment script accepts an existing disk image through `DISK_IMAGE`. Building the Secondary Boot Disk is a separate cloud operation and is intentionally not hidden inside the application deployment script. Keep the model, workload, GPU type, zone, PVC state and image version constant when comparing runs. Record queue arrival, node provisioning, image availability, vLLM readiness, model load and first token separately.

Cloud performance and cost claims in this note describe the dated benchmark above. They are not guarantees for other regions, GPU types, image versions or cluster configurations. The Secondary Boot Disk itself was measured; DCGM dashboard data, cache-hit telemetry and any further local-storage optimization remain environment-specific checks.
