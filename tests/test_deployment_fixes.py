"""Deployment-fix regressions: service permissions, verify scripts, TLS,
initial personalization — plus a functional permission-model test that runs
in the sandbox without extra users.

Sandbox vs. Portable: everything here is deterministic off-line checking.
The service-account smoke test (runuser startup + config write as the
'aurora' user) physically requires root + the target machine; it lives in
deploy/verify-permissions.sh and is exercised there, never claimed here.
"""

import ast
import getpass
import os
import re
import shutil
import stat
import subprocess
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"

UNIT = (DEPLOY / "aurora-companion.service").read_text()
INSTALL = (DEPLOY / "install.sh").read_text()
VERIFY_VOICE = (DEPLOY / "verify-voice.sh").read_text()
VERIFY_PERMS = (DEPLOY / "verify-permissions.sh").read_text()
MAKE_CERT = (DEPLOY / "make-cert.sh").read_text()
PORTABLE_CFG = (DEPLOY / "config.portable.yaml").read_text()


def _bash_n(script: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)


# ---------------------------------------------------------------- shell syntax
@pytest.mark.parametrize("name", ["install.sh", "verify-voice.sh", "verify-permissions.sh",
                                  "make-cert.sh", "backup.sh"])
def test_deploy_scripts_have_valid_bash_syntax(name):
    result = _bash_n(DEPLOY / name)
    assert result.returncode == 0, result.stderr


def test_deploy_scripts_are_executable():
    """The zip preserves source file modes, so the exec bit must be set here."""
    for name in ("install.sh", "verify-voice.sh", "verify-permissions.sh",
                 "make-cert.sh", "backup.sh"):
        p = DEPLOY / name
        assert os.access(p, os.X_OK), f"{name} is not executable"


# ---------------------------------------------------------------- systemd unit
def test_unit_runs_as_non_root_service_account():
    assert re.search(r"^User=aurora$", UNIT, re.M)
    assert re.search(r"^Group=aurora$", UNIT, re.M)
    assert "User=root" not in UNIT


def test_unit_hardening_retained():
    assert "NoNewPrivileges=true" in UNIT
    assert "ProtectSystem=strict" in UNIT
    assert "PrivateTmp=true" in UNIT
    assert "Restart=on-failure" in UNIT
    assert "RestartSec=10s" in UNIT


def test_unit_readwrite_paths_separate_app_from_state():
    """App tree must be read-only; only data + config are writable."""
    m = re.search(r"^ReadWritePaths=(.+)$", UNIT, re.M)
    assert m, "ReadWritePaths missing"
    paths = m.group(1).split()
    assert "/var/lib/aurora" in paths and "/etc/aurora" in paths
    assert not any(p.startswith("/opt/") for p in paths), \
        "the application tree must not be writable by the service"


def test_unit_reads_config_via_env_file_and_skips_bytecode():
    assert "EnvironmentFile=-/etc/aurora/aurora.env" in UNIT
    assert "Environment=PYTHONDONTWRITEBYTECODE=1" in UNIT
    assert "config.yaml" in UNIT


# ---------------------------------------------------------------- install.sh
def test_installer_ownership_gives_service_account_the_config_dir():
    """The fix: /etc/aurora is chowned to the service account (read + write)
    and stays invisible to other users — never world-writable, never root-run."""
    assert re.search(r'chown -R "\$AURORA_USER":"\$AURORA_USER" "\$AURORA_ETC"', INSTALL)
    assert re.search(r'chmod 750 "\$AURORA_HOME" "\$AURORA_DATA" "\$AURORA_ETC"', INSTALL)
    assert re.search(r'chmod 600 "\$AURORA_ETC/config.yaml"', INSTALL)
    assert re.search(r'chown root:"\$AURORA_USER" "\$AURORA_ETC/aurora\.env"', INSTALL)
    assert re.search(r'chmod 0640 "\$AURORA_ETC/aurora\.env"', INSTALL)


