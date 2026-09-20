"""The real client behind the http seam. No network: urlopen is monkeypatched.

Discord sits behind Cloudflare, which can reject urllib's default User-Agent
with a 403 (error code 1010). The client must send an identifying UA on every
request.
"""

import io
import json
import pathlib
import re

import kanban_task_threads.transport as transport_mod
from kanban_task_threads.transport import urllib_http


class FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def capture_request(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["request"] = request
        return FakeResponse(json.dumps({"id": "1"}).encode())

    monkeypatch.setattr(transport_mod.urllib.request, "urlopen", fake_urlopen)
    return seen


def test_sends_identifying_user_agent(monkeypatch):
    seen = capture_request(monkeypatch)
    urllib_http("GET", "https://discord.com/api/webhooks/1/tok", None)
    ua = seen["request"].get_header("User-agent", "")
    assert "kanban-task-threads" in ua
    assert "Python-urllib" not in ua
    manifest = (pathlib.Path(__file__).resolve().parents[1] / "plugin.yaml").read_text()
    version = re.search(r"^version: (\d+\.\d+\.\d+)$", manifest, re.MULTILINE).group(1)
    assert f"kanban-task-threads/{version}" in ua


def test_extra_headers_are_sent(monkeypatch):
    seen = capture_request(monkeypatch)
    urllib_http(
        "GET", "https://discord.com/api/v10/channels/7", None, headers={"Authorization": "Bot x"}
    )
    assert seen["request"].get_header("Authorization") == "Bot x"


def test_json_body_and_content_type(monkeypatch):
    seen = capture_request(monkeypatch)
    status, body = urllib_http("POST", "https://x.example/wh", {"a": 1})
    request = seen["request"]
    assert json.loads(request.data) == {"a": 1}
    assert request.get_header("Content-type") == "application/json"
    assert (status, body) == (200, {"id": "1"})
