from io import BytesIO
from PIL import Image
from novelai_image.client import NovelAIClient, build_payload, validate_image
from novelai_image.exceptions import NovelAIError
from novelai_image.parser import parse_args
import asyncio
import base64
import json
import sys
from types import SimpleNamespace

import httpx
import pytest

@pytest.fixture
def encoded():
    output = BytesIO()
    Image.new("RGB", (8, 8), "blue").save(output, format="PNG")
    return base64.b64encode(output.getvalue()).decode()

@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv("NOVELAI_API_TOKEN", "MOCK_ONLY_NOT_A_REAL_CREDENTIAL")

def execute(handler):
    req = build_payload(parse_args("castle --seed 123"), 5, "k_euler_ancestral")
    return asyncio.run(NovelAIClient(transport=httpx.MockTransport(handler)).generate(req))

def test_success(encoded, token):
    def handler(request):
        payload = json.loads(request.content)
        assert request.headers["accept"] == "application/json"
        assert payload["parameters"]["v4_prompt"]["caption"]["char_captions"] == []
        return httpx.Response(201, json={"images": [{"image": encoded, "index": 0, "seed": 123}]})
    assert execute(handler)[0]["seed"] == 123

@pytest.mark.parametrize("status,text", [(400,"参数"),(401,"令牌"),(402,"Anlas"),(403,"权限"),(422,"参数"),(429,"频率"),(500,"报告编号"),(503,"服务异常")])
def test_status(status, text, token, caplog):
    with pytest.raises(NovelAIError, match=text) as error:
        execute(lambda request: httpx.Response(status, headers={"x-correlation-id": "Ab12Cd"}, json={"message": "MOCK_ONLY_NOT_A_REAL_CREDENTIAL"}))
    assert "MOCK_ONLY_NOT_A_REAL_CREDENTIAL" not in str(error.value) + caplog.text

@pytest.mark.parametrize("exception,text", [(httpx.ReadTimeout,"超时"),(httpx.ConnectError,"网络")])
def test_network(exception, text, token):
    def handler(request):
        raise exception("MOCK_ONLY_NOT_A_REAL_CREDENTIAL", request=request)
    with pytest.raises(NovelAIError, match=text):
        execute(handler)

@pytest.mark.parametrize("body", [{}, {"images": []}, {"images": [{"image": "%%", "seed": 1}]}, {"images": [{"image": "", "seed": 1}]}, {"images": [{"image": "aa", "seed": "bad"}]}])
def test_bad_response(body, token):
    with pytest.raises(NovelAIError):
        execute(lambda request: httpx.Response(201, json=body))

def test_invalid_json(token):
    with pytest.raises(NovelAIError, match="JSON"):
        execute(lambda request: httpx.Response(201, content=b"invalid"))

def test_missing_token(monkeypatch):
    monkeypatch.delenv("NOVELAI_API_TOKEN", raising=False)
    monkeypatch.setattr("novelai_image.client.resolve_api_token", lambda: "")
    with pytest.raises(NovelAIError, match="未配置"):
        execute(lambda request: pytest.fail("must not request"))

def test_resolve_api_token_reads_current_user_registry(monkeypatch):
    monkeypatch.delenv("NOVELAI_API_TOKEN", raising=False)

    class RegistryKey:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    registry = SimpleNamespace(
        HKEY_CURRENT_USER=object(),
        KEY_READ=1,
        OpenKey=lambda *args: RegistryKey(),
        QueryValueEx=lambda key, name: ("  registry-test-token  ", 1),
    )
    monkeypatch.setitem(sys.modules, "winreg", registry)

    from novelai_image.client import resolve_api_token

    assert resolve_api_token() == "registry-test-token"

def test_resolve_api_token_prefers_process_environment(monkeypatch):
    monkeypatch.setenv("NOVELAI_API_TOKEN", "  process-test-token  ")
    registry = SimpleNamespace(
        HKEY_CURRENT_USER=object(),
        KEY_READ=1,
        OpenKey=lambda *args: pytest.fail("registry should not be read when process token exists"),
    )
    monkeypatch.setitem(sys.modules, "winreg", registry)

    from novelai_image.client import resolve_api_token

    assert resolve_api_token() == "process-test-token"

def test_image_validation(encoded):
    assert validate_image("data:image/png;base64," + encoded) == encoded
    for value in ("%%%", base64.b64encode(b"not an image").decode(), base64.b64encode(b"\x89PNG\r\n\x1a\nbroken").decode()):
        with pytest.raises(NovelAIError):
            validate_image(value)
    with pytest.raises(NovelAIError, match="大小"):
        validate_image(encoded, max_bytes=1)

@pytest.mark.parametrize("url", ["http://image.novelai.net", "https://127.0.0.1", "https://localhost", "https://evil.test", "https://image.novelai.net@localhost"])
def test_endpoint(url):
    with pytest.raises(NovelAIError):
        NovelAIClient(url)
