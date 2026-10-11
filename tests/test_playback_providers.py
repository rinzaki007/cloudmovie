from __future__ import annotations

from types import SimpleNamespace

import pytest

from moviesync.app import create_app
from moviesync.card_plugins import CardFilePluginManager
from moviesync.cards import CardManifest, CardRegistry, PlaybackProviderCard
from moviesync.clients.quark import QuarkClient
from moviesync.services.playback_providers import PlaybackProviderManager


class FakePlaybackCard(PlaybackProviderCard):
    manifest = CardManifest(
        id="fake-playback",
        name="Fake Playback",
        type="playback_provider",
        capabilities=("playback.list_files", "playback.resolve"),
    )

    def is_configured(self, config):
        return True

    def list_files(self, parent_fid="0"):
        return [{"fid": "video-1", "file_name": "sample.mp4", "is_dir": False, "is_video": True}]

    def resolve_playback(self, fid):
        return {
            "fid": fid,
            "file_name": "sample.mp4",
            "url": "https://media.example/video.mp4",
            "mime_type": "video/mp4",
        }


class FakeStore:
    def load(self):
        return {"cards": {}}


class FakeLogger:
    def exception(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass


def test_playback_provider_manager_dispatches_by_card_contract_and_honors_disabled_state():
    registry = CardRegistry()
    registry.register(FakePlaybackCard())
    store = FakeStore()
    manager = PlaybackProviderManager(registry, store, FakeLogger())

    assert manager.list_providers() == [{
        "id": "fake-playback",
        "name": "Fake Playback",
        "description": "",
        "configured": True,
    }]
    assert manager.list_files("fake-playback", "0")[0]["fid"] == "video-1"
    assert manager.resolve_playback("fake-playback", "video-1")["url"].startswith("https://")

    store.load = lambda: {"cards": {"fake-playback": {"enabled": False, "config": {}}}}
    assert manager.list_providers() == []
    with pytest.raises(LookupError, match="已停用"):
        manager.list_files("fake-playback", "0")


def test_quark_client_persists_refreshed_session_cookies_for_followup_requests(monkeypatch):
    client = QuarkClient("foo=old; __pus=old-pus")
    response = SimpleNamespace(
        status_code=200,
        cookies=SimpleNamespace(get_dict=lambda: {"__pus": "new-pus", "__puus": "new-puus"}),
    )
    monkeypatch.setattr(client.http, "request_json", lambda *args, **kwargs: (response, {"code": 0}))

    client._request_json("GET", "https://drive.quark.cn/example", timeout=2)

    assert "foo=old" in client.get_cookie()
    assert "__pus=new-pus" in client.get_cookie()
    assert "__puus=new-puus" in client.get_cookie()
    assert client.http.headers["Cookie"] == client.get_cookie()


def test_quark_playback_error_includes_safe_upstream_status_and_message(monkeypatch):
    client = QuarkClient("cookie=do-not-log")
    response = SimpleNamespace(status_code=400)
    payload = {"code": 31001, "message": "file not ready"}
    monkeypatch.setattr(client.http, "request_json", lambda *args, **kwargs: (response, payload))

    with pytest.raises(RuntimeError, match=r"HTTP 400.*code=31001.*file not ready") as exc:
        client.get_playback_info("fid-123")

    assert "do-not-log" not in str(exc.value)



def test_quark_playback_falls_back_to_original_download_on_plf_invalid(monkeypatch):
    client = QuarkClient("cookie=do-not-log")
    transcode_response = SimpleNamespace(status_code=400)
    transcode_payload = {"code": 14018, "message": "data invalid: [plf_invalid]"}
    download_response = SimpleNamespace(status_code=200)
    download_payload = {
        "code": 0,
        "data": [{
            "fid": "fid-123",
            "file_name": "sample.mp4",
            "size": 12345,
            "download_url": "https://download.example/sample.mp4?sign=temporary",
        }],
    }
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if "file/v2/play/project" in url:
            return transcode_response, transcode_payload
        return download_response, download_payload

    monkeypatch.setattr(client.http, "request_json", fake_request)

    info = client.get_playback_info("fid-123")

    assert info["url"] == "https://download.example/sample.mp4?sign=temporary"
    assert info["file_name"] == "sample.mp4"
    assert info["mime_type"] == "video/mp4"
    assert info["resolution"] == "原始文件"
    assert len(calls) == 2
    assert calls[1][0] == "POST"
    assert calls[1][1].startswith("https://drive-pc.quark.cn/1/clouddrive/file/download?")
    assert calls[1][2]["json"] == {"fids": ["fid-123"]}
    assert "do-not-log" not in repr(info)


def test_quark_playback_retries_size_limited_download_with_pc_client_headers(monkeypatch):
    client = QuarkClient("cookie=do-not-log")
    transcode_response = SimpleNamespace(status_code=400)
    transcode_payload = {"code": 14018, "message": "data invalid: [plf_invalid]"}
    limited_response = SimpleNamespace(status_code=400)
    limited_payload = {"code": 23018, "message": "download file size limit"}
    token_response = SimpleNamespace(status_code=200)
    token_payload = {"code": 0, "data": {"token": "temporary-token"}}
    pc_response = SimpleNamespace(status_code=200)
    pc_payload = {
        "code": 0,
        "data": [{
            "file_name": "large-video.mp4",
            "size": 987654321,
            "download_url": "https://download.example/large-video.mp4?sign=temporary",
        }],
    }
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if "file/v2/play/project" in url:
            return transcode_response, transcode_payload
        if "ve=2.5.56" in url:
            return limited_response, limited_payload
        if "acquire_dl_token" in url:
            return token_response, token_payload
        return pc_response, pc_payload

    monkeypatch.setattr(client.http, "request_json", fake_request)

    info = client.get_playback_info("fid-123")

    assert info["url"] == "https://download.example/large-video.mp4?sign=temporary"
    assert info["file_name"] == "large-video.mp4"
    assert info["size"] == 987654321
    assert len(calls) == 4
    assert "acquire_dl_token" in calls[2][1]
    assert calls[3][1].endswith("sys=win32&ve=6.9.7.761")
    assert calls[3][2]["headers"]["User-Agent"].endswith(
        "QuarkCloudDrivePC/6.9.7.761 quark-cloud-drive/2.5.40"
    )
    assert calls[3][2]["json"] == {
        "fids": ["fid-123"],
        "speedup_session": "",
        "token": "temporary-token",
    }
    assert "do-not-log" not in repr(info)


def test_quark_playback_does_not_fallback_for_unrelated_api_errors(monkeypatch):
    client = QuarkClient("cookie=do-not-log")
    response = SimpleNamespace(status_code=400)
    payload = {"code": 31001, "message": "require login"}
    calls = []
    monkeypatch.setattr(
        client.http,
        "request_json",
        lambda *args, **kwargs: (calls.append((args, kwargs)) or (response, payload)),
    )

    with pytest.raises(RuntimeError, match=r"HTTP 400.*code=31001.*require login"):
        client.get_playback_info("fid-123")

    assert len(calls) == 1

def test_quark_playback_parses_https_url_and_rejects_invalid_fid(monkeypatch):
    client = QuarkClient("cookie=test")
    response = SimpleNamespace(status_code=200)
    payload = {
        "code": 0,
        "data": {
            "file_name": "sample.mp4",
            "video_list": [
                {
                    "resolution": "normal",
                    "video_info": {
                        "url": "https://media.example/stream.mp4?sign=abc",
                        "format": "fmp4_av",
                        "size": 123,
                    },
                },
                {
                    "resolution": "low",
                    "video_info": {
                        "url": "http://bad.example/video.mp4",
                        "format": "mp4",
                        "size": 123,
                    },
                },
            ],
        },
    }
    monkeypatch.setattr(client.http, "request_json", lambda *args, **kwargs: (response, payload))

    info = client.get_playback_info("fid-123")
    assert info["url"] == "https://media.example/stream.mp4?sign=abc"
    assert info["file_name"] == "sample.mp4"
    assert info["mime_type"] == "video/mp4"
    with pytest.raises(ValueError, match="FID"):
        client.get_playback_info("../not-a-fid")


def test_playback_routes_keep_core_available_without_playback_card(tmp_path, monkeypatch):
    monkeypatch.delenv("MOVIESYNC_AUTO_INSTALL_BUNDLED_CARDS", raising=False)
    app = create_app({"MOVIESYNC_DATA_DIR": str(tmp_path)}, start_scheduler=False)
    client = app.test_client()
    client.post("/api/setup", json={"username": "admin", "password": "password123"})

    assert client.get("/api/playback/providers").get_json()["providers"] == []
    assert client.get("/playback").status_code == 200

    app.extensions["moviesync"]["card_registry"].register(FakePlaybackCard())
    response = client.get("/api/playback/providers")
    assert response.status_code == 200
    assert response.get_json()["providers"][0]["id"] == "fake-playback"
    response = client.get("/api/playback/files?provider_id=fake-playback&parent_fid=0")
    assert response.status_code == 200
    assert response.get_json()["files"][0]["file_name"] == "sample.mp4"
    response = client.get("/api/playback/resolve?provider_id=fake-playback&fid=video-1")
    assert response.status_code == 200
    assert response.get_json()["playback"]["url"] == "https://media.example/video.mp4"

    anonymous = app.test_client()
    assert anonymous.get("/api/playback/providers").status_code == 401



def test_quark_playback_card_loads_as_an_independent_single_file_plugin(tmp_path):
    import logging
    from pathlib import Path

    class ConfigStore:
        def get_card_config(self, card_id):
            return {"cookie": "cookie=placeholder"} if card_id == "quark" else {}

        def load(self):
            return {"cards": {}}

    plugin_dir = tmp_path / "cards"
    registry = CardRegistry()
    manager = CardFilePluginManager(plugin_dir, registry, logging.getLogger("test-playback"))
    manager.load_all({"config_store": ConfigStore()})
    source = Path(__file__).resolve().parents[1] / "card_templates" / "quark_playback.py"
    card = manager.install("quark_playback.py", source.read_bytes())

    assert card.card_id == "quark_playback"
    assert card.card_type == "playback_provider"
    assert registry.get("quark_playback") is card
