#!/usr/bin/env python3
"""Submit a prompt to the inference gateway and print its job ID."""
import json
import os
from urllib.request import Request, urlopen


if __name__ == "__main__":
    gateway = os.environ.get("GATEWAY_URL", "http://localhost:8080")
    payload = json.dumps({"prompt": "Explain scale-to-zero GPU inference.", "max_tokens": 80})
    request = Request(f"{gateway}/generate", data=payload.encode(), headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=10) as response:
        print(json.loads(response.read())["job_id"])
