"""Relay selection, secret handling and authenticated read-only controls."""
import io
import json
import sqlite3
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

from autoharness.lib import live_auth as auth
from autoharness.lib import live_pilot as pilot

TOKEN = "private-test-credential"


def provider(**changes):
    return auth.RelayConfig({"ANTHROPIC_BASE_URL": "https://relay.example/prefix",
                             "ANTHROPIC_AUTH_TOKEN": TOKEN, **changes})


def source(tmp_path, *, settings_id="selected", database_id="selected", current=1, env=None):
    (tmp_path / "settings.json").write_text(json.dumps({"currentProviderClaude": settings_id}), encoding="utf-8")
    with sqlite3.connect(tmp_path / "cc-switch.db") as db:
        db.execute("CREATE TABLE providers(id TEXT,app_type TEXT,name TEXT,settings_config TEXT,is_current INTEGER)")
        db.execute("INSERT INTO providers VALUES(?,?,?,?,?)", (database_id, "claude", "relay", json.dumps({"env": env or provider().env}), current))
        # Selection must never accidentally use another app's current credentials.
        db.execute("INSERT INTO providers VALUES(?,?,?,?,?)", ("other-app", "codex", "other", '{"env":{"ANTHROPIC_AUTH_TOKEN":"other-private"}}', 1))
    return tmp_path


def test_reads_only_selected_claude_provider_and_keeps_database_unchanged(tmp_path):
    directory = source(tmp_path)
    before = (directory / "cc-switch.db").read_bytes()
    configured = auth.load_cc_switch(directory)
    assert configured.env["ANTHROPIC_AUTH_TOKEN"] == TOKEN
    assert (directory / "cc-switch.db").read_bytes() == before
    assert "other-private" not in repr(configured)


@pytest.mark.parametrize("args", [{"settings_id": "missing"}, {"database_id": "different"}, {"current": 0}])
def test_ambiguous_or_missing_selection_fails_closed(tmp_path, args):
    with pytest.raises(ValueError):
        auth.load_cc_switch(source(tmp_path, **args))


def test_does_not_create_a_missing_database(tmp_path):
    with pytest.raises(ValueError):
        auth.load_cc_switch(tmp_path)
    assert not (tmp_path / "cc-switch.db").exists()


def test_does_not_follow_linked_database(tmp_path):
    directory = tmp_path / "configuration"
    directory.mkdir()
    source(directory)
    db = directory / "cc-switch.db"
    original = tmp_path / "elsewhere.db"
    db.rename(original)
    try:
        db.symlink_to(original)
    except OSError:
        pytest.skip("file symlink privilege unavailable")
    with pytest.raises(ValueError):
        auth.load_cc_switch(directory)


def test_public_metadata_repr_and_fingerprint_exclude_credentials():
    configured = provider()
    rotated = provider(ANTHROPIC_AUTH_TOKEN="rotated-test-credential")
    assert configured.public() == rotated.public()
    assert TOKEN not in repr(configured) + json.dumps(configured.public())
    assert TOKEN not in configured.redact("gateway echoed " + TOKEN)


def test_configuration_fingerprint_detects_endpoint_and_model_changes():
    original = provider().public()["configuration_sha256"]
    assert provider(ANTHROPIC_BASE_URL="https://other.example").public()["configuration_sha256"] != original
    assert provider(ANTHROPIC_DEFAULT_SONNET_MODEL="claude-sonnet-fixed").public()["configuration_sha256"] != original


@pytest.mark.parametrize("url", ["http://public.example", "https://user:password@relay.example", "https://relay.example/?key=secret",
                                  "https://relay.example/#secret", "file:///secret", "https://relay.example:invalid", "https://relay.example/\nsecret",
                                  "https://@relay.example", "https://relay.example:0"])
def test_unsafe_urls_are_rejected_without_echoing_the_url(url):
    with pytest.raises(ValueError) as error:
        provider(ANTHROPIC_BASE_URL=url)
    assert url not in str(error.value)


def test_loopback_proxy_and_base_path_are_preserved():
    configured = provider(ANTHROPIC_BASE_URL="http://127.0.0.1:15721/proxy/")
    assert configured.port == 15721 and configured.base_url.endswith("/proxy")


@pytest.mark.parametrize("env", [{"ANTHROPIC_AUTH_TOKEN": ""}, {"ANTHROPIC_API_KEY": "second-secret"},
                                   {"ANTHROPIC_AUTH_TOKEN": "key\r\nHeader: injected"}])
def test_credential_ambiguity_or_header_injection_is_rejected(env):
    with pytest.raises(ValueError):
        provider(**env)


def test_relay_environment_is_explicit_and_does_not_import_personal_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "wrong-inherited-secret")
    configured = provider(apiKeyHelper="run-personal-command", PYTHONPATH="/personal/hooks", hooks={"bad": "command"})
    env = pilot.environment(tmp_path, configured)
    assert env["ANTHROPIC_AUTH_TOKEN"] == TOKEN
    assert env["ANTHROPIC_BASE_URL"] == "https://relay.example/prefix"
    assert not {"ANTHROPIC_API_KEY", "apiKeyHelper", "PYTHONPATH", "hooks", "CLAUDE_CODE_OAUTH_TOKEN"} & env.keys()
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "config")


class Response:
    status = 200

    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size):
        return self.body[:size]


