"""Android relay client: HTTP contract, message shaping, outbox resend."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from synapse_wx.android.client import AndroidClient

TOKEN = "s3cret"


@pytest.fixture
def client():
    c = AndroidClient(listen="127.0.0.1", port=0, token=TOKEN, resend_after_sec=5.0)
    yield c
    c.close()


def _url(client: AndroidClient, path: str) -> str:
    return f"http://127.0.0.1:{client.server_port}{path}"


def _request(client, path, method="GET", body=None, token=TOKEN):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(_url(client, path), data=data, method=method)
    if token is not None:
        req.add_header("X-Token", token)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_bad_token_returns_401(client):
    status, _ = _request(client, "/ping", token="wrong")
    assert status == 401


def test_missing_token_returns_401(client):
    status, _ = _request(client, "/ping", token=None)
    assert status == 401


def test_inbox_to_poll_messages_shape(client):
    status, body = _request(
        client,
        "/inbox",
        method="POST",
        body={"title": "小雨", "text": "[3条]小雨: 你好", "when": 1, "pkg": "com.tencent.mm"},
    )
    assert status == 200
    assert body == {"ok": True}

    msgs = client.poll_messages()
    assert len(msgs) == 1
    msg = msgs[0]
    assert msg["message_type"] == 1
    assert msg["from_user_id"] == "小雨"
    assert msg["context_token"] == ""
    assert AndroidClient.extract_text(msg) == "小雨: 你好"

    # inbox drained — a second poll is empty.
    assert client.poll_messages() == []


def test_inbox_without_multi_prefix_passes_through(client):
    _request(client, "/inbox", method="POST", body={"title": "糖霜", "text": "在吗"})
    msgs = client.poll_messages()
    assert AndroidClient.extract_text(msgs[0]) == "在吗"


def test_outbox_ack_cycle(client):
    assert client.send_text("糖霜", "", "在") is True

    status, body = _request(client, "/outbox")
    assert status == 200
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["chat"] == "糖霜"
    assert item["text"] == "在"

    # a second immediate poll must NOT redeliver — not due for resend yet.
    status, body = _request(client, "/outbox")
    assert body["items"] == []

    status, body = _request(client, "/ack", method="POST", body={"id": item["id"], "ok": True, "err": ""})
    assert status == 200
    assert body == {"ok": True}

    # acked items are gone even past the resend window.
    client._now = lambda: 9999.0
    status, body = _request(client, "/outbox")
    assert body["items"] == []


def test_resend_after_timeout_then_drop(client):
    fake_time = [0.0]
    client._now = lambda: fake_time[0]

    client.send_text("糖霜", "", "在")
    status, body = _request(client, "/outbox")
    first_id = body["items"][0]["id"]

    # not yet due for resend
    fake_time[0] = 2.0
    status, body = _request(client, "/outbox")
    assert body["items"] == []

    # resend window elapsed once — redelivered exactly once
    fake_time[0] = 6.0
    status, body = _request(client, "/outbox")
    assert len(body["items"]) == 1
    assert body["items"][0]["id"] == first_id

    # second window elapses unacked — dropped, no third delivery
    fake_time[0] = 12.0
    status, body = _request(client, "/outbox")
    assert body["items"] == []


def test_ping_marks_phone_online(client):
    assert client.phone_online is False
    status, body = _request(client, "/ping")
    assert status == 200
    assert body["ok"] is True
    assert client.phone_online is True


def test_unsupported_media_senders_return_none(client):
    assert client.send_image("x.png") is None
    assert client.send_file("x.pdf") is None
    assert client.send_gif("x.gif") is None
    assert client.send_video("x.mp4") is None
    assert client.extract_media({"item_list": []}) == []


def test_is_logged_in_and_reconnect_close_noop(client):
    assert client.is_logged_in is True
    client.reconnect()  # must not raise


def test_allow_chats_drops_unlisted_titles():
    c = AndroidClient(
        listen="127.0.0.1", port=0, token=TOKEN, allow_chats=["糖霜"]
    )
    try:
        status, body = _request(
            c, "/inbox", method="POST", body={"title": "微信团队", "text": "安全提醒"}
        )
        assert status == 200
        assert body == {"ok": True}
        assert c.poll_messages() == []  # still 200'd to the phone, just dropped

        status, body = _request(
            c, "/inbox", method="POST", body={"title": "糖霜", "text": "在吗"}
        )
        assert status == 200
        msgs = c.poll_messages()
        assert len(msgs) == 1
        assert msgs[0]["from_user_id"] == "糖霜"
    finally:
        c.close()
