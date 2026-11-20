import io
import os
import sys
import json
import time
import zipfile
import threading
import urllib.request
import urllib.error
from unittest.mock import patch, MagicMock
import pytest
from PIL import Image

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    """Isolated test environment with temporary persistent files and zero host side effects."""
    sec_file = tmp_path / "security_config.json"
    notify_file = tmp_path / "notify_config.json"
    profiles_file = tmp_path / "account_profiles.json"
    rom_file = tmp_path / "roms" / "NRO.jar"
    emu_home = tmp_path / ".microemulator"
    rom_file.parent.mkdir(parents=True, exist_ok=True)
    emu_home.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("SECURITY_CONFIG_FILE", str(sec_file))
    monkeypatch.setenv("NOTIFY_CONFIG_FILE", str(notify_file))
    monkeypatch.setenv("PROFILES_CONFIG_FILE", str(profiles_file))
    monkeypatch.setenv("ROM_FILE", str(rom_file))
    monkeypatch.setenv("MICROEMULATOR_HOME", str(emu_home))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("FORCE_IMAGEGRAB", "1")
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)

    import app.server as srv

    monkeypatch.setattr(srv, "SECURITY_CONFIG_FILE", str(sec_file))
    monkeypatch.setattr(srv, "NOTIFY_CONFIG_FILE", str(notify_file))
    monkeypatch.setattr(srv, "PROFILES_CONFIG_FILE", str(profiles_file))
    monkeypatch.setattr(srv, "ROM_FILE", str(rom_file))
    srv.FAILED_LOGIN_ATTEMPTS.clear()
    srv.NOTIFY_HISTORY.clear()
    srv.invalidate_screenshot_cache()
    srv._nro_socket_cache["ts"] = 0.0
    srv._nro_socket_cache["online"] = None
    srv.app.config["TESTING"] = True
    monkeypatch.setitem(srv.app.config, "SESSION_COOKIE_SECURE", False)

    return srv, tmp_path


# =====================================================================
# 1. AUTHENTICATION, RATE LIMITING & CSRF SECURITY
# =====================================================================

def test_default_admin_password_rejected(app_env):
    srv, _ = app_env
    client = srv.app.test_client()

    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
    assert resp.status_code == 503
    assert "ADMIN_PASSWORD" in resp.get_json()["error"]
    assert not srv.verify_credentials("admin", "admin")


def test_env_admin_password_login_and_logout(app_env, monkeypatch):
    srv, _ = app_env
    monkeypatch.setenv("ADMIN_PASSWORD", "MySuperSecretPass123!")
    client = srv.app.test_client()

    # Unauthenticated / returns login page
    r_login_page = client.get("/")
    assert r_login_page.status_code == 200
    assert "Cổng Xác Thực" in r_login_page.get_data(as_text=True)

    # Wrong password fails with 401
    with patch("time.sleep"):
        r_bad = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert r_bad.status_code == 401

    # Valid login succeeds
    r_ok = client.post("/api/auth/login", json={"username": "admin", "password": "MySuperSecretPass123!"})
    assert r_ok.status_code == 200
    assert r_ok.headers.get("X-Content-Type-Options") == "nosniff"
    assert r_ok.headers.get("X-Frame-Options") == "DENY"
    assert r_ok.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"

    # Authenticated / returns main dashboard
    r_dash = client.get("/")
    assert r_dash.status_code == 200
    assert "NRO CYBER CLOUD" in r_dash.get_data(as_text=True)

    # Auth status
    r_st = client.get("/api/auth/status").get_json()
    assert r_st["authenticated"] is True
    assert r_st["is_default"] is False

    # Logout clears session
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/auth/status").get_json()["authenticated"] is False


def test_login_rate_limiting_and_reset(app_env, monkeypatch):
    srv, _ = app_env
    monkeypatch.setenv("ADMIN_PASSWORD", "MySuperSecretPass123!")
    client = srv.app.test_client()

    with patch("time.sleep"):
        for i in range(4):
            r = client.post("/api/auth/login", json={"username": "admin", "password": f"bad{i}"})
            assert r.status_code == 401
            assert r.get_json()["remaining"] == 4 - i

        r5 = client.post("/api/auth/login", json={"username": "admin", "password": "bad5"})
        assert r5.status_code == 429
        assert r5.get_json()["locked"] is True

        # Locked out even with right password
        r6 = client.post("/api/auth/login", json={"username": "admin", "password": "MySuperSecretPass123!"})
        assert r6.status_code == 429

    srv.reset_failed_attempts("127.0.0.1")
    assert client.post("/api/auth/login", json={"username": "admin", "password": "MySuperSecretPass123!"}).status_code == 200


def test_change_credentials_and_legacy_change_pin(app_env, monkeypatch):
    srv, tmp_path = app_env
    monkeypatch.setenv("ADMIN_PASSWORD", "InitialEnvPassword123")
    client = srv.app.test_client()

    client.post("/api/auth/login", json={"username": "admin", "password": "InitialEnvPassword123"})

    r_chg = client.post("/api/auth/change_credentials", json={
        "old_user": "admin",
        "old_pass": "InitialEnvPassword123",
        "new_user": "cyberboss",
        "new_pass": "NewStrongPass999!"
    })
    assert r_chg.status_code == 200

    saved = json.loads((tmp_path / "security_config.json").read_text(encoding="utf-8"))
    assert saved["admin_user"] == "cyberboss"
    assert "NewStrongPass999!" not in json.dumps(saved)
    assert len(saved["hashed_password"]) == 64

    assert not srv.verify_credentials("admin", "InitialEnvPassword123")
    assert srv.verify_credentials("cyberboss", "NewStrongPass999!")

    # Legacy /api/auth/change_pin alias
    r_pin = client.post("/api/auth/change_pin", json={
        "old_pin": "NewStrongPass999!",
        "new_pin": "UpdatedPin888!"
    })
    assert r_pin.status_code == 200
    assert srv.verify_credentials("cyberboss", "UpdatedPin888!")