def gateway(monkeypatch, *, anonymous_status=401, authorized_status=200, body=None):
    calls, handlers = [], []
    body = body if body is not None else json.dumps({"data": [{"id": "claude-sonnet-test"}]}).encode()

    def fetch(request, timeout):
        calls.append(request)
        authorized = request.get_header("Authorization") is not None or request.get_header("X-api-key") is not None
        status = authorized_status if authorized else anonymous_status
        if status != 200:
            raise urllib.error.HTTPError(request.full_url, status, "gateway error", {}, io.BytesIO(TOKEN.encode()))
        return Response(body)

    def opener(*args):
        handlers.extend(args)
        return SimpleNamespace(open=fetch)

    monkeypatch.setattr(auth.urllib.request, "build_opener", opener)
    return calls, handlers


def test_authentication_needs_both_negative_and_positive_control(monkeypatch):
    calls, handlers = gateway(monkeypatch)
    report = auth.verify_relay(provider(), "2.1.312 (Claude Code)")
    assert report["authenticated"] and report["model_requests"] == 0 and not report["inference_verified"]
    assert len(calls) == 2 and all(r.method == "GET" and r.full_url == "https://relay.example/prefix/v1/models" for r in calls)
    assert calls[0].get_header("Authorization") is None
    assert calls[1].get_header("Authorization") == "Bearer " + TOKEN
    assert calls[1].get_header("User-agent") == "claude-cli/2.1.312 (external, cli)"
    assert any(isinstance(h, auth._NoRedirect) for h in handlers)
    assert any(isinstance(h, auth.urllib.request.ProxyHandler) and h.proxies == {} for h in handlers)
    assert TOKEN not in json.dumps(report)


def test_public_model_catalog_cannot_prove_authentication(monkeypatch):
    gateway(monkeypatch, anonymous_status=200)
    assert not auth.verify_relay(provider(), "2.1.312")["authenticated"]


@pytest.mark.parametrize("status", [401, 403, 429, 500, 302])
def test_errors_and_redirects_never_count_as_success_or_echo_gateway_body(monkeypatch, status):
    gateway(monkeypatch, authorized_status=status)
    report = auth.verify_relay(provider(), "2.1.312")
    assert not report["authenticated"] and TOKEN not in json.dumps(report)


@pytest.mark.parametrize("body", [b"not-json", b"{}", b'{"data":[{}]}', b'x' * (auth.MAX_BYTES + 1)])
def test_malformed_and_oversized_catalogs_fail_closed(monkeypatch, body):
    gateway(monkeypatch, body=body)
    assert not auth.verify_relay(provider(), "2.1.312")["authenticated"]


def test_api_key_uses_its_own_header_without_bearer_auth(monkeypatch):
    calls, _ = gateway(monkeypatch)
    configured = provider(ANTHROPIC_AUTH_TOKEN="", ANTHROPIC_API_KEY=TOKEN)
    assert auth.verify_relay(configured, "2.1.312")["authenticated"]
    assert calls[1].get_header("X-api-key") == TOKEN and calls[1].get_header("Authorization") is None


def test_installing_verifier_copies_only_code(tmp_path):
    (tmp_path / "shim").mkdir()
    auth.install_adapter(tmp_path)
    files = list((tmp_path / "shim").iterdir())
    assert [p.name for p in files] == ["live_auth.py"]
    assert files[0].read_bytes() == Path(auth.__file__).read_bytes()


def test_invalid_stdin_never_echoes_secret_input(monkeypatch, capsys):
    monkeypatch.setattr(auth.sys, "argv", ["helper", "--verify-relay"])
    monkeypatch.setattr(auth.sys, "stdin", SimpleNamespace(buffer=io.BytesIO((TOKEN + "{bad-json").encode())))
    assert auth.main() == 1
    report = capsys.readouterr().out
    assert TOKEN not in report and json.loads(report)["model_requests"] == 0


def test_host_check_failure_is_visible_in_command_exit_status(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pilot.sys, "argv", ["pilot", "check", "--output", str(tmp_path)])
    monkeypatch.setattr(pilot, "check_host", lambda *a, **k: {"passed": False})
    assert pilot.main() == 1
    assert json.loads(capsys.readouterr().out) == {"passed": False}


def test_deepseek_project_loader_reads_only_literal_selected_fields(tmp_path):
    source_file = tmp_path / ".env"
    source_file.write_text('MODEL_NAME=deepseek-flash\nMODEL_BASE_URL=https://api.deepseek.com\nMODEL_API_KEY=private-test-key\nDATABASE_PASSWORD=unrelated-private\nDANGEROUS=$(touch /tmp/do-not-execute)\n', encoding="utf-8")
    configured = auth.load_deepseek_project(source_file)
    assert configured.env["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"
    assert configured.model == "deepseek-flash" and configured.source == "deepseek_project"
    assert "unrelated-private" not in json.dumps(configured.env)
    assert "private-test-key" not in json.dumps(configured.public())


def test_deepseek_loader_refuses_sending_project_key_to_another_origin(tmp_path):
    source_file = tmp_path / ".env"
    source_file.write_text('MODEL_BASE_URL=https://another.example\nMODEL_API_KEY=private-key\n', encoding="utf-8")
    with pytest.raises(ValueError, match="official DeepSeek"):
        auth.load_deepseek_project(source_file)
