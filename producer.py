#!/usr/bin/env python3
"""Enqueue a prompt for the scale-to-zero inference worker."""
import json
import os
import socket


def redis_command(*parts: str) -> bytes:
    return ("*" + str(len(parts)) + "\r\n" + "".join(f"${len(p.encode())}\r\n{p}\r\n" for p in parts)).encode()


url = os.environ.get("REDIS_URL", "redis://localhost:6379").removeprefix("redis://")
host, _, port = url.partition(":")
payload = json.dumps({"prompt": "Explain scale-to-zero GPU inference.", "max_tokens": 80})
with socket.create_connection((host, int(port or 6379)), timeout=5) as connection:
    connection.sendall(redis_command("LPUSH", "inference-jobs", payload))
    print(connection.recv(128).decode().strip())