def test_protected_endpoints_reject_unauthenticated(app_env):
    srv, _ = app_env
    client = srv.app.test_client()

    for ep in ["/api/screenshot", "/api/status", "/api/ping", "/api/game_status", "/api/notify_config", "/api/profiles", "/api/logs"]:
        assert client.get(ep).status_code in (401, 403), f"GET {ep} not protected"

    for ep in [
        "/api/input", "/api/quick_cmd", "/api/mod_action", "/api/auto_farm",
        "/api/xmap", "/api/stop_farm", "/api/open_account_dialog", "/api/select_server",
        "/api/quick_login", "/api/notify_config", "/api/notify_test", "/api/profiles",
        "/api/restart", "/api/upload_rom", "/api/auth/change_credentials"
    ]:
        assert client.post(ep, json={}).status_code in (401, 403), f"POST {ep} not protected"

    assert client.delete("/api/profiles?id=123").status_code in (401, 403)


def test_x_pin_header_api_authentication(app_env, monkeypatch):
    srv, _ = app_env
    monkeypatch.setenv("ADMIN_PASSWORD", "ApiAutomationToken123!")
    client = srv.app.test_client()

    r = client.get("/api/ping", headers={"X-PIN": "ApiAutomationToken123!"})
    assert r.status_code == 200
    assert r.get_json()["pong"] is True


def test_csrf_cross_origin_and_null_origin_blocked(app_env, monkeypatch):
    srv, _ = app_env
    monkeypatch.setenv("ADMIN_PASSWORD", "MySuperSecretPass123!")
    client = srv.app.test_client()
    client.post("/api/auth/login", json={"username": "admin", "password": "MySuperSecretPass123!"})

    # 1. Evil cross-origin Origin header -> 403
    r_evil = client.post(
        "/api/auth/change_credentials",
        headers={"Origin": "https://evil-attacker.example.com"},
        json={"old_user": "admin", "old_pass": "MySuperSecretPass123!", "new_user": "h", "new_pass": "hacked123"}
    )
    assert r_evil.status_code == 403

    # 2. Sandboxed iframe Origin: null -> 403
    r_null = client.post("/api/restart", headers={"Origin": "null"}, json={})
    assert r_null.status_code == 403

    # 3. Evil Referer when Origin absent -> 403
    r_ref = client.post("/api/restart", headers={"Referer": "https://phishing.example.org/page"}, json={})
    assert r_ref.status_code == 403

    # 4. Reverse proxy X-Forwarded-Host matching Origin -> Allowed!
    with patch.object(srv, "restart_emulator", return_value=True):
        r_proxy = client.post(
            "/api/restart",
            headers={"Origin": "https://my-nro.onrender.com", "X-Forwarded-Host": "my-nro.onrender.com"},
            json={}
        )
    assert r_proxy.status_code == 200


# =====================================================================
# 2. HEALTH, PING & SOCKET ENV VAR TESTS
# =====================================================================

def test_health_and_detailed_health_and_ping(app_env, monkeypatch):
    srv, _ = app_env
    monkeypatch.setenv("ADMIN_PASSWORD", "MySuperSecretPass123!")
    client = srv.app.test_client()

    assert client.get("/health").data == b"OK"

    with patch.object(srv, "get_bridge_status", return_value={"bridge_online": True}) as mock_bridge:
        r_hd = client.get("/health/detailed")
        assert r_hd.status_code == 200
        body = r_hd.get_json()
        assert body["web"] == "ok"
        assert body["emulator"] == "running"
        assert body["bridge"] == "online"
        assert mock_bridge.call_count == 1

    client.post("/api/auth/login", json={"username": "admin", "password": "MySuperSecretPass123!"})
    r_p = client.get("/api/ping")
    assert r_p.status_code == 200
    assert r_p.get_json()["pong"] is True


def test_check_nro_socket_env_vars_and_invalid_port(app_env, monkeypatch):
    srv, _ = app_env
    monkeypatch.setenv("NRO_SERVER_HOST", "10.20.30.40")
    monkeypatch.setenv("NRO_SERVER_PORT", "14446")

    with patch("socket.socket") as mock_sock_cls:
        mock_sock = MagicMock()
        mock_sock_cls.return_value = mock_sock
        assert srv.check_nro_socket() is True
        mock_sock.connect.assert_called_once_with(("10.20.30.40", 14446))

    # Empty or invalid port falls back safely to 14445
    for bad_port in ["", "   ", "invalid_port"]:
        monkeypatch.setenv("NRO_SERVER_PORT", bad_port)
        with patch("socket.socket") as mock_sock_cls:
            mock_sock = MagicMock()
            mock_sock_cls.return_value = mock_sock
            assert srv.check_nro_socket() is True
            mock_sock.connect.assert_called_once_with(("10.20.30.40", 14445))


# =====================================================================
# 3. SCREENSHOT PIPELINE, WEBP, BRIDGE FALLBACK & CACHE INVALIDATION
# =====================================================================