def test_installer_installs_env_file_before_applying_permissions():
    """aurora.env must exist BEFORE the permission block (so chown/chmod
    actually apply on a clean install) and BEFORE the pre-start verification
    (so the readability check runs). Reinstall keeps an existing file."""
    cp_at = INSTALL.find('cp "$SRC/deploy/aurora.env.example" "$AURORA_ETC/aurora.env"')
    perm_at = INSTALL.find('chown -R "$AURORA_USER":"$AURORA_USER" "$AURORA_ETC"')
    verify_at = INSTALL.find('check "aurora.env readable"')
    assert cp_at != -1, "env file not installed"
    assert 0 < cp_at < perm_at < verify_at, \
        "env copy must precede the permission block and the verification"
    assert '[[ ! -f "$AURORA_ETC/aurora.env" ]]' in INSTALL  # reinstall preserves it
    # the redundant copy in the service section is gone
    assert INSTALL.count("aurora.env.example") == 1


def test_installer_never_relaxes_permissions():
    for pattern in ("chmod 777", "chmod 666", "chmod -R a+rw", "chmod o+", "a+w"):
        assert pattern not in INSTALL, f"installer must never use {pattern}"


def test_installer_verifies_paths_as_service_account_before_start():
    assert 'runuser -u "$AURORA_USER"' in INSTALL
    assert 'check "config.yaml readable (service account)"' in INSTALL
    assert 'check "config.yaml writable (preference persistence)"' in INSTALL
    assert 'check "data dir writable' in INSTALL
    assert "-perm /o+rwx" in INSTALL  # world-accessible entry check
    assert "refusing to start the service" in INSTALL  # abort on failure


def test_installer_smoke_tests_startup_and_persistence_under_service_account():
    assert "configuration read + persistence works as" in INSTALL
    assert "run.py" in INSTALL and "--check" in INSTALL
    assert "PYTHONPATH=\"$AURORA_HOME\"" in INSTALL


def test_installer_tls_flag_wiring():
    assert "--tls" in INSTALL and "make-cert.sh" in INSTALL
    assert "# tls_cert: " in PORTABLE_CFG and "# tls_key: " in PORTABLE_CFG


# ---------------------------------------------------------------- verify-voice.sh
def test_voice_verify_uses_auth_array_not_word_splitting():
    assert "AUTH_ARGS=()" in VERIFY_VOICE
    assert 'AUTH_ARGS=(-H "Authorization: Bearer $AURORA_TOKEN")' in VERIFY_VOICE
    assert '"${AUTH_ARGS[@]}"' in VERIFY_VOICE
    assert "$(auth)" not in VERIFY_VOICE and "auth()" not in VERIFY_VOICE


def test_voice_verify_never_prints_the_token():
    for line in VERIFY_VOICE.splitlines():
        if "AURORA_TOKEN" in line and not line.lstrip().startswith("#"):
            assert (re.match(r'\s*AURORA_TOKEN="\$\{AURORA_TOKEN:-\}"', line)
                    or '[[ -n "$AURORA_TOKEN" ]]' in line
                    or 'AUTH_ARGS=(-H "Authorization: Bearer $AURORA_TOKEN")' in line
                    or "AURORA_TOKEN=... $0" in line), f"token may leak: {line.strip()}"


def test_voice_verify_checks_401_with_http_code():
    """curl succeeds on 401 without -f, so the code must be captured and compared."""
    assert '%{http_code}' in VERIFY_VOICE
    assert '== "401"' in VERIFY_VOICE


def test_voice_verify_https_only_adds_insecure_for_self_signed():
    assert '[[ "$AURORA_URL" == https:* ]]' in VERIFY_VOICE
    assert "-k" in VERIFY_VOICE


def _extract_python_snippets(script: str) -> list[str]:
    """python3 -c '<snippet>' args use shell single quotes (no escapes inside)
    plus <<'PY' heredocs — extract and return them for syntax checking."""
    snippets = []
    idx = 0
    marker = "python3 -c '"
    while (start := script.find(marker, idx)) != -1:
        begin = start + len(marker)
        end = script.find("'", begin)  # next single quote closes the snippet
        assert end != -1, "unterminated python3 -c snippet"
        snippets.append(script[begin:end])
        idx = end + 1
    for m in re.finditer(r"<<'PY'[^\n]*\n(.*?)\nPY\n", script, re.S):
        snippets.append(m.group(1))
    return snippets


