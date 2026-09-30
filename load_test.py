"""Submit concurrent jobs and measure end-to-end completion latency."""
import argparse
import json
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.request import Request, urlopen


def submit_and_wait(gateway: str, prompt: str, timeout: int) -> tuple[str, float]:
    start = time.monotonic()
    request = Request(
        f"{gateway}/generate",
        data=json.dumps({"prompt": prompt, "max_tokens": 80}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=10) as response:
        job_id = json.load(response)["job_id"]
    while time.monotonic() - start < timeout:
        with urlopen(f"{gateway}/result/{job_id}", timeout=10) as response:
            result = json.load(response)
        if result["status"] != "pending":
            return result["status"], time.monotonic() - start
        time.sleep(2)
    return "timeout", time.monotonic() - start


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gateway", default="http://localhost:8080")
    parser.add_argument("--requests", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--timeout", type=int, default=1200)
    args = parser.parse_args()
    if min(args.requests, args.concurrency, args.timeout) < 1:
        parser.error("requests, concurrency and timeout must be positive")
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        jobs = [pool.submit(submit_and_wait, args.gateway, "Explain GPU autoscaling.", args.timeout)
                for _ in range(args.requests)]
        results = [future.result() for future in as_completed(jobs)]
    durations = [duration for status, duration in results if status == "done"]
    print(json.dumps({
        "submitted": len(results),
        "completed": len(durations),
        "failed": sum(status == "error" for status, _ in results),
        "timed_out": sum(status == "timeout" for status, _ in results),
        "mean_seconds": round(statistics.mean(durations), 2) if durations else None,
        "max_seconds": round(max(durations), 2) if durations else None,
    }, indent=2))
    if len(durations) != len(results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()