def test_screenshot_webp_jpeg_bridge_fallback_and_hd_download(app_env, monkeypatch):
    srv, _ = app_env
    monkeypatch.setenv("ADMIN_PASSWORD", "MySuperSecretPass123!")
    client = srv.app.test_client()
    client.post("/api/auth/login", json={"username": "admin", "password": "MySuperSecretPass123!"})

    fake_img = Image.new("RGB", (240, 360), color=(40, 120, 200))
    with patch("PIL.ImageGrab.grab", return_value=fake_img):
        r_webp = client.get("/api/screenshot", headers={"Accept": "image/webp,*/*"})
        assert r_webp.status_code == 200
        assert r_webp.mimetype == "image/webp"
        assert r_webp.data[:4] == b"RIFF" and r_webp.data[8:12] == b"WEBP"

        r_jpeg = client.get("/api/screenshot", headers={"Accept": "image/jpeg"})
        assert r_jpeg.status_code == 200
        assert r_jpeg.mimetype == "image/jpeg"
        assert r_jpeg.data[:2] == b"\xff\xd8"

        r_dl = client.get("/api/screenshot?download=1", headers={"Accept": "text/html,image/webp,*/*"})
        assert r_dl.status_code == 200
        assert r_dl.mimetype == "image/jpeg"
        assert r_dl.data[:2] == b"\xff\xd8"
        assert "attachment" in r_dl.headers.get("Content-Disposition", "")

    # Bridge JPEG -> WebP conversion when X11 is black
    srv.invalidate_screenshot_cache()
    black_img = Image.new("RGB", (240, 360), color=(0, 0, 0))
    bridge_buf = io.BytesIO()
    Image.new("RGB", (240, 360), color=(220, 90, 40)).save(bridge_buf, format="JPEG", quality=75)

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = bridge_buf.getvalue()
    mock_resp.__enter__.return_value = mock_resp

    with patch("PIL.ImageGrab.grab", return_value=black_img), \
         patch("urllib.request.urlopen", return_value=mock_resp):
        r_bw = client.get("/api/screenshot", headers={"Accept": "image/webp"})
        assert r_bw.status_code == 200
        assert r_bw.mimetype == "image/webp"
        assert r_bw.data[:4] == b"RIFF" and r_bw.data[8:12] == b"WEBP"


def test_screenshot_cache_hit_and_post_input_invalidation(app_env, monkeypatch):
    srv, _ = app_env
    monkeypatch.setenv("ADMIN_PASSWORD", "MySuperSecretPass123!")
    client = srv.app.test_client()
    client.post("/api/auth/login", json={"username": "admin", "password": "MySuperSecretPass123!"})

    frame1 = Image.new("RGB", (240, 360), color=(10, 20, 30))
    frame2 = Image.new("RGB", (240, 360), color=(200, 210, 220))

    with patch("PIL.ImageGrab.grab", side_effect=[frame1, frame2]) as mock_grab, \
         patch.object(srv, "bridge_call", return_value={"status": "ok"}):
        r1 = client.get("/api/screenshot", headers={"Accept": "image/jpeg"})
        r1_cached = client.get("/api/screenshot", headers={"Accept": "image/jpeg"})
        assert r1.data == r1_cached.data
        assert mock_grab.call_count == 1

        # Action invalidates cache immediately
        assert client.post("/api/input", json={"action": "key", "value": "ok"}).status_code == 200

        r2 = client.get("/api/screenshot", headers={"Accept": "image/jpeg"})
        assert mock_grab.call_count == 2
        assert r2.data != r1.data


# =====================================================================
# 4. GAME INPUT, AUTO FARM, XMAP, QUICK LOGIN & SERVER SWITCH
# =====================================================================

def test_input_actions_key_click_text_clear_antistuck(app_env, monkeypatch):
    srv, _ = app_env
    monkeypatch.setenv("ADMIN_PASSWORD", "MySuperSecretPass123!")
    client = srv.app.test_client()
    client.post("/api/auth/login", json={"username": "admin", "password": "MySuperSecretPass123!"})

    # Reject disallowed key
    assert client.post("/api/input", json={"action": "key", "value": "SuperMetaKey"}).status_code == 400
    # Reject invalid coordinates
    assert client.post("/api/input", json={"action": "click", "x": "abc", "y": 10}).status_code == 400
    # Reject unknown action
    assert client.post("/api/input", json={"action": "unknown"}).status_code == 400

    with patch.object(srv, "bridge_call", return_value={"status": "ok"}) as mock_bc:
        assert client.post("/api/input", json={"action": "key", "value": "up"}).get_json()["method"] == "bridge"
        assert client.post("/api/input", json={"action": "click", "x": 300, "y": -5}).get_json()["method"] == "bridge"
        # Verify coordinates were clamped to [0..239] and [0..359]
        click_url = mock_bc.call_args[0][0]
        assert "x=239&y=0" in click_url

        assert client.post("/api/input", json={"action": "text", "value": "user@nro.vn", "field": "user", "f2": True}).status_code == 200
        assert client.post("/api/input", json={"action": "clear_text"}).status_code == 200

    with patch.object(srv, "is_emulator_running", return_value=True), \
         patch.object(srv, "focus_game_window", return_value=None), \
         patch.object(srv, "safe_run", return_value=MagicMock(returncode=0)):
        assert client.post("/api/input", json={"action": "anti_stuck"}).status_code == 200


def test_auto_farm_xmap_stop_farm_quick_cmd(app_env, monkeypatch):
    srv, _ = app_env
    monkeypatch.setenv("ADMIN_PASSWORD", "MySuperSecretPass123!")
    client = srv.app.test_client()
    client.post("/api/auth/login", json={"username": "admin", "password": "MySuperSecretPass123!"})

    # Xmap validation
    assert client.post("/api/xmap", json={"map": "-1"}).status_code == 400
    assert client.post("/api/xmap", json={"map": "xmap"}).status_code == 400

    with patch.object(srv, "bridge_call", return_value={"status": "ok", "traveling": True, "map": "15"}), \
         patch.object(srv, "configure_punch_via_bridge", return_value={"status": "ok", "skill": 1}):
        r_af = client.post("/api/auto_farm", json={"map": "15", "zone": "5", "mode": "all"})
        assert r_af.status_code == 200
        assert r_af.get_json()["traveling"] is True

        r_xm = client.post("/api/xmap", json={"map": "15"})
        assert r_xm.status_code == 200
        assert r_xm.get_json()["map"] == 15

        r_sf = client.post("/api/stop_farm", json={})
        assert r_sf.status_code == 200

        r_qc = client.post("/api/quick_cmd", json={"cmd": "k5,tdlt"})
        assert r_qc.status_code == 200