def test_voice_verify_embedded_python_is_version_compatible():
    """The old f-string with \\" escapes raised SyntaxError on Ubuntu's
    Python 3.8–3.11; every embedded snippet must now compile here."""
    snippets = _extract_python_snippets(VERIFY_VOICE)
    assert len(snippets) >= 4, "expected the embedded snippets to still exist"
    for snippet in snippets:
        ast.parse(snippet)  # raises SyntaxError on any version-incompatible code


def test_voice_verify_keeps_readonly_and_test_calls():
    # read-only probes of existing services
    assert 'GET /v1/models' in VERIFY_VOICE and 'curl -sS -m 10 "$TTS_URL/v1/models"' in VERIFY_VOICE
    assert 'for path in /health /status /' in VERIFY_VOICE and '"$TL_URL$path"' in VERIFY_VOICE
    # the explicitly requested one-shot test synthesis + transcription
    assert "POST /v1/audio/speech" in VERIFY_VOICE
    assert "/v1/audio/transcriptions" in VERIFY_VOICE
    # and nothing that mutates other services (scan code, not comments)
    code = "\n".join(l for l in VERIFY_VOICE.splitlines() if not l.lstrip().startswith("#"))
    for verb in ("systemctl restart", "systemctl stop", "kill ", "unload", "rm -rf /opt"):
        assert verb not in code, f"verify script must stay read-only, found: {verb}"


# ---------------------------------------------------------------- personalization
def test_portable_config_uses_eric():
    assert re.search(r'^user_name: "Eric"', PORTABLE_CFG, re.M)
    assert '"Alex"' not in PORTABLE_CFG


def test_aurora_identity_and_evolution_untouched():
    seed = (ROOT / "personas" / "default.json").read_text()
    assert "Eric" not in seed  # Aurora's own persona/identity unchanged
    block = PORTABLE_CFG.split("personality:")[1]
    assert "every_n_user_turns: 40" in block  # evolution mechanism untouched


# ---------------------------------------------------------------- TLS options
def test_tls_options_both_paths_enable_https():
    from run import tls_options
    from aurora.config import Config

    with pytest.MonkeyPatch.context() as mp:
        mp.chdir(ROOT)  # use two real files from the repo as stand-ins
        cert = ROOT / "personas" / "default.json"
        key = ROOT / "pytest.ini"
        opts = tls_options(Config({"server": {"tls_cert": str(cert), "tls_key": str(key)}}))
        assert opts["ssl_certfile"] == str(cert) and opts["ssl_keyfile"] == str(key)


def test_tls_options_off_by_default_and_partial_configs_ignored():
    from run import tls_options
    from aurora.config import Config

    assert tls_options(Config({})) == {}
    assert tls_options(Config({"server": {"tls_cert": "/nope.crt", "tls_key": "/nope.key"}})) == {}
    assert tls_options(Config({"server": {"tls_cert": "/only.crt"}})) == {}


# ---------------------------------------------------------------- permission model
def _apply_installer_modes(base: Path) -> None:
    """Replicate the installer's exact permission commands on a temp tree."""
    (base / "app").chmod(0o750)
    (base / "data").chmod(0o750)
    (base / "etc").chmod(0o750)
    (base / "etc" / "config.yaml").chmod(0o600)
    (base / "etc" / "aurora.env").chmod(0o640)


