#!/usr/bin/env python3
"""Consume queued completion requests and store their results in Redis."""
import json
import os
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import redis
from prometheus_client import Counter, Histogram, start_http_server


QUEUE = "inference-jobs"
PROCESSING = "inference-processing"
RESULT_TTL = 300
JOBS = Counter("inference_jobs_total", "Processed inference jobs", ["status"])
JOB_DURATION = Histogram(
    "inference_job_duration_seconds", "Time from queue admission to stored result", ["status"],
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1200, 2400),
)


def infer(job: dict) -> dict:
    endpoint = os.environ["VLLM_URL"]
    request = Request(
        endpoint,
        data=json.dumps({
            "model": os.environ.get("MODEL_ID", "Qwen/Qwen2.5-1.5B-Instruct"),
            "prompt": job["prompt"],
            "max_tokens": job.get("max_tokens", 80),
        }).encode(),
        headers={"Content-Type": "application/json"},
    )
    deadline = time.monotonic() + 900
    while True:
        try:
            with urlopen(request, timeout=120) as response:
                return json.loads(response.read())
        except HTTPError as error:
            if error.code < 500 or time.monotonic() >= deadline:
                raise
        except (URLError, TimeoutError):
            if time.monotonic() >= deadline:
                raise
        time.sleep(5)


def process_job(client: redis.Redis, payload: str) -> None:
    job = None
    try:
        job = json.loads(payload)
        completion = infer(job)
        result = {"status": "done", "response": completion["choices"][0]["text"]}
    except Exception as error:
        if not isinstance(job, dict) or "job_id" not in job:
            client.lpush("inference-dead-letter", payload)
            client.lrem(PROCESSING, 1, payload)
            JOBS.labels("invalid").inc()
            return
        result = {"status": "error", "error": str(error)}
    with client.pipeline() as pipeline:
        pipeline.setex(f"result:{job['job_id']}", RESULT_TTL, json.dumps(result))
        pipeline.lrem(PROCESSING, 1, payload)
        pipeline.execute()
    JOBS.labels(result["status"]).inc()
    queued_at = job.get("queued_at")
    if isinstance(queued_at, (int, float)):
        JOB_DURATION.labels(result["status"]).observe(max(0, time.time() - queued_at))


def run(client: redis.Redis) -> None:
    while client.rpoplpush(PROCESSING, QUEUE) is not None:
        pass
    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = set()
        while True:
            if pending:
                finished, pending = wait(pending, timeout=0 if len(pending) < 2 else None, return_when=FIRST_COMPLETED)
                for future in finished:
                    future.result()
            payload = client.brpoplpush(QUEUE, PROCESSING, timeout=5)
            if payload is not None:
                pending.add(executor.submit(process_job, client, payload))


if __name__ == "__main__":
    start_http_server(9100)
    client = redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379"), decode_responses=True)
    while True:
        try:
            run(client)
        except (redis.RedisError, OSError) as error:
            print(f"Redis unavailable: {error}", flush=True)
            time.sleep(2)