def test_quick_login_and_select_server(app_env, monkeypatch):
    srv, _ = app_env
    monkeypatch.setenv("ADMIN_PASSWORD", "MySuperSecretPass123!")
    client = srv.app.test_client()
    client.post("/api/auth/login", json={"username": "admin", "password": "MySuperSecretPass123!"})

    assert client.post("/api/select_server", json={"server": 0}).status_code == 400
    assert client.post("/api/select_server", json={"server": 14}).status_code == 400

    with patch.object(srv, "login_via_bridge", return_value=True):
        r_sv = client.post("/api/select_server", json={"server": 7})
        assert r_sv.status_code == 200
        assert r_sv.get_json()["method"] == "direct_jvm"

        r_ql = client.post("/api/quick_login", json={"username": "vegeta@nro.vn", "password": "pw", "server": 4})
        assert r_ql.status_code == 200
        assert r_ql.get_json()["method"] == "direct_jvm"

    saved = srv.read_rms_saved_info()
    assert saved["username"] == "vegeta@nro.vn"
    assert saved["server"] == 4


# =====================================================================
# 5. WATCHDOG, BACKOFF, RMS, PROFILES, NOTIFICATIONS & ROM UPLOAD
# =====================================================================

def test_watchdog_unbound_local_st_and_adaptive_sleep(app_env):
    srv, _ = app_env

    class StopLoop(BaseException):
        pass

    # Case 1: Exception before `st` is set -> sleeps 2.0s without UnboundLocalError
    sleeps_err = []
    def fake_sleep_err(sec):
        sleeps_err.append(sec)
        if len(sleeps_err) >= 2:
            raise StopLoop()

    with patch.object(srv, "is_emulator_running", side_effect=RuntimeError("proc error")), \
         patch("time.sleep", side_effect=fake_sleep_err):
        with pytest.raises(StopLoop):
            srv.emulator_ui_watchdog_worker()
    assert sleeps_err == [15.0, 2.0]

    # Case 2: Emulator running and in_game=True -> sleeps 5.0s
    sleeps_ok = []
    def fake_sleep_ok(sec):
        sleeps_ok.append(sec)
        if len(sleeps_ok) >= 2:
            raise StopLoop()

    with patch.object(srv, "is_emulator_running", return_value=True), \
         patch.object(srv, "get_bridge_status", return_value={"midlet_loaded": True, "in_game": True}), \
         patch("time.sleep", side_effect=fake_sleep_ok):
        with pytest.raises(StopLoop):
            srv.emulator_ui_watchdog_worker()
    assert sleeps_ok == [15.0, 5.0]


def test_bridge_call_exponential_backoff(app_env):
    srv, _ = app_env
    sleeps = []
    with patch("urllib.request.urlopen", side_effect=ConnectionRefusedError("Connection refused")), \
         patch("time.sleep", side_effect=lambda s: sleeps.append(round(s, 2))):
        with pytest.raises(ConnectionRefusedError):
            srv.bridge_call("http://127.0.0.1:28888/api/game/status", retries=4)
    assert sleeps == [0.3, 0.6, 1.2]


def test_rms_binary_format_and_corrupt_cache_cleanup(app_env):
    srv, tmp_path = app_env
    raw = srv.make_rs_bytes("vjacc", {1: "ngọcrồng@vn", 2: 7})
    sample_rs = tmp_path / "sample.rs"
    sample_rs.write_bytes(raw)
    store_name, recs = srv.read_rs_records(str(sample_rs))
    assert store_name == "vjacc"
    assert recs[1].decode("utf-8") == "ngọcrồng@vn"
    assert recs[2] == bytes([7])

    srv.write_rms_account("Piccolo@Namek.vn", "Makankosappo", server_num=9)
    info = srv.read_rms_saved_info()
    assert info == {"username": "piccolo@namek.vn", "server": 9, "has_saved": True}

    sdir = srv.get_rms_suite_dir()
    bad_cache = os.path.join(sdir, "RMS_Image_Corrupt.rs")
    with open(bad_cache, "wb") as f:
        f.write(raw)
    assert srv.cleanup_corrupted_rms_cache() >= 1
    assert not os.path.exists(bad_cache)
    assert srv.read_rms_saved_info()["username"] == "piccolo@namek.vn"