def test_permission_model_allows_read_and_persistence_and_protects_token(tmp_path):
    """Functional round-trip under the deployed modes: config readable,
    preferences persistable (voice selection), and the file stays 600 —
    i.e. the auth token is never exposed. (The uid switch itself needs
    root/Portable — see verify-permissions.sh, honestly SKIPped here.)"""
    from aurora.config import Config

    app = tmp_path / "app"
    data = tmp_path / "data"
    etc = tmp_path / "etc"
    for d in (app, data, etc):
        d.mkdir()
    (app / "run.py").write_text("# app tree\n")
    cfg_path = etc / "config.yaml"
    cfg_path.write_text("user_name: 'Eric'\nvoice:\n  tts: kayock\n  voice_id: am_adam\n")
    (etc / "aurora.env").write_text("AURORA_HOME=/opt/aurora-companion\n")
    _apply_installer_modes(tmp_path)

    # service account (same uid here) reads and persists a preference
    cfg = Config.load(cfg_path)
    assert cfg.get("user_name") == "Eric"
    cfg.set("voice.voice_id", "picard")
    cfg.save()
    reloaded = Config.load(cfg_path)
    assert reloaded.get("voice.voice_id") == "picard"

    # the write preserved the restrictive mode — token stays protected
    assert stat.S_IMODE(cfg_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(etc.stat().st_mode) in (0o750, 0o700)

    # and nothing in the tree is world-writable or world-readable secrets
    for path in etc.rglob("*"):
        mode = stat.S_IMODE(path.stat().st_mode)
        assert not mode & 0o007, f"world access on {path}: {oct(mode)}"


def test_verify_permissions_script_matches_the_model():
    assert 'chmod 600' in VERIFY_PERMS.replace('"', '') or "600" in VERIFY_PERMS
    assert "config.yaml owned by" in VERIFY_PERMS
    assert "-perm /o+rwx" in VERIFY_PERMS
    assert "runuser -u \"$AURORA_USER\"" in VERIFY_PERMS
    # honest skipping when there is no root/service account (sandbox)
    assert "SKIP" in VERIFY_PERMS and "requires root" in VERIFY_PERMS
    # TLS policy checks agree with make-cert.sh (dir 750, files 640/600,
    # never world-readable — including the public certificate)
    assert '$AURORA_ETC/tls' in VERIFY_PERMS
    assert '"640" || "$FMODE" == "600"' in VERIFY_PERMS
    assert '"750" && "$TOWN" == "root:$AURORA_USER"' in VERIFY_PERMS


def test_make_cert_tls_policy_is_internally_consistent():
    """Cert AND key 0640 root:aurora, tls dir 0750 root:aurora — matching the
    verifier's blanket no-world-access rule for /etc/aurora. No world-readable
    modes anywhere in the script."""
    assert re.search(r'chmod 0750 "\$OUT"', MAKE_CERT)
    assert re.search(r'chmod 0640 "\$CRT" "\$KEY"', MAKE_CERT)
    assert "0644" not in MAKE_CERT and "0600" not in MAKE_CERT  # single policy
    assert re.search(r'chown root:"\$SERVICE_USER" "\$OUT"', MAKE_CERT)
    assert "subjectAltName" in MAKE_CERT  # hostname + IP SANs for LAN browsers


def test_verify_voice_https_applies_to_unauthenticated_probe_too():
    assert re.search(r'TLS_ARGS=\(\)', VERIFY_VOICE)
    assert re.search(r'TLS_ARGS=\(-k\)', VERIFY_VOICE)
    # the 401 probe uses the same TLS args as the authenticated request and
    # accepts a received HTTP code even after a rude transport close
    start = VERIFY_VOICE.find("UNAUTH_CODE=")
    block = VERIFY_VOICE[start:start + 320]
    assert '"${TLS_ARGS[@]}"' in block
    assert "|| true" in block
    assert '=~ ^[0-9]{3}$' in VERIFY_VOICE  # code normalization (no 000-appending)


def test_config_defaults_carry_tls_keys():
    from aurora.config import DEFAULTS

    assert DEFAULTS["server"]["tls_cert"] == ""
    assert DEFAULTS["server"]["tls_key"] == ""


# ---------------------------------------------------------------- auth + voice endpoints
def test_voice_endpoints_respect_the_token_without_disabling_auth(tmp_path):
    """Voice verification must work with authentication ON: gated 401 without
    the token, full access with it — same rules as every other /api call."""
    from fastapi.testclient import TestClient

    from aurora.app import CompanionApp
    from aurora.clock import Clock
    from aurora.config import Config as AuroraConfig
    from aurora.server.app import create_server
    from tests.conftest import SEED, T0, mock_provider, standard_rules
    from tests.test_voice_selection import _adapter_stt, _adapter_tts

    import yaml
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "data_dir": str(tmp_path / "data"),
        "voice": {"tts": "kayock", "stt": "kayock"},
    }), encoding="utf-8")
    provider = mock_provider(standard_rules())
    app = CompanionApp(AuroraConfig.load(cfg_path), clock=Clock(T0), seed_path=SEED,
                       chat_provider=provider, background_provider=provider)
    app.voice.tts = _adapter_tts()
    app.voice.stt = _adapter_stt()
    app.config.set("server.auth_token", "deploy-check-token")

    client = TestClient(create_server(app))
    assert client.get("/api/voice/voices").status_code == 401
    assert client.get("/api/voice/status").status_code == 401
    ok = client.get("/api/voice/voices", headers={"X-Aurora-Token": "deploy-check-token"})
    assert ok.status_code == 200 and ok.json()["voices"], "voice discovery must work with auth on"
    status = client.get("/api/voice/status", headers={"Authorization": "Bearer deploy-check-token"})
    assert status.status_code == 200


