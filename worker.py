#!/usr/bin/env python3
"""Consume queued completion requests and store their results in Redis."""
import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import redis


QUEUE = "inference-jobs"
PROCESSING = "inference-processing"
RESULT_TTL = 300


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


def run(client: redis.Redis) -> None:
    while client.rpoplpush(PROCESSING, QUEUE) is not None:
        pass
    while True:
        payload = client.brpoplpush(QUEUE, PROCESSING, timeout=0)
        job = None
        try:
            job = json.loads(payload)
            completion = infer(job)
            result = {"status": "done", "response": completion["choices"][0]["text"]}
        except Exception as error:
            if not isinstance(job, dict) or "job_id" not in job:
                client.lpush("inference-dead-letter", payload)
                client.lrem(PROCESSING, 1, payload)
                continue
            result = {"status": "error", "error": str(error)}
        with client.pipeline() as pipeline:
            pipeline.setex(f"result:{job['job_id']}", RESULT_TTL, json.dumps(result))
            pipeline.lrem(PROCESSING, 1, payload)
            pipeline.execute()


if __name__ == "__main__":
    client = redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379"), decode_responses=True)
    while True:
        try:
            run(client)
        except (redis.RedisError, OSError) as error:
            print(f"Redis unavailable: {error}", flush=True)
            time.sleep(2)