def test_profiles_encryption_tamper_detection_and_legacy_migration(app_env, monkeypatch):
    srv, tmp_path = app_env
    monkeypatch.setenv("ADMIN_PASSWORD", "MySuperSecretPass123!")
    client = srv.app.test_client()
    client.post("/api/auth/login", json={"username": "admin", "password": "MySuperSecretPass123!"})

    # Legacy plaintext profile migration check
    legacy_data = [{"id": "leg1", "label": "Legacy", "username": "gohan", "password": "LegacyPlainPass", "server": 2}]
    (tmp_path / "account_profiles.json").write_text(json.dumps(legacy_data), encoding="utf-8")
    loaded_legacy = srv.load_account_profiles()
    assert loaded_legacy[0]["password"] == "LegacyPlainPass"

    # Saving new profile encrypts BOTH legacy and new profiles on disk!
    r_add = client.post("/api/profiles", json={
        "label": "Acc Chính",
        "username": "goku@gmail.com",
        "password": "PlaintextGamePass777!",
        "server": 3,
        "map": "15",
        "zone": "8"
    })
    assert r_add.status_code == 200

    raw_disk = (tmp_path / "account_profiles.json").read_text(encoding="utf-8")
    assert "PlaintextGamePass777!" not in raw_disk
    assert "LegacyPlainPass" not in raw_disk
    assert "enc:v1:" in raw_disk

    # Verify round-trip decryption
    r_get = client.get("/api/profiles").get_json()["profiles"]
    by_user = {p["username"]: p for p in r_get}
    assert by_user["goku@gmail.com"]["password"] == "PlaintextGamePass777!"
    assert by_user["gohan"]["password"] == "LegacyPlainPass"

    # Tamper detection: corrupting ciphertext returns "" instead of garbage
    disk_json = json.loads(raw_disk)
    disk_json[0]["password"] = disk_json[0]["password"][:-4] + "AAAA"
    (tmp_path / "account_profiles.json").write_text(json.dumps(disk_json), encoding="utf-8")
    tampered = srv.load_account_profiles()
    assert tampered[0]["password"] == ""


def test_notify_config_and_test_dispatch(app_env, monkeypatch):
    srv, _ = app_env
    monkeypatch.setenv("ADMIN_PASSWORD", "MySuperSecretPass123!")
    client = srv.app.test_client()
    client.post("/api/auth/login", json={"username": "admin", "password": "MySuperSecretPass123!"})

    r_save = client.post("/api/notify_config", json={
        "enabled": True,
        "telegram_bot_token": "123456:FAKE_TOKEN",
        "telegram_chat_id": "-100999999",
        "discord_webhook_url": "https://discord.com/api/webhooks/123/abc",
        "notify_skh": True,
        "notify_full_bag": True,
        "notify_disconnect": True
    })
    assert r_save.status_code == 200

    mock_http_resp = MagicMock()
    mock_http_resp.status = 200
    mock_http_resp.__enter__.return_value = mock_http_resp

    with patch.object(srv, "capture_game_screenshot_bytes", return_value=b"\xff\xd8\xff\xe0fakejpeg"), \
         patch.object(srv, "get_bridge_status", return_value={"in_game": True, "char_name": "kakarot", "map_name": "Đồi hoang", "map_id": 15, "zone": 3, "power": 1500000, "skh_count": 2}), \
         patch("urllib.request.urlopen", return_value=mock_http_resp) as mock_url:
        r_test = client.post("/api/notify_test", json={})
        assert r_test.status_code == 200
        assert len(r_test.get_json()["results"]) == 2
        assert mock_url.call_count == 2


def test_upload_rom_and_mod_action_validation(app_env, monkeypatch):
    srv, tmp_path = app_env
    monkeypatch.setenv("ADMIN_PASSWORD", "MySuperSecretPass123!")
    client = srv.app.test_client()
    client.post("/api/auth/login", json={"username": "admin", "password": "MySuperSecretPass123!"})

    assert client.post("/api/upload_rom", data={"file": (io.BytesIO(b"x"), "bad.txt")}, content_type="multipart/form-data").status_code == 400
    assert client.post("/api/upload_rom", data={"file": (io.BytesIO(b"notzip"), "bad.jar")}, content_type="multipart/form-data").status_code == 400

    jar_buf = io.BytesIO()
    with zipfile.ZipFile(jar_buf, "w") as zf:
        zf.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
    jar_buf.seek(0)
    with patch.object(srv, "restart_emulator", return_value=True):
        assert client.post("/api/upload_rom", data={"file": (jar_buf, "NRO.jar")}, content_type="multipart/form-data").status_code == 200
    assert zipfile.is_zipfile(tmp_path / "roms" / "NRO.jar")

    # Mod action validation
    for invalid_cmd in ["rm -rf /", "skill0", "skill11", "skill1 12", "unknown"]:
        assert client.post("/api/mod_action", json={"cmd": invalid_cmd}).status_code == 400

    with patch.object(srv, "send_mod_actions_via_bridge", return_value={"status": "ok"}):
        assert client.post("/api/mod_action", json={"cmd": "cd,locdo,anhat,gb,s20,k0,k29,skill1,skill5 3,tdlt,ts,ats"}).status_code == 200


