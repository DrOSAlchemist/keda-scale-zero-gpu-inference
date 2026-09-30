import json
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

import gateway
import worker


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

        redis_client.get.return_value = None
        redis_client.exists.return_value = 1
        self.assertEqual(self.client.get(f"/result/{job_id}").json(), {"status": "pending"})
        redis_client.get.return_value = json.dumps({"status": "done", "response": "world"})
        self.assertEqual(self.client.get(f"/result/{job_id}").json()["response"], "world")

    def test_rejects_empty_prompt_and_unknown_job(self):
        self.assertEqual(self.client.post("/generate", json={"prompt": ""}).status_code, 422)
        self.assertEqual(self.client.get("/result/not-a-job").status_code, 404)


class WorkerTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()