# ---------------------------------------------------------------- functional HTTPS run
def test_verify_voice_over_self_signed_https(tmp_path):
    """End-to-end: make-cert.sh (non-root path — also checks its modes),
    a TLS-gated Aurora stand-in, and verify-voice.sh run against it.
    Confirms HTTPS authenticated requests succeed and unauthenticated ones
    get HTTP 401, without disabling auth or breaking on the self-signed cert."""
    if not (shutil.which("curl") and shutil.which("openssl")):
        pytest.skip("curl/openssl not available")
    port = _free_port()
    tls_dir = tmp_path / "tls"

    # 1. certificate via the real deploy script (its non-root code path)
    env = {**os.environ, "AURORA_ETC": str(tmp_path), "AURORA_USER": getpass.getuser(),
           "AURORA_HOSTNAME": "localhost", "AURORA_IP": "127.0.0.1"}
    cert_run = subprocess.run(["bash", str(DEPLOY / "make-cert.sh")],
                              capture_output=True, text=True, env=env, timeout=30)
    assert cert_run.returncode == 0, cert_run.stderr
    cert, key = tls_dir / "aurora.crt", tls_dir / "aurora.key"
    assert cert.is_file() and key.is_file()
    assert stat.S_IMODE(cert.stat().st_mode) == 0o640, "certificate must follow the 0640 policy"
    assert stat.S_IMODE(key.stat().st_mode) == 0o640

    # 2. minimal TLS status endpoint with the same auth contract as Aurora
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import ssl

    TOKEN = "https-regression-token"
    STATUS = (b'{"version":"1.2.0","voice":{"tts":"kayock","voice":"am_adam",'
              b'"stt":"kayock_whisper","stt_available":true},"resources":{}}')

    class TLSAurora(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_GET(self):
            if self.headers.get("Authorization") == f"Bearer {TOKEN}":
                self.send_response(200)
            else:
                self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(STATUS if self.headers.get("Authorization") else b'{"detail":"unauthorized"}')

    srv = ThreadingHTTPServer(("127.0.0.1", port), TLSAurora)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        # 3. the real checklist script over https
        run_env = {**os.environ, "AURORA_URL": f"https://127.0.0.1:{port}",
                   "AURORA_TOKEN": TOKEN,
                   "TTS_URL": "http://127.0.0.1:1",  # unreachable → FAILs are expected
                   "STT_URL": "http://127.0.0.1:1", "TL_URL": "http://127.0.0.1:1"}
        out = subprocess.run(["bash", str(DEPLOY / "verify-voice.sh")],
                             capture_output=True, text=True, env=run_env, timeout=120)
        script_output = out.stdout + out.stderr
        # authenticated HTTPS request succeeded
        assert "GET /api/status OK (authenticated)" in script_output
        # unauthenticated HTTPS probe is rejected with 401
        assert "requests WITHOUT the token are rejected (HTTP 401)" in script_output
        # and the Aurora section itself caused no failure
        assert "Aurora not answering" not in script_output
        # TLS handling was applied and the token never leaked
        assert "using --insecure for the LOCAL self-signed certificate only" in script_output
        assert TOKEN not in script_output
    finally:
        srv.shutdown()


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