def test_live_http_server_end_to_end(app_env, monkeypatch):
    """Spins up a real local WSGI HTTP server on an ephemeral port and verifies live TCP HTTP traffic."""
    from werkzeug.serving import make_server

    srv, _ = app_env
    monkeypatch.setenv("ADMIN_PASSWORD", "LiveServerPass12345!")

    http_server = make_server("127.0.0.1", 0, srv.app)
    port = http_server.server_port
    thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    thread.start()

    try:
        base_url = f"http://127.0.0.1:{port}"

        # 1. Live GET /health
        with urllib.request.urlopen(f"{base_url}/health", timeout=3) as resp:
            assert resp.status == 200
            assert resp.read() == b"OK"

        # 2. Live GET /api/ping without auth -> HTTP 403
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"{base_url}/api/ping", timeout=3)
        assert exc_info.value.code == 403

        # 3. Live GET /api/ping with X-PIN header -> HTTP 200
        req = urllib.request.Request(f"{base_url}/api/ping", headers={"X-PIN": "LiveServerPass12345!"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            assert resp.status == 200
            payload = json.loads(resp.read().decode("utf-8"))
            assert payload["pong"] is True
    finally:
        http_server.shutdown()


# =====================================================================
# 6. HARDENING, CONCURRENCY, BRIDGE ERROR & REGRESSION TESTS
# =====================================================================

def test_empty_session_secret_fallback_and_session_invalidation_on_password_change(app_env, monkeypatch):
    import importlib

    srv, _ = app_env
    monkeypatch.setenv("ADMIN_PASSWORD", "InitialStrongPass123!")
    monkeypatch.setenv("SESSION_SECRET", "")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "1")
    # Verify empty SESSION_SECRET falls back to a random non-empty secret key on module load
    monkeypatch.setenv("DISABLE_BACKGROUND_WORKERS", "1")
    reloaded = importlib.reload(srv)
    assert isinstance(reloaded.app.secret_key, str) and len(reloaded.app.secret_key) >= 32
    assert reloaded.app.config["SESSION_COOKIE_SECURE"] is True

    # Restore isolated test paths on reloaded module
    monkeypatch.setattr(reloaded, "SECURITY_CONFIG_FILE", srv.SECURITY_CONFIG_FILE)
    monkeypatch.setattr(reloaded, "NOTIFY_CONFIG_FILE", srv.NOTIFY_CONFIG_FILE)
    monkeypatch.setattr(reloaded, "PROFILES_CONFIG_FILE", srv.PROFILES_CONFIG_FILE)
    monkeypatch.setattr(reloaded, "ROM_FILE", srv.ROM_FILE)
    reloaded.app.config["TESTING"] = True

    # Verify Set-Cookie emits Secure, HttpOnly, SameSite=Lax behind HTTPS proxy
    proxy_client = reloaded.app.test_client()
    r_login_sec = proxy_client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "InitialStrongPass123!"},
        headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "my-nro.onrender.com"},
    )
    assert r_login_sec.status_code == 200
    set_cookie = r_login_sec.headers.get("Set-Cookie", "")
    assert "Secure" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=Lax" in set_cookie

    # Disable SESSION_COOKIE_SECURE for plain HTTP test_client cookie jar round-trips below
    reloaded.app.config["SESSION_COOKIE_SECURE"] = False
    client_a = reloaded.app.test_client()
    client_b = reloaded.app.test_client()

    assert client_a.post("/api/auth/login", json={"username": "admin", "password": "InitialStrongPass123!"}).status_code == 200
    assert client_b.post("/api/auth/login", json={"username": "admin", "password": "InitialStrongPass123!"}).status_code == 200

    csrf_a = client_a.get("/api/auth/status").get_json()["csrf_token"]
    assert len(csrf_a) >= 32

    # client_a changes password -> client_a stays valid with same CSRF token, client_b is kicked out immediately
    r_chg = client_a.post(
        "/api/auth/change_credentials",
        headers={"X-CSRF-Token": csrf_a},
        json={
            "old_user": "admin",
            "old_pass": "InitialStrongPass123!",
            "new_user": "admin",
            "new_pass": "RotatedStrongPass456!"
        },
    )
    assert r_chg.status_code == 200
    assert client_a.get("/api/ping").status_code == 200
    # Current tab's existing CSRF token still works right after password change
    with patch.object(reloaded, "restart_emulator", return_value=True):
        assert client_a.post("/api/restart", headers={"X-CSRF-Token": csrf_a}, json={}).status_code == 200

    # Stale client_b session cookie is invalidated
    assert client_b.get("/api/auth/status").get_json()["authenticated"] is False
    assert client_b.get("/api/ping").status_code == 403


def test_csrf_hardening_tokens_sec_fetch_site_referer_and_x_pin_bypass(app_env, monkeypatch):
    srv, _ = app_env
    monkeypatch.setenv("ADMIN_PASSWORD", "MySuperSecretPass123!")
    client = srv.app.test_client()
    assert client.post("/api/auth/login", json={"username": "admin", "password": "MySuperSecretPass123!"}).status_code == 200

    csrf_tok = client.get("/api/auth/status").get_json()["csrf_token"]
    assert isinstance(csrf_tok, str) and len(csrf_tok) >= 32

    # 1. Malformed Referer with empty netloc -> 403
    assert client.post("/api/restart", headers={"Referer": "about:blank"}, json={}).status_code == 403
    assert client.post("/api/restart", headers={"Referer": "not-a-valid-url"}, json={}).status_code == 403

    # 2. Cross-site Sec-Fetch-Site even without Origin/Referer -> 403
    assert client.post("/api/restart", headers={"Sec-Fetch-Site": "cross-site"}, json={}).status_code == 403

    # 3. X-PIN header cannot bypass CSRF when authenticated via session cookie
    assert client.post(
        "/api/restart",
        headers={"Origin": "https://evil.example", "X-PIN": "bogus"},
        json={},
    ).status_code == 403

    # 4. Invalid / empty X-CSRF-Token on authenticated session -> 403
    r_bad_tok = client.post("/api/restart", headers={"X-CSRF-Token": "wrong-token"}, json={})
    assert r_bad_tok.status_code == 403
    assert "CSRF" in r_bad_tok.get_json()["error"]
    assert client.post("/api/restart", headers={"X-CSRF-Token": ""}, json={}).status_code == 403

    # 5. Valid X-CSRF-Token + Sec-Fetch-Site: same-origin + Render :443 default port normalization -> 200
    with patch.object(srv, "restart_emulator", return_value=True):
        r_ok = client.post(
            "/api/restart",
            headers={
                "Origin": "https://my-nro.onrender.com",
                "X-Forwarded-Host": "my-nro.onrender.com:443",
                "Sec-Fetch-Site": "same-origin",
                "X-CSRF-Token": csrf_tok,
            },
            json={},
        )
    assert r_ok.status_code == 200


