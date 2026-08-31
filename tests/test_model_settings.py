import io
import json
import os
import threading
import urllib.error
import urllib.request

import pytest

from nagi import webapp
from nagi.agent_web import AgentWebService
from nagi.model_settings import (
    PRESETS,
    ManagedAnthropicClient,
    ManagedChatClient,
    ModelSettings,
    _NoRedirect,
    client_from_config,
    validate_base_url,
)

SECRET = "fixture-secret-only-not-real"


@pytest.fixture
def settings(tmp_path):
    return ModelSettings(
        tmp_path / "settings.json",
        lambda: {
            "provider": "deepseek",
            "model": "deepseek-v4-flash-vision-exp",
            "base_url": "https://api.deepseek.com/anthropic",
            "api_key": SECRET,
        },
    )


def provider(kind="kimi", **extra):
    preset = PRESETS[kind]
    return {
        "kind": kind,
        "name": preset["name"],
        "model": preset["models"][0],
        "base_url": preset["base_url"],
        "api_key": SECRET,
        **extra,
    }


def test_existing_environment_is_imported_without_copying_or_exposing_key(settings):
    data = settings.public()
    assert data["active_provider"] == "deepseek"
    assert data["providers"][0]["model"] == "deepseek-v4-flash-vision-exp"
    assert data["providers"][0]["api_key_configured"]
    assert SECRET not in json.dumps(data)
    assert "credential" not in data["providers"][0]
    assert not settings.path.exists()


def test_save_switch_reload_and_keep_existing_key(settings):
    data = settings.save(provider(), "initial", activate=True)
    identity = data["active_provider"]
    assert identity != "deepseek"
    assert SECRET not in json.dumps(data)
    assert settings.active_config()["model"] == "kimi-k2.6"
    if os.name == "nt":
        assert SECRET not in settings.path.read_text(encoding="utf-8")
        assert "dpapi:" in settings.path.read_text(encoding="utf-8")
    data = settings.save(
        {"id": identity, "name": "My Kimi", "api_key": ""}, data["revision"]
    )
    reloaded = ModelSettings(settings.path, settings.legacy_loader)
    assert reloaded.active_config()["api_key"] == SECRET
    assert reloaded.active_config()["name"] == "My Kimi"
    data = settings.activate("deepseek", "deepseek-v4-pro", data["revision"])
    assert data["active_provider"] == "deepseek"
    assert settings.active_config()["api_key"] == SECRET


def test_stale_revision_never_overwrites_file(settings):
    settings.save(provider(), "initial")
    before = settings.path.read_bytes()
    with pytest.raises(ValueError, match="其他页面"):
        settings.save(provider("glm"), "initial")
    assert settings.path.read_bytes() == before


def test_corrupt_settings_are_reported_without_overwriting(settings):
    settings.path.write_text('{"revision":"x","providers":[{}]}', encoding="utf-8")
    before = settings.path.read_bytes()
    with pytest.raises(ValueError, match="配置文件无效"):
        settings.public()
    assert settings.path.read_bytes() == before


def test_translation_cache_identity_separates_providers_and_endpoints():
    legacy = {
        "provider": "deepseek",
        "model": "same-id",
        "base_url": "https://api.deepseek.com/anthropic",
    }
    assert webapp._translation_model_id(legacy) == "same-id"
    other = {**legacy, "provider": "kimi", "base_url": "https://api.moonshot.cn/v1"}
    assert webapp._translation_model_id(other) != webapp._translation_model_id(legacy)
    assert webapp._translation_model_id(other) != webapp._translation_model_id(
        {**other, "base_url": "https://proxy.example/v1"}
    )


def test_cross_origin_edit_requires_new_key(settings):
    with pytest.raises(ValueError, match="重新输入密钥"):
        settings.save(
            {"id": "deepseek", "base_url": "https://other.example/v1"}, "initial"
        )
    assert not settings.path.exists()
    data = settings.save(
        {
            "id": "deepseek",
            "base_url": "https://other.example/v1",
            "api_key": "new-fixture-secret",
        },
        "initial",
    )
    assert data["providers"][0]["key_source"] == "local"
    assert settings.active_config()["api_key"] == "new-fixture-secret"


def test_delete_active_has_no_silent_fallback_and_clear_key_prevents_activation(
    settings,
):
    data = settings.save(provider(), "initial", activate=True)
    data = settings.remove(data["active_provider"], data["revision"])
    assert data["active_provider"] is None
    assert len(data["providers"]) == 1
    assert not settings.active_config()["api_key_configured"]
    data = settings.save({"id": "deepseek", "clear_key": True}, data["revision"])
    with pytest.raises(ValueError, match="密钥"):
        settings.activate("deepseek", "deepseek-v4-pro", data["revision"])


def test_test_config_does_not_save_or_activate(settings):
    result = settings.test_config(provider("glm"), "initial")
    assert result["api_key"] == SECRET
    assert result["provider"] == "glm"
    assert not settings.path.exists()
    assert settings.public()["active_provider"] == "deepseek"


@pytest.mark.parametrize(
    "base",
    [
        "http://remote.example/v1",
        "file:///tmp/key",
        "https://user:pass@example.com",
        "https://api.example.com/v1?key=secret",
        "https://api.example.com/v1/chat/completions",
        "https://api.example.com/#key",
        "https://api.example.com:999999",
        "https://api.example.com/\nfoo",
    ],
)
def test_unsafe_base_urls_are_rejected(base):
    with pytest.raises(ValueError):
        validate_base_url(base)


