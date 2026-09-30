import json
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import redis
from fastapi.testclient import TestClient

from gateway import gateway
from worker import worker


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(gateway.app)

    @patch.object(gateway, "client")
    def test_submit_and_poll(self, redis_client):
        response = self.client.post("/generate", json={"prompt": "hello"})
        self.assertEqual(response.status_code, 202)
        job_id = response.json()["job_id"]
        redis_client.pipeline.return_value.__enter__.return_value.lpush.assert_called_once()
        queued = redis_client.pipeline.return_value.__enter__.return_value.lpush.call_args.args[1]
        self.assertEqual(json.loads(queued)["job_id"], job_id)
        self.assertIsInstance(json.loads(queued)["queued_at"], float)
        self.assertIn('gateway_http_requests_total{path="/generate",status="202"}', self.client.get("/metrics").text)

        redis_client.get.return_value = None
        redis_client.exists.return_value = 1
        self.assertEqual(self.client.get(f"/result/{job_id}").json(), {"status": "pending"})
        redis_client.get.return_value = json.dumps({"status": "done", "response": "world"})
        self.assertEqual(self.client.get(f"/result/{job_id}").json()["response"], "world")

    def test_rejects_empty_prompt_and_unknown_job(self):
        self.assertEqual(self.client.post("/generate", json={"prompt": ""}).status_code, 422)
        self.assertEqual(self.client.get("/result/not-a-job").status_code, 404)

    def test_gateway_metrics_bound_paths_and_statuses(self):
        self.client.post("/generate", json={"prompt": ""})
        self.client.get("/result/not-a-job")
        metrics = self.client.get("/metrics")
        self.assertEqual(metrics.status_code, 200)
        self.assertIn('gateway_http_requests_total{path="/generate",status="422"}', metrics.text)
        self.assertIn('gateway_http_requests_total{path="/result/{job_id}",status="404"}', metrics.text)
        self.assertNotIn('path="/metrics"', metrics.text)


class WorkerTests(unittest.TestCase):
    def test_worker_surfaces_failed_result_write_while_queue_is_empty(self):
        client = MagicMock()
        client.rpoplpush.return_value = None
        finished = threading.Event()
        payload = json.dumps({"job_id": "one", "prompt": "hello"})
        reads = iter([payload, None])

        def pop(*args, **kwargs):
            value = next(reads)
            if value is None:
                self.assertTrue(finished.wait(timeout=2))
            return value

        def failed_write(*args, **kwargs):
            finished.set()
            raise redis.RedisError("result write failed")

        client.brpoplpush.side_effect = pop
        with patch.object(worker, "process_job", side_effect=failed_write):
            with self.assertRaisesRegex(redis.RedisError, "result write failed"):
                worker.run(client)

    def test_two_requests_can_reach_vllm_together(self):
        client = MagicMock()
        client.rpoplpush.return_value = None
        client.brpoplpush.side_effect = [
            json.dumps({"job_id": "one", "prompt": "first"}),
            json.dumps({"job_id": "two", "prompt": "second"}),
            KeyboardInterrupt,
        ]
        rendezvous = threading.Barrier(2)

        def complete(job):
            rendezvous.wait(timeout=2)
            return {"choices": [{"text": job["prompt"]}]}

        with patch.object(worker, "infer", side_effect=complete) as infer:
            with self.assertRaises(KeyboardInterrupt):
                worker.run(client)
        self.assertEqual(infer.call_count, 2)
        self.assertEqual(client.pipeline.return_value.__enter__.return_value.setex.call_count, 2)

    @patch.object(worker, "infer", return_value={"choices": [{"text": "world"}]})
    def test_acks_completed_job(self, infer):
        client = MagicMock()
        payload = json.dumps({"job_id": "abc", "prompt": "hello"})
        client.rpoplpush.return_value = None
        client.brpoplpush.side_effect = [payload, KeyboardInterrupt]
        with self.assertRaises(KeyboardInterrupt):
            worker.run(client)
        pipeline = client.pipeline.return_value.__enter__.return_value
        self.assertEqual(json.loads(pipeline.setex.call_args.args[2])["response"], "world")
        pipeline.lrem.assert_called_once_with(worker.PROCESSING, 1, payload)

    @patch.object(worker, "infer", side_effect=ValueError("invalid completion"))
    def test_records_inference_failure(self, infer):
        client = MagicMock()
        client.rpoplpush.return_value = None
        client.brpoplpush.side_effect = [json.dumps({"job_id": "abc", "prompt": "hello"}), KeyboardInterrupt]
        with self.assertRaises(KeyboardInterrupt):
            worker.run(client)
        result = json.loads(client.pipeline.return_value.__enter__.return_value.setex.call_args.args[2])
        self.assertEqual(result, {"status": "error", "error": "invalid completion"})

    @patch.object(worker, "infer", return_value={"choices": [{"text": "world"}]})
    def test_observes_queue_to_result_time_after_ack(self, infer):
        client = MagicMock()
        payload = json.dumps({"job_id": "abc", "prompt": "hello", "queued_at": 100.0})
        with patch.object(worker.time, "time", return_value=108.0), patch.object(worker.JOB_DURATION, "labels") as duration:
            worker.process_job(client, payload)
        client.pipeline.return_value.__enter__.return_value.execute.assert_called_once()
        duration.assert_called_once_with("done")
        duration.return_value.observe.assert_called_once_with(8.0)

    @patch.object(worker, "infer", return_value={"choices": [{"text": "world"}]})
    def test_failed_redis_ack_does_not_count_completion(self, infer):
        client = MagicMock()
        client.pipeline.return_value.__enter__.return_value.execute.side_effect = redis.RedisError("write failed")
        payload = json.dumps({"job_id": "abc", "prompt": "hello", "queued_at": 100.0})
        with patch.object(worker.JOBS, "labels") as jobs, patch.object(worker.JOB_DURATION, "labels") as duration:
            with self.assertRaises(redis.RedisError):
                worker.process_job(client, payload)
        jobs.assert_not_called()
        duration.assert_not_called()

    def test_quarantines_malformed_job(self):
        client = MagicMock()
        client.rpoplpush.return_value = None
        client.brpoplpush.side_effect = ["not json", KeyboardInterrupt]
        with self.assertRaises(KeyboardInterrupt):
            worker.run(client)
        client.lpush.assert_called_once_with("inference-dead-letter", "not json")
        client.lrem.assert_called_once_with(worker.PROCESSING, 1, "not json")

    def test_requeues_inflight_job_after_restart(self):
        client = MagicMock()
        client.rpoplpush.side_effect = ["inflight", None]
        client.brpoplpush.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            worker.run(client)
        client.rpoplpush.assert_any_call(worker.PROCESSING, worker.QUEUE)


class DashboardTests(unittest.TestCase):
    def test_dashboard_has_seventeen_distinct_prometheus_panels(self):
        dashboard = json.loads((Path(__file__).resolve().parents[1] / "monitoring/dashboard.json").read_text())
        panels = dashboard["panels"]
        self.assertEqual(len(panels), 17)
        self.assertEqual(len({panel["id"] for panel in panels}), 17)
        self.assertTrue(all(panel["datasource"]["uid"] == "${DS_PROMETHEUS}" for panel in panels))
        expressions = [target["expr"] for panel in panels for target in panel["targets"]]
        self.assertTrue(any("gateway_http_requests_total" in expression for expression in expressions))
        self.assertTrue(any("inference_job_duration_seconds_bucket" in expression for expression in expressions))


if __name__ == "__main__":
    unittest.main()