def test_x_pin_rate_limiting_and_non_dict_json_payloads(app_env, monkeypatch):
    srv, _ = app_env
    monkeypatch.setenv("ADMIN_PASSWORD", "MySuperSecretPass123!")
    client = srv.app.test_client()

    # Repeated bad X-PIN header attempts trigger IP rate limit
    for i in range(srv.MAX_FAILED_ATTEMPTS):
        assert client.get("/api/ping", headers={"X-PIN": f"wrong-{i}"}).status_code == 403
    assert srv.is_ip_rate_limited("127.0.0.1") is True
    # Even the valid X-PIN is blocked until rate limit resets
    assert client.get("/api/ping", headers={"X-PIN": "MySuperSecretPass123!"}).status_code == 403
    srv.reset_failed_attempts("127.0.0.1")
    assert client.get("/api/ping", headers={"X-PIN": "MySuperSecretPass123!"}).status_code == 200

    # Non-dict JSON payloads never cause HTTP 500
    client.post("/api/auth/login", json={"username": "admin", "password": "MySuperSecretPass123!"})
    for bad_body in ["[]", "123", '"hello"']:
        for ep in ["/api/input", "/api/mod_action", "/api/xmap", "/api/quick_login", "/api/profiles", "/api/notify_test"]:
            resp = client.post(ep, data=bad_body, content_type="application/json")
            assert resp.status_code == 400, f"POST {ep} with {bad_body} returned {resp.status_code}"


def test_spacebar_and_input_validation_fixes(app_env, monkeypatch):
    srv, _ = app_env
    monkeypatch.setenv("ADMIN_PASSWORD", "MySuperSecretPass123!")
    client = srv.app.test_client()
    client.post("/api/auth/login", json={"username": "admin", "password": "MySuperSecretPass123!"})

    with patch.object(srv, "bridge_call", return_value={"status": "ok"}) as mock_bc:
        r_space = client.post("/api/input", json={"action": "key", "value": " "})
        assert r_space.status_code == 200
        assert "key=space" in mock_bc.call_args[0][0]

    assert client.post("/api/input", json={"action": "key", "value": "   "}).status_code == 400
    assert client.post("/api/input", json={"action": "click", "x": "NaN", "y": 10}).status_code == 400
    assert client.post("/api/input", json={"action": "click", "x": "inf", "y": 10}).status_code == 400
    assert client.post("/api/xmap", json={"map": "-5"}).status_code == 400
    assert client.post("/api/xmap", json={"map": "--5"}).status_code == 400
    assert client.post("/api/xmap", json={"map": "1000"}).status_code == 400
    assert client.post("/api/auto_farm", json={"mode": "invalid_mode"}).status_code == 400
    assert client.post("/api/quick_login", json={"username": "goku", "server": 14}).status_code == 400
    assert client.post("/api/quick_login", json={"username": "goku", "server": -1}).status_code == 400
    assert client.post("/api/mod_action", json={"cmd": "k999"}).status_code == 400

    # write_rms_account clamps server index safely to 0..12 (Server 1..13)
    srv.write_rms_account("broly@nro.vn", "pw", server_num=14)
    assert srv.read_rms_saved_info()["server"] == 13


def test_bridge_call_http_error_json_extraction_and_malformed_json(app_env, monkeypatch):
    srv, _ = app_env
    monkeypatch.setenv("ADMIN_PASSWORD", "MySuperSecretPass123!")
    client = srv.app.test_client()
    client.post("/api/auth/login", json={"username": "admin", "password": "MySuperSecretPass123!"})

    err_payload = io.BytesIO(b'{"status":"error","error":"Unsupported mod action: bogus"}')
    http_err = urllib.error.HTTPError(
        url="http://127.0.0.1:28888/api/game/mod_action?cmd=cd",
        code=400,
        msg="OK",
        hdrs=None,
        fp=err_payload,
    )
    sleeps = []
    with patch("urllib.request.urlopen", side_effect=http_err) as mock_open, \
         patch("time.sleep", side_effect=lambda s: sleeps.append(s)):
        resp = client.post("/api/mod_action", json={"cmd": "cd"})
    assert resp.status_code == 400
    assert "Unsupported mod action: bogus" in resp.get_json()["error"]
    assert mock_open.call_count == 1
    assert sleeps == []

    # Malformed non-JSON 200 response raises BridgeError cleanly; get_bridge_status returns None
    bad_resp = MagicMock()
    bad_resp.status = 200
    bad_resp.read.return_value = b"{not valid json"
    bad_resp.__enter__.return_value = bad_resp
    with patch("urllib.request.urlopen", return_value=bad_resp):
        with pytest.raises(srv.BridgeError):
            srv.bridge_call("http://127.0.0.1:28888/api/game/status", retries=1)
        assert srv.get_bridge_status() is None


def test_upload_rom_restart_runtime_error_returns_json_500(app_env, monkeypatch):
    srv, tmp_path = app_env
    monkeypatch.setenv("ADMIN_PASSWORD", "MySuperSecretPass123!")
    client = srv.app.test_client()
    client.post("/api/auth/login", json={"username": "admin", "password": "MySuperSecretPass123!"})

    jar_buf = io.BytesIO()
    with zipfile.ZipFile(jar_buf, "w") as zf:
        zf.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
    jar_buf.seek(0)

    with patch.object(srv, "restart_emulator", side_effect=RuntimeError("no supervisor")):
        resp = client.post("/api/upload_rom", data={"file": (jar_buf, "NRO.jar")}, content_type="multipart/form-data")
    assert resp.status_code == 500
    assert resp.is_json
    assert "khởi động lại emulator" in resp.get_json()["error"]
    assert zipfile.is_zipfile(tmp_path / "roms" / "NRO.jar")