@pytest.mark.parametrize(
    "base",
    [
        "http://127.0.0.1:1234/v1",
        "http://[::1]:1234/v1",
        "https://api.example.com/custom/v1",
    ],
)
def test_supported_urls_preserve_paths(base):
    assert validate_base_url(base + "/") == base


@pytest.mark.parametrize("kind", list(PRESETS))
def test_provider_payloads_use_correct_endpoint_and_no_secret_in_payload(kind):
    config = provider(kind)
    client = client_from_config(config, translation=True)
    seen = []

    def opener(request, timeout):
        seen.append(request)
        return io.BytesIO(
            json.dumps(
                {"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]}
            ).encode()
        )

    client.request_opener = opener
    assert client.complete("Return JSON.", max_new_tokens=32) == "OK"
    request = seen[0]
    assert request.full_url == PRESETS[kind]["base_url"] + "/chat/completions"
    assert request.get_header("Authorization") == "Bearer " + SECRET
    body = json.loads(request.data)
    assert body["model"] == config["model"]
    assert SECRET not in request.data.decode()
    assert body["response_format"] == {"type": "json_object"}
    if kind in {"kimi", "glm", "deepseek"}:
        assert body["thinking"] == {"type": "disabled"}
    if kind == "kimi":
        assert "temperature" not in body
    if kind == "qwen":
        assert body["enable_thinking"] is False


def test_legacy_anthropic_agent_and_json_translation(settings):
    config = settings.active_config()
    assert isinstance(client_from_config(config), ManagedAnthropicClient)
    client = client_from_config(config, translation=True)
    assert isinstance(client, ManagedChatClient)
    assert client.base_url == "https://api.deepseek.com"
    assert client.response_format == {"type": "json_object"}


def test_custom_provider_disables_optional_provider_parameters():
    client = client_from_config(
        {
            "kind": "custom",
            "model": "custom-model",
            "base_url": "https://custom.example/v1",
            "api_key": SECRET,
            "json_output": False,
        },
        translation=True,
    )
    assert client.temperature is None
    assert client.thinking is None
    assert client.response_format is None
    assert not client.extra_body


def test_network_errors_and_model_text_redact_key():
    client = client_from_config(provider())

    def rejected(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 401, "Unauthorized", {}, io.BytesIO(SECRET.encode())
        )

    client.request_opener = rejected
    with pytest.raises(RuntimeError) as error:
        client.complete("OK", 16)
    assert SECRET not in str(error.value)
    assert "<redacted>" in str(error.value)
    client.request_opener = lambda *args, **kwargs: io.BytesIO(
        json.dumps({"choices": [{"message": {"content": SECRET}}]}).encode()
    )
    assert client.complete("OK", 16) == "<redacted>"


def test_redirects_never_forward_credentials():
    request = urllib.request.Request(
        "https://api.example.com/v1", headers={"Authorization": SECRET}
    )
    with pytest.raises(ValueError, match="重定向"):
        _NoRedirect().redirect_request(
            request, None, 307, "redirect", {}, "https://other.example/"
        )


def test_agent_captures_client_before_background_start(tmp_path, monkeypatch):
    first, second = object(), object()
    current = [first]
    monkeypatch.setattr("nagi.agent_web.AgentJob.start", lambda self: None)
    service = AgentWebService(tmp_path / "storage", tmp_path, lambda: current[0])
    job = service.start_job(
        workspace_path=str(tmp_path),
        session_id=None,
        message="hello",
        approval_policy="ask",
    )
    current[0] = second
    assert job.model_client is first
    assert "model_client" not in job.public_dict()


def test_translation_snapshot_stays_private(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp.threading.Thread, "start", lambda self: None)
    config = {"provider": "kimi", "model": "test", "api_key": SECRET}
    monkeypatch.setattr(webapp, "_configured_model", lambda: dict(config))
    monkeypatch.setattr(webapp, "JOBS", webapp.JobRegistry())
    job = webapp.start_translation_job(str(tmp_path))
    config["model"] = "different"
    assert job.model_config["model"] == "test"
    assert SECRET not in json.dumps(job.public_dict())
    assert SECRET not in repr(job)


def test_http_settings_end_to_end_without_real_provider(settings, monkeypatch):
    monkeypatch.setattr(webapp, "_model_settings", lambda: settings)
    server = webapp.QlieWebServer(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = f"http://127.0.0.1:{server.server_address[1]}"

    def call(path, body=None):
        request = urllib.request.Request(
            root + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request) as response:
            text = response.read().decode()
            assert SECRET not in text
            return json.loads(text)

    try:
        config = call("/api/config")
        data = call("/api/settings/models")
        with pytest.raises(urllib.error.HTTPError) as error:
            call(
                "/api/settings/models/save",
                {"provider": provider(), "expected_revision": data["revision"]},
            )
        assert error.value.code == 403
        token = {"csrf_token": config["csrf_token"]}
        data = call(
            "/api/settings/models/save",
            {
                **token,
                "provider": provider(),
                "expected_revision": data["revision"],
                "activate": True,
            },
        )
        assert call("/api/config")["model"] == "kimi-k2.6"
        before = settings.path.read_bytes()

        class FakeClient:
            def complete(self, *args, **kwargs):
                return "OK"

        monkeypatch.setattr(
            webapp, "client_from_config", lambda *args, **kwargs: FakeClient()
        )
        result = call(
            "/api/settings/models/test",
            {
                **token,
                "provider": provider("glm"),
                "expected_revision": data["revision"],
            },
        )
        assert result["ok"] is True
        assert settings.path.read_bytes() == before
        data = call(
            "/api/settings/models/remove",
            {
                **token,
                "id": data["active_provider"],
                "expected_revision": data["revision"],
            },
        )
        assert call("/api/config")["api_key_configured"] is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
