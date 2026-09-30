"""Queue inference requests and expose pollable results."""
import json
import os
import uuid

import redis
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


app = FastAPI(title="Scale-to-zero inference")
client = redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379"), decode_responses=True)


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=8192)
    max_tokens: int = Field(default=80, ge=1, le=1024)


@app.get("/health")
def health() -> dict:
    try:
        client.ping()
    except redis.RedisError as error:
        raise HTTPException(status_code=503, detail="Redis unavailable") from error
    return {"status": "ok"}


@app.post("/generate", status_code=202)
def generate(request: GenerateRequest) -> dict:
    job_id = uuid.uuid4().hex
    job = {"job_id": job_id, **request.model_dump()}
    try:
        with client.pipeline() as pipeline:
            pipeline.setex(f"job:{job_id}", 3600, "submitted")
            pipeline.lpush("inference-jobs", json.dumps(job))
            pipeline.execute()
    except redis.RedisError as error:
        raise HTTPException(status_code=503, detail="Redis unavailable") from error
    return {"job_id": job_id}


@app.get("/result/{job_id}")
def result(job_id: str) -> dict:
    try:
        if len(job_id) != 32 or any(character not in "0123456789abcdef" for character in job_id):
            raise HTTPException(status_code=404, detail="Unknown job")
        value = client.get(f"result:{job_id}")
        if value is not None:
            return json.loads(value)
        if client.exists(f"job:{job_id}"):
            return {"status": "pending"}
    except redis.RedisError as error:
        raise HTTPException(status_code=503, detail="Redis unavailable") from error
    raise HTTPException(status_code=404, detail="Unknown or expired job")