def test_rms_suite_dirs_host_isolation(app_env, monkeypatch):
    srv, tmp_path = app_env
    isolated = tmp_path / "isolated_microemu"
    monkeypatch.setenv("MICROEMULATOR_HOME", str(isolated))

    dirs = srv.get_all_suite_dirs()
    assert len(dirs) >= 2
    isolated_norm = os.path.normcase(os.path.abspath(str(isolated)))
    for d in dirs:
        d_norm = os.path.normcase(os.path.abspath(d))
        assert d_norm.startswith(isolated_norm), f"Suite dir escaped MICROEMULATOR_HOME: {d}"


def test_check_nro_socket_closes_on_exception_and_cgroup_ram(app_env):
    srv, _ = app_env

    # Socket is always closed even when connect() raises
    with patch("socket.socket") as mock_sock_cls:
        mock_sock = MagicMock()
        mock_sock.connect.side_effect = TimeoutError("timed out")
        mock_sock_cls.return_value = mock_sock
        assert srv.check_nro_socket() is False
        mock_sock.close.assert_called_once()

    # cgroup v2 container memory parsing (512MB limit, 256MB current, 56MB inactive_file -> 200MB used)
    fake_files = {
        "/sys/fs/cgroup/memory.max": str(512 * 1024 * 1024),
        "/sys/fs/cgroup/memory.current": str(256 * 1024 * 1024),
        "/sys/fs/cgroup/memory.stat": f"anon 100\ninactive_file {56 * 1024 * 1024}\n",
    }
    real_open = open
    def fake_open(path, *args, **kwargs):
        if path in fake_files:
            return io.StringIO(fake_files[path])
        return real_open(path, *args, **kwargs)

    with patch("builtins.open", side_effect=fake_open):
        cg = srv._read_cgroup_ram_info(host_total_mb=16384)
    assert cg == {"used_mb": 200, "total_mb": 512, "percent": 39.1}


def test_screenshot_concurrency_no_stampede_and_invalidation_generation_guard(app_env):
    srv, _ = app_env
    srv.invalidate_screenshot_cache()

    calls = 0
    call_lock = threading.Lock()
    def slow_capture(quality=55, fmt="jpeg"):
        nonlocal calls
        with call_lock:
            calls += 1
        time.sleep(0.08)
        return b"\xff\xd8\xff\xe0frame-shared"

    results = []
    with patch.object(srv, "capture_game_screenshot_bytes", side_effect=slow_capture):
        threads = [
            threading.Thread(target=lambda: results.append(srv.get_cached_screenshot(max_age=2.0, fmt="jpeg")))
            for _ in range(6)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert calls == 1
    assert len(results) == 6
    assert all(r == b"\xff\xd8\xff\xe0frame-shared" for r in results)

    # Invalidation mid-capture must prevent stale frame from being cached
    srv.invalidate_screenshot_cache()
    entered = threading.Event()
    release = threading.Event()
    def racing_capture(quality=55, fmt="jpeg"):
        entered.set()
        release.wait(timeout=2.0)
        return b"\xff\xd8\xff\xe0stale-frame"

    with patch.object(srv, "capture_game_screenshot_bytes", side_effect=racing_capture):
        t = threading.Thread(target=lambda: srv.get_cached_screenshot(max_age=5.0, fmt="jpeg"))
        t.start()
        assert entered.wait(timeout=2.0)
        srv.invalidate_screenshot_cache()
        release.set()
        t.join()

    assert "jpeg" not in srv._screenshot_cache


def test_notify_ssrf_validation_and_profiles_delete_and_concurrent_key(app_env, monkeypatch):
    srv, _ = app_env
    monkeypatch.setenv("ADMIN_PASSWORD", "MySuperSecretPass123!")
    client = srv.app.test_client()
    client.post("/api/auth/login", json={"username": "admin", "password": "MySuperSecretPass123!"})

    # Block SSRF / non-Discord webhook URLs and malformed Telegram tokens
    for bad_url in ["file:///etc/passwd", "http://127.0.0.1:28888/api/game/status", "https://evil.example/api/webhooks/1/2"]:
        assert client.post("/api/notify_config", json={"discord_webhook_url": bad_url}).status_code == 400
        assert client.post("/api/notify_test", json={"discord_webhook_url": bad_url}).status_code == 400
    assert client.post("/api/notify_config", json={"telegram_bot_token": "123:abc/../../etc"}).status_code == 400

    # Handle power=None in /api/notify_test without TypeError
    mock_http_resp = MagicMock()
    mock_http_resp.status = 204
    mock_http_resp.__enter__.return_value = mock_http_resp
    with patch.object(srv, "capture_game_screenshot_bytes", return_value=b"\xff\xd8\xff\xe0jpg"), \
         patch.object(srv, "get_bridge_status", return_value={"in_game": True, "char_name": "goku", "power": None}), \
         patch("urllib.request.urlopen", return_value=mock_http_resp):
        r_nt = client.post("/api/notify_test", json={"discord_webhook_url": "https://discord.com/api/webhooks/123/xyz"})
    assert r_nt.status_code == 200

    # DELETE /api/profiles returns 500 when save fails
    with patch.object(srv, "save_account_profiles", return_value=False):
        r_del = client.delete("/api/profiles", json={"id": "abc123"})
    assert r_del.status_code == 500

    # Concurrent _get_profile_cipher_key across threads yields one identical 32-byte key
    keys = []
    threads = [threading.Thread(target=lambda: keys.append(srv._get_profile_cipher_key())) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(keys) == 8
    assert len(set(keys)) == 1 and len(keys[0]) == 32
