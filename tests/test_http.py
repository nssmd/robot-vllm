from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time

import pytest

from robot_vllm.policies import AlternatingPolicy, ChatPolicy, VLAPolicy
from robot_vllm.protocol import ACTION_SPEC, Observation, validate_proposal
from robot_vllm.runtime import RunStore, Runtime
from test_runtime import FixtureEnvironment, manifest


@pytest.fixture
def server():
    records = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            records.append((self.path, body))
            if self.path == "/chat":
                result = {"choices": [{"message": {"content": json.dumps({
                    "kind": "skill", "name": "gripper", "arguments": {"closed": True, "steps": 1}})}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}
            elif self.path == "/vla":
                result = {"schema": "robot_vllm.v1", "episode_id": body["observation"]["episode_id"],
                          "observation_version": body["observation"]["observation_version"],
                          "action_spec": ACTION_SPEC, "actions": [[0, 0, 0.01, 0, 0, 0, -1]]}
            elif self.path == "/bad-chat":
                result = {"choices": [{"message": {"content": "not JSON"}}]}
            else:
                self.send_error(503)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
        def log_message(self, *args):
            pass
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(httpd.server_port), records
    finally:
        httpd.shutdown()
        thread.join()
        httpd.server_close()


def test_model_adapters_share_episode_and_feedback(server, tmp_path):
    url, records = server
    policy = AlternatingPolicy(ChatPolicy(url + "/chat", "fixture-chat"),
                               VLAPolicy(url + "/vla", "fixture-vla"))
    env = FixtureEnvironment()
    result = Runtime(RunStore(tmp_path), max_steps=4).run(lambda _: env, policy, manifest())
    assert [r[0] for r in records] == ["/chat", "/vla", "/chat", "/vla"]
    assert records[1][1]["feedback"]["status"] == "completed"
    assert records[1][1]["observation"]["observation_version"] == 1
    assert result["status"] == "failure" and len(env.actions) == 4
    metrics = result["metrics"]
    assert metrics["vlm_calls"] == 2 and metrics["model_calls"] == 4
    assert metrics["total_tokens"] == 240 and metrics["unmetered_call_count"] == 2
    assert metrics["token_totals_are_partial"] is True
    wire = json.dumps(records)
    for forbidden in ("simulator_verdict", "init_states", '"seed"', '"reward"'):
        assert forbidden not in wire


def test_chat_images_are_encoded_not_local_paths(server, tmp_path):
    from PIL import Image
    url, records = server
    frame = tmp_path / "frame.png"
    Image.new("RGB", (2, 2)).save(frame)
    obs = Observation("test-episode", 0, time.monotonic(), "move", {"agentview": str(frame)}, {})
    decision = ChatPolicy(url + "/chat", "fixture-chat").decide(obs, None)
    assert validate_proposal(decision.proposal, obs, set(), 8)
    request = json.dumps(records[0][1])
    assert "data:image/png;base64," in request and str(frame) not in request


def test_malformed_chat_output_is_rejected_without_motion(server, tmp_path):
    url, _ = server
    env = FixtureEnvironment()
    result = Runtime(RunStore(tmp_path)).run(lambda _: env,
                 ChatPolicy(url + "/bad-chat", "fixture"), manifest())
    assert env.actions == []
    assert result["status"] == "failure" and result["metrics"]["rejected_proposals"] == 3


def test_http_provider_error_excluded(server, tmp_path):
    url, _ = server
    env = FixtureEnvironment()
    result = Runtime(RunStore(tmp_path)).run(lambda _: env,
                 ChatPolicy(url + "/unavailable", "fixture"), manifest())
    assert result["status"] == "infra" and result["task_denominator"] == 0
    assert result["metrics"]["model_calls"] == 1
    assert result["metrics"]["vlm_calls"] == 1
    assert result["metrics"]["unmetered_call_count"] == 1
