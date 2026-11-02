"""Explicit CC Switch relay authentication; never infer, refresh or store keys.

Only the controller reads the selected Claude provider. The isolated verifier
receives its allowlisted environment through stdin, never argv or a disk file.
"""
import hashlib
import json
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MAX_BYTES = 1_000_000
ENV_KEYS = frozenset(("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
                      "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                      "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                      "ANTHROPIC_SMALL_FAST_MODEL"))
CREDENTIAL_KEYS = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")


def _json_file(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError("expected a bounded, regular authentication source")
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError):
        raise ValueError("authentication source could not be read") from None


class RelayConfig:
    """Secrets have no repr and no public serialization path."""
    def __init__(self, env, *, provider_name="configured relay", provider_id="", source="cc-switch", model=None):
        if not isinstance(env, dict):
            raise ValueError("relay environment must be an object")
        self.env = {key: value for key, value in env.items() if key in ENV_KEYS}
        if not all(isinstance(value, str) and len(value) <= 16384 for value in self.env.values()):
            raise ValueError("invalid relay environment values")
        if any(any(ord(char) < 32 or ord(char) == 127 for char in value) for value in self.env.values()):
            raise ValueError("control characters in relay environment")
        self.base_url = self.env.get("ANTHROPIC_BASE_URL", "").rstrip("/")
        try:
            parts = urllib.parse.urlsplit(self.base_url)
            port = parts.port if parts.port is not None else (443 if parts.scheme == "https" else 80)
        except ValueError:
            raise ValueError("invalid relay URL") from None
        if (parts.scheme not in ("https", "http") or not parts.hostname or "@" in parts.netloc
                or parts.query or parts.fragment or " " in self.base_url
                or not 1 <= port <= 65535
                or (parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1", "::1"))):
            raise ValueError("relay requires HTTPS or loopback HTTP, without URL credentials or query")
        credentials = [key for key in CREDENTIAL_KEYS if self.env.get(key)]
        if len(credentials) != 1:
            raise ValueError("select exactly one relay credential type")
        self.credential_key = credentials[0]
        self.port = port
        self.env["ANTHROPIC_BASE_URL"] = self.base_url
        self.provider_name = provider_name
        self.provider_id = provider_id
        self.source = source
        self.model = model

    def __repr__(self):
        return "RelayConfig(credentials=redacted)"

    def redact(self, text):
        for key in CREDENTIAL_KEYS:
            value = self.env.get(key)
            if value:
                text = text.replace(value, "[REDACTED_CREDENTIAL]")
        return text

    def public(self):
        public_env = {key: value for key, value in self.env.items() if key not in CREDENTIAL_KEYS}
        digest = hashlib.sha256(json.dumps(public_env, sort_keys=True).encode("utf-8")).hexdigest()
        return {"source": self.source, "provider_name": self.redact(str(self.provider_name))[:100],
                "provider_identity_sha256": hashlib.sha256(str(self.provider_id).encode("utf-8")).hexdigest(),
                "configuration_sha256": digest, "credential_kind": self.credential_key,
                "relay_port": self.port, "credentials_persisted": False}


def load_deepseek_project(env_file):
    """Read only literal DeepSeek fields; never source or execute another project's .env."""
    path = Path(env_file).expanduser()
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError("invalid DeepSeek project environment source")
    allowed = {"MODEL_API_KEY", "DEEPSEEK_API_KEY", "MODEL_BASE_URL", "MODEL_NAME"}
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*", line)
        if not match or match[1] not in allowed:
            continue
        key, value = match[1], match[2]
        if key in values:
            raise ValueError("duplicate DeepSeek configuration field")
        if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    base = values.get("MODEL_BASE_URL", "https://api.deepseek.com").rstrip("/")
    if base not in ("https://api.deepseek.com", "https://api.deepseek.com/v1"):
        raise ValueError("selected project does not use the official DeepSeek API")
    model = values.get("MODEL_NAME", "deepseek-flash")
    if model != "deepseek-flash":
        raise ValueError("only the explicitly selected DeepSeek Flash fallback is supported")
    keys = [values[k] for k in ("MODEL_API_KEY", "DEEPSEEK_API_KEY") if values.get(k)]
    if len(set(keys)) != 1:
        raise ValueError("missing or ambiguous DeepSeek credential")
    env = {"ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic", "ANTHROPIC_AUTH_TOKEN": keys[0],
           "ANTHROPIC_MODEL": model, "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
           "ANTHROPIC_DEFAULT_HAIKU_MODEL": model, "ANTHROPIC_DEFAULT_OPUS_MODEL": model}
    return RelayConfig(env, provider_name="DeepSeek official", provider_id=str(path.resolve()),
                       source="deepseek_project", model=model)


def load_cc_switch(directory=None):
    """Read only the selected Claude row; do not consult other applications."""
    directory = Path(directory or Path.home() / ".cc-switch").resolve()
    selection = _json_file(directory / "settings.json")
    selected = selection.get("currentProviderClaude") if isinstance(selection, dict) else None
    if not isinstance(selected, str) or not selected:
        raise ValueError("CC Switch has no selected Claude provider")
    database = directory / "cc-switch.db"
    if database.is_symlink() or not database.is_file():
        raise ValueError("CC Switch database is not a regular file")
    try:
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=3) as db:
            rows = db.execute("SELECT id,name,settings_config,is_current FROM providers WHERE app_type='claude' AND (id=? OR is_current=1)",
                              (selected,)).fetchall()
    except sqlite3.Error:
        raise ValueError("CC Switch provider could not be read") from None
    if len(rows) != 1 or rows[0][0] != selected or not rows[0][3]:
        raise ValueError("CC Switch selected provider and database current provider disagree")
    try:
        if len(rows[0][2]) > MAX_BYTES:
            raise ValueError
        config = json.loads(rows[0][2])
        env = config.get("env") if isinstance(config, dict) else None
    except (TypeError, ValueError):
        raise ValueError("invalid selected provider configuration") from None
    return RelayConfig(env, provider_name=rows[0][1], provider_id=rows[0][0])


def install_adapter(visible):
    """Only code is installed in the attempt; no credential files or security shim."""
    helper = Path(visible) / "shim/live_auth.py"
    helper.write_bytes(Path(__file__).read_bytes())
    return hashlib.sha256(helper.read_bytes()).hexdigest()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _catalog(config, version, *, authorized):
    headers = {"Accept": "application/json", "anthropic-version": "2023-06-01",
               "User-Agent": f"claude-cli/{version} (external, cli)"}
    if authorized:
        if config.credential_key == "ANTHROPIC_AUTH_TOKEN":
            headers["Authorization"] = "Bearer " + config.env[config.credential_key]
        else:
            headers["x-api-key"] = config.env[config.credential_key]
    # SDK-compatible base-path append; never contact an unrelated origin.
    request = urllib.request.Request(config.base_url + "/v1/models", headers=headers, method="GET")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        fetch = opener.open
        with fetch(request, timeout=10) as response:
            raw = response.read(MAX_BYTES + 1)
            status = response.status
        if len(raw) > MAX_BYTES:
            return {"http_status": status, "valid_catalog": False, "reason": "response_too_large"}
        data = json.loads(raw)
        models = data.get("data") if isinstance(data, dict) else None
        if not isinstance(models, list) or not all(isinstance(row, dict) and isinstance(row.get("id"), str) for row in models):
            return {"http_status": status, "valid_catalog": False, "reason": "invalid_catalog"}
        sonnet = [row["id"] for row in models if "sonnet" in row["id"].lower()
                  and re.fullmatch(r"[A-Za-z0-9_./:-]{1,200}", row["id"])]
        return {"http_status": status, "valid_catalog": status == 200, "model_count": len(models),
                "sonnet_model_ids": [config.redact(value) for value in sorted(set(sonnet))[:30]]}
    except urllib.error.HTTPError as exc:
        # Never print the response body: gateways may echo credentials in errors.
        return {"http_status": exc.code, "valid_catalog": False}
    except (OSError, ValueError, urllib.error.URLError):
        return {"http_status": None, "valid_catalog": False, "reason": "transport_or_decode_failed"}


def verify_relay(config, cli_version):
    match = re.search(r"\b\d+\.\d+\.\d+\b", cli_version)
    if not match:
        raise ValueError("CLI version required for relay transport")
    negative = _catalog(config, match[0], authorized=False)
    positive = _catalog(config, match[0], authorized=True)
    proved = negative["http_status"] in (401, 403) and positive["valid_catalog"]
    return {"kind": "relay_catalog_auth_check", "model_requests": 0,
            "authenticated": proved, "negative_control": negative, "authorized": positive,
            "credential_kind": config.credential_key, "credentials_persisted": False,
            "inference_verified": False}


def main():
    if sys.argv[1:] != ["--verify-relay"]:
        print("Use --verify-relay; provider environment is read from stdin", file=sys.stderr)
        return 2
    try:
        raw = sys.stdin.buffer.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError
        payload = json.loads(raw)
        config = RelayConfig(payload.get("env"))
        report = verify_relay(config, payload.get("cli_version", ""))
    except (OSError, ValueError, TypeError, AttributeError):
        report = {"authenticated": False, "model_requests": 0, "reason": "invalid_relay_probe_input"}
    print(json.dumps(report, ensure_ascii=True))
    return 0 if report["authenticated"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
