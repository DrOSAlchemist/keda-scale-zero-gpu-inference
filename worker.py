#!/usr/bin/env python3
"""Minimal queue-worker skeleton; package redis/http dependencies for production."""
import json
import os
from urllib.request import Request, urlopen


def infer(job: dict) -> dict:
    endpoint = os.environ["VLLM_URL"]
    request = Request(endpoint, data=json.dumps(job).encode(), headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=120) as response:
        return json.loads(response.read())


if __name__ == "__main__":
    print("Worker image should BRPOP inference-jobs and call infer(job).")
    print("Configured endpoint:", os.environ.get("VLLM_URL", "not set"))
