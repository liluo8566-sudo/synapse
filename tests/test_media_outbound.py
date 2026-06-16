"""C1 outbound media tests.

Covers: AES-128-ECB encrypt helper, two-step upload protocol (getuploadurl +
CDN direct), payload shapes per type, and failure paths. All HTTP mocked.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call

import httpx
import pytest

from synapse_wx.ilink import _auth
from synapse_wx.ilink import _media as media_mod
from synapse_wx.ilink import client as client_module
from synapse_wx.ilink._media import (
    _CDN_UA,
    encrypt_aes_ecb,
    pkcs7_pad,
    upload_and_encrypt,
)
from synapse_wx.ilink.client import ILinkClient
from synapse_wx.ilink.cursor import Cursor

# ── helpers ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fast_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Silence the inter-attempt backoff so retry tests stay fast."""
    monkeypatch.setattr(media_mod.time, "sleep", lambda *_: None)


def _resp(status: int = 200, body: dict | None = None, headers: dict | None = None) -> MagicMock:
    r = MagicMock(spec=httpx.Response)
    r.status_code = status
    r.text = json.dumps(body) if body is not None else ""
    r.json.return_value = body if body is not None else {}
    r.headers = headers or {}
    if status >= 400:
        r.raise_for_status.side_effect = httpx.HTTPStatusError(
            "boom", request=MagicMock(), response=r
        )
    else:
        r.raise_for_status.return_value = None
    return r


@pytest.fixture
def isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    token_file = tmp_path / "token.json"
    monkeypatch.setattr(_auth, "TOKEN_FILE", token_file)
    monkeypatch.setattr(client_module, "TOKEN_FILE", token_file)
    return tmp_path


@pytest.fixture
def logged_in_client(
    isolated_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> ILinkClient:
    token_file = isolated_paths / "token.json"
    token_file.write_text(
        json.dumps(
            {
                "bot_token": "tok-abc",
                "base_url": "https://ilinkai.weixin.qq.com",
            }
        )
    )
    cursor = Cursor(isolated_paths / "cursor.json")
    c = ILinkClient(cursor=cursor)
    c._client = MagicMock(spec=httpx.Client)
    return c


# ── pkcs7 + AES helpers ────────────────────────────────────────────────────


def test_pkcs7_pad_exact_block_adds_full_block() -> None:
    # 16-byte input gets 16 bytes of pad (PKCS7 always pads).
    out = pkcs7_pad(b"a" * 16, block=16)
    assert len(out) == 32
    assert out[-16:] == bytes([16]) * 16


def test_pkcs7_pad_partial_block() -> None:
    out = pkcs7_pad(b"abc", block=16)
    assert len(out) == 16
    assert out[-13:] == bytes([13]) * 13


def test_encrypt_aes_ecb_roundtrip() -> None:
    key = b"k" * 16
    plain = b"hello world this is a test of more than one block!!"
    ct = encrypt_aes_ecb(plain, key)
    assert len(ct) % 16 == 0
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    d = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    raw = d.update(ct) + d.finalize()
    pad_len = raw[-1]
    assert raw[:-pad_len] == plain


def test_encrypt_aes_ecb_rejects_bad_key() -> None:
    with pytest.raises(ValueError):
        encrypt_aes_ecb(b"data", b"short")


# ── upload_and_encrypt — two-step protocol ─────────────────────────────────


def test_upload_and_encrypt_getuploadurl_request_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Step A request body: correct media_type, rawfilemd5, filesize=padded, aeskey hex."""
    plain = b"\x89PNGfakeimagecontent" * 4
    f = tmp_path / "img.png"
    f.write_bytes(plain)

    pinned_key = b"K" * 16
    pinned_filekey = "abcd1234" * 4  # 32-char hex
    monkeypatch.setattr(media_mod, "_random_key", lambda: pinned_key)
    monkeypatch.setattr(media_mod, "_random_filekey", lambda: pinned_filekey)

    # Two sequential POSTs: getuploadurl then CDN upload.
    ticket_resp = _resp(200, {"ret": 0, "upload_param": "TICKET-XYZ"})
    cdn_resp = _resp(200, {}, headers={"x-encrypted-param": "DL-PARAM"})
    http = MagicMock(spec=httpx.Client)
    http.post.side_effect = [ticket_resp, cdn_resp]

    meta = upload_and_encrypt(
        http,
        base_url="https://ilinkai.weixin.qq.com",
        headers={"Authorization": "Bearer tok"},
        path=f,
        item_type="image",
        to_user_id="wxid_abc",
    )

    # Step A call
    first_call = http.post.call_args_list[0]
    assert first_call.args[0].endswith("/ilink/bot/getuploadurl")
    body = first_call.kwargs["json"]

    assert body["filekey"] == pinned_filekey
    assert body["media_type"] == 1  # image
    assert body["to_user_id"] == "wxid_abc"
    assert body["rawsize"] == len(plain)
    assert body["rawfilemd5"] == hashlib.md5(plain).hexdigest()
    ciphertext = encrypt_aes_ecb(plain, pinned_key)
    assert body["filesize"] == len(ciphertext)  # padded size
    assert body["no_need_thumb"] is True
    assert body["aeskey"] == pinned_key.hex()


def test_upload_and_encrypt_cdn_upload_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Step B: CDN URL has both query params urlencoded; body is ciphertext."""
    plain = b"filedata" * 3
    f = tmp_path / "doc.pdf"
    f.write_bytes(plain)

    pinned_key = b"M" * 16
    pinned_filekey = "ff112233" * 4
    monkeypatch.setattr(media_mod, "_random_key", lambda: pinned_key)
    monkeypatch.setattr(media_mod, "_random_filekey", lambda: pinned_filekey)

    ticket_resp = _resp(200, {"ret": 0, "upload_param": "TICKET-ABC"})
    cdn_resp = _resp(200, {}, headers={"x-encrypted-param": "DLPARAM"})
    http = MagicMock(spec=httpx.Client)
    http.post.side_effect = [ticket_resp, cdn_resp]

    upload_and_encrypt(
        http,
        base_url="https://ilinkai.weixin.qq.com",
        headers={},
        path=f,
        item_type="file",
        to_user_id="wxid_xyz",
    )

    cdn_call = http.post.call_args_list[1]
    cdn_url = cdn_call.args[0]
    assert "encrypted_query_param=TICKET-ABC" in cdn_url
    assert f"filekey={pinned_filekey}" in cdn_url
    assert cdn_call.kwargs.get("headers", {}).get("Content-Type") == "application/octet-stream"
    body = cdn_call.kwargs["content"]
    # Body is ciphertext — decrypts back to plaintext
    from synapse_wx.ilink._media import decrypt_aes_ecb
    assert decrypt_aes_ecb(body, pinned_key) == plain


def test_upload_and_encrypt_returns_download_param(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """x-encrypted-param from CDN response becomes encrypt_query_param in result."""
    f = tmp_path / "img.png"
    f.write_bytes(b"x" * 32)
    monkeypatch.setattr(media_mod, "_random_key", lambda: b"K" * 16)
    monkeypatch.setattr(media_mod, "_random_filekey", lambda: "aa" * 16)

    ticket_resp = _resp(200, {"ret": 0, "upload_param": "T"})
    cdn_resp = _resp(200, {}, headers={"x-encrypted-param": "THE-DOWNLOAD-PARAM"})
    http = MagicMock(spec=httpx.Client)
    http.post.side_effect = [ticket_resp, cdn_resp]

    meta = upload_and_encrypt(
        http, base_url="https://ilinkai.weixin.qq.com", headers={},
        path=f, item_type="image", to_user_id="u",
    )
    assert meta["encrypt_query_param"] == "THE-DOWNLOAD-PARAM"
    assert meta["aes_key_hex"] == b"K".hex() * 16
    # aes_key_b64: base64(hex_string.encode("ascii"))
    expected_b64 = base64.b64encode((b"K".hex() * 16).encode("ascii")).decode("ascii")
    assert meta["aes_key_b64"] == expected_b64


def test_upload_and_encrypt_media_type_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """media_type: image=1, video=2, file=3."""
    monkeypatch.setattr(media_mod, "_random_key", lambda: b"K" * 16)
    monkeypatch.setattr(media_mod, "_random_filekey", lambda: "aa" * 16)

    for item_type, expected_media_type in [("image", 1), ("video", 2), ("file", 3)]:
        f = tmp_path / f"x.{item_type}"
        f.write_bytes(b"data")
        ticket_resp = _resp(200, {"ret": 0, "upload_param": "T"})
        cdn_resp = _resp(200, {}, headers={"x-encrypted-param": "P"})
        http = MagicMock(spec=httpx.Client)
        http.post.side_effect = [ticket_resp, cdn_resp]
        upload_and_encrypt(
            http, base_url="https://x.com", headers={},
            path=f, item_type=item_type, to_user_id="u",
        )
        body = http.post.call_args_list[0].kwargs["json"]
        assert body["media_type"] == expected_media_type, f"{item_type} → {expected_media_type}"


def test_upload_and_encrypt_getuploadurl_fail_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-200 from getuploadurl: logs, returns {}, no CDN call."""
    f = tmp_path / "img.png"
    f.write_bytes(b"x" * 32)
    monkeypatch.setattr(media_mod, "_random_key", lambda: b"K" * 16)
    monkeypatch.setattr(media_mod, "_random_filekey", lambda: "aa" * 16)

    http = MagicMock(spec=httpx.Client)
    http.post.return_value = _resp(500, {"ret": -1})

    meta = upload_and_encrypt(
        http, base_url="https://ilinkai.weixin.qq.com", headers={},
        path=f, item_type="image", to_user_id="u",
    )
    assert meta == {}
    # getuploadurl retried each attempt; CDN never reached.
    assert http.post.call_count == media_mod._CDN_UPLOAD_ATTEMPTS


def test_upload_and_encrypt_cdn_fail_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Persistent non-200 from CDN: retries all attempts, returns {}."""
    f = tmp_path / "img.png"
    f.write_bytes(b"x" * 32)
    monkeypatch.setattr(media_mod, "_random_key", lambda: b"K" * 16)
    monkeypatch.setattr(media_mod, "_random_filekey", lambda: "aa" * 16)

    ticket_resp = _resp(200, {"ret": 0, "upload_param": "T"})
    cdn_resp = _resp(403, None, headers={"x-error-message": "quota exceeded"})
    http = MagicMock(spec=httpx.Client)
    http.post.side_effect = [ticket_resp, cdn_resp] * media_mod._CDN_UPLOAD_ATTEMPTS

    meta = upload_and_encrypt(
        http, base_url="https://ilinkai.weixin.qq.com", headers={},
        path=f, item_type="image", to_user_id="u",
    )
    assert meta == {}
    assert http.post.call_count == 2 * media_mod._CDN_UPLOAD_ATTEMPTS


def test_upload_and_encrypt_retries_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Transient CDN 500 on attempt 1, success on attempt 2 → populated meta."""
    f = tmp_path / "img.png"
    f.write_bytes(b"x" * 32)
    monkeypatch.setattr(media_mod, "_random_key", lambda: b"K" * 16)
    monkeypatch.setattr(media_mod, "_random_filekey", lambda: "aa" * 16)

    ticket = _resp(200, {"ret": 0, "upload_param": "T"})
    cdn_500 = _resp(500, None, headers={"x-error-code": "-5104001"})
    cdn_ok = _resp(200, None, headers={"x-encrypted-param": "DOWNPARAM"})
    http = MagicMock(spec=httpx.Client)
    http.post.side_effect = [ticket, cdn_500, ticket, cdn_ok]

    meta = upload_and_encrypt(
        http, base_url="https://ilinkai.weixin.qq.com", headers={},
        path=f, item_type="image", to_user_id="u",
    )
    assert meta.get("encrypt_query_param") == "DOWNPARAM"
    assert http.post.call_count == 4  # ticket+500, then ticket+200


def test_upload_and_encrypt_missing_file_returns_empty(tmp_path: Path) -> None:
    http = MagicMock(spec=httpx.Client)
    meta = upload_and_encrypt(
        http,
        base_url="https://ilinkai.weixin.qq.com",
        headers={},
        path=tmp_path / "nope.png",
        item_type="image",
        to_user_id="u",
    )
    assert meta == {}
    assert http.post.call_count == 0


# ── aes_key double-encoding ────────────────────────────────────────────────


def test_aes_key_double_encoding() -> None:
    """aes_key_b64 = base64(hex_string.encode()) — not base64 of raw key bytes."""
    key_hex = "4b4b4b4b4b4b4b4b4b4b4b4b4b4b4b4b"  # b"K"*16
    expected = base64.b64encode(key_hex.encode("ascii")).decode("ascii")
    # Verify it decodes back to the hex string, not the raw bytes
    decoded = base64.b64decode(expected).decode("ascii")
    assert decoded == key_hex
    assert bytes.fromhex(decoded) == b"K" * 16


# ── client send_* payload shapes ──────────────────────────────────────────


def _good_meta(padded_size: int = 48, rawsize: int = 32) -> dict:
    """Meta dict matching what upload_and_encrypt returns in the verified protocol."""
    key_hex = "4b" * 16
    return {
        "encrypt_query_param": "DL-QP",
        "aes_key_hex": key_hex,
        "aes_key_b64": base64.b64encode(key_hex.encode("ascii")).decode("ascii"),
        "padded_size": padded_size,
        "rawsize": rawsize,
        "md5": "d" * 32,
    }


def _patch_upload(monkeypatch: pytest.MonkeyPatch, meta: dict[str, Any]) -> MagicMock:
    stub = MagicMock(return_value=meta)
    monkeypatch.setattr(client_module, "upload_and_encrypt", stub)
    return stub


def test_send_image_payload_shape(
    logged_in_client: ILinkClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    f = tmp_path / "a.png"
    f.write_bytes(b"\x89PNGdata")
    meta = _good_meta()
    stub = _patch_upload(monkeypatch, meta)
    logged_in_client._client.post.return_value = _resp(200, {"ret": 0})

    ok = logged_in_client.send_image(str(f), to_user_id="user-9", context_token="ctx-1")
    assert ok is True
    assert stub.call_args.kwargs["item_type"] == "image"

    call_args = logged_in_client._client.post.call_args
    assert call_args.args[0].endswith("/ilink/bot/sendmessage")
    # x-encrypted-param must NOT be in request headers (it's a CDN response header)
    headers = call_args.kwargs.get("headers", {})
    assert "x-encrypted-param" not in headers

    msg = call_args.kwargs["json"]["msg"]
    assert msg["to_user_id"] == "user-9"
    assert msg["context_token"] == "ctx-1"
    item = msg["item_list"][0]
    assert item["type"] == 2
    image_item = item["image_item"]
    media_ref = image_item["media"]
    assert media_ref["encrypt_query_param"] == "DL-QP"
    assert media_ref["encrypt_type"] == 1
    # aes_key in media ref is the double-encoded b64
    assert media_ref["aes_key"] == meta["aes_key_b64"]
    # aeskey at image_item level is the raw hex
    assert image_item["aeskey"] == meta["aes_key_hex"]
    assert image_item["mid_size"] == meta["padded_size"]
    assert image_item["hd_size"] == meta["padded_size"]


def test_send_file_payload_shape(
    logged_in_client: ILinkClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF-fake")
    meta = _good_meta(rawsize=9)
    _patch_upload(monkeypatch, meta)
    logged_in_client._client.post.return_value = _resp(200, {"ret": 0})

    ok = logged_in_client.send_file(str(f), to_user_id="user-9", context_token="ctx-1")
    assert ok is True
    msg = logged_in_client._client.post.call_args.kwargs["json"]["msg"]
    item = msg["item_list"][0]
    assert item["type"] == 4
    file_item = item["file_item"]
    media_ref = file_item["media"]
    assert media_ref["encrypt_query_param"] == "DL-QP"
    assert media_ref["encrypt_type"] == 1
    assert file_item["file_name"] == "report.pdf"
    # len is a string of the raw plaintext size
    assert file_item["len"] == str(meta["rawsize"])
    # no full_url/cdn_url/file_size fields
    assert "full_url" not in media_ref
    assert "cdn_url" not in file_item
    assert "file_size" not in file_item


def test_send_gif_uses_image_type_2(
    logged_in_client: ILinkClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GIFs route through image path (type 2). No emoticon_item."""
    f = tmp_path / "cat.gif"
    f.write_bytes(b"GIF89a")
    meta = _good_meta()
    _patch_upload(monkeypatch, meta)
    logged_in_client._client.post.return_value = _resp(200, {"ret": 0})

    ok = logged_in_client.send_gif(str(f), to_user_id="u", context_token="c")
    assert ok is True
    msg = logged_in_client._client.post.call_args.kwargs["json"]["msg"]
    item = msg["item_list"][0]
    # Must be type 2 (image), never 8 (emoticon)
    assert item["type"] == 2
    assert "image_item" in item
    assert "emoticon_item" not in item
    assert item["image_item"]["media"]["encrypt_query_param"] == "DL-QP"


def test_send_video_payload_shape(
    logged_in_client: ILinkClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"\x00\x00\x00\x18ftypmp42fake")
    meta = _good_meta()
    _patch_upload(monkeypatch, meta)
    logged_in_client._client.post.return_value = _resp(200, {"ret": 0})

    ok = logged_in_client.send_video(str(f), to_user_id="u", context_token="c")
    assert ok is True
    msg = logged_in_client._client.post.call_args.kwargs["json"]["msg"]
    item = msg["item_list"][0]
    assert item["type"] == 5
    video_item = item["video_item"]
    media_ref = video_item["media"]
    assert media_ref["encrypt_query_param"] == "DL-QP"
    assert media_ref["encrypt_type"] == 1
    assert video_item["video_size"] == meta["padded_size"]
    # no full_url in media ref
    assert "full_url" not in media_ref


def test_send_image_returns_false_when_upload_fails(
    logged_in_client: ILinkClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    f = tmp_path / "a.png"
    f.write_bytes(b"data")
    _patch_upload(monkeypatch, {})
    ok = logged_in_client.send_image(str(f), to_user_id="u", context_token="c")
    assert ok is False
    assert logged_in_client._client.post.call_count == 0


def test_send_image_returns_false_when_missing_recipient(
    logged_in_client: ILinkClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    f = tmp_path / "a.png"
    f.write_bytes(b"data")
    _patch_upload(monkeypatch, _good_meta())
    ok = logged_in_client.send_image(str(f))
    assert ok is False


def test_sendmessage_has_no_x_encrypted_param_header(
    logged_in_client: ILinkClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """x-encrypted-param is a CDN upload response header — never forwarded on sendmessage."""
    f = tmp_path / "a.png"
    f.write_bytes(b"data")
    _patch_upload(monkeypatch, _good_meta())
    logged_in_client._client.post.return_value = _resp(200, {"ret": 0})
    logged_in_client.send_image(str(f), to_user_id="u", context_token="c")
    call_args = logged_in_client._client.post.call_args
    headers = call_args.kwargs.get("headers", {})
    assert "x-encrypted-param" not in headers


# ── CDN User-Agent fix ─────────────────────────────────────────────────────


def test_cdn_upload_includes_micromessenger_ua(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CDN upload POST (step B) must include the MicroMessenger User-Agent."""
    plain = b"imagedata" * 10
    f = tmp_path / "img.png"
    f.write_bytes(plain)

    monkeypatch.setattr(media_mod, "_random_key", lambda: b"K" * 16)
    monkeypatch.setattr(media_mod, "_random_filekey", lambda: "aa" * 16)

    ticket_resp = _resp(200, {"ret": 0, "upload_param": "TICKET"})
    cdn_resp = _resp(200, {}, headers={"x-encrypted-param": "DLPARAM"})
    http = MagicMock(spec=httpx.Client)
    http.post.side_effect = [ticket_resp, cdn_resp]

    upload_and_encrypt(
        http,
        base_url="https://ilinkai.weixin.qq.com",
        headers={},
        path=f,
        item_type="image",
        to_user_id="u",
    )

    cdn_call = http.post.call_args_list[1]
    cdn_headers = cdn_call.kwargs.get("headers", {})
    assert cdn_headers.get("User-Agent") == _CDN_UA
    assert "MicroMessenger" in cdn_headers["User-Agent"]


def test_cdn_ua_not_on_getuploadurl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """User-Agent is only set on the CDN upload, not on the iLink getuploadurl call."""
    f = tmp_path / "img.png"
    f.write_bytes(b"data" * 10)

    monkeypatch.setattr(media_mod, "_random_key", lambda: b"K" * 16)
    monkeypatch.setattr(media_mod, "_random_filekey", lambda: "aa" * 16)

    ticket_resp = _resp(200, {"ret": 0, "upload_param": "T"})
    cdn_resp = _resp(200, {}, headers={"x-encrypted-param": "P"})
    http = MagicMock(spec=httpx.Client)
    http.post.side_effect = [ticket_resp, cdn_resp]

    upload_and_encrypt(
        http,
        base_url="https://ilinkai.weixin.qq.com",
        headers={"Authorization": "Bearer tok"},
        path=f,
        item_type="image",
        to_user_id="u",
    )

    # Step A (getuploadurl) must NOT carry the CDN UA
    first_call_headers = http.post.call_args_list[0].kwargs.get("headers", {})
    assert "User-Agent" not in first_call_headers or (
        first_call_headers.get("User-Agent") != _CDN_UA
    )


# ── oversize guard ─────────────────────────────────────────────────────────


def test_oversize_ciphertext_returns_empty_no_cdn_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ciphertext > 550KB: upload_and_encrypt returns {} and never calls CDN."""
    # Write plaintext just over 550KB so ciphertext (padded) also exceeds limit.
    # AES-ECB pads to nearest 16 bytes — 551_000 bytes plaintext -> >= 551_008 ciphertext.
    f = tmp_path / "big.pdf"
    f.write_bytes(b"x" * 551_000)

    monkeypatch.setattr(media_mod, "_random_key", lambda: b"K" * 16)
    monkeypatch.setattr(media_mod, "_random_filekey", lambda: "aa" * 16)

    ticket_resp = _resp(200, {"ret": 0, "upload_param": "T"})
    http = MagicMock(spec=httpx.Client)
    http.post.return_value = ticket_resp

    meta = upload_and_encrypt(
        http,
        base_url="https://ilinkai.weixin.qq.com",
        headers={},
        path=f,
        item_type="file",
        to_user_id="u",
    )

    assert meta == {}
    # Neither getuploadurl nor CDN should be called
    assert http.post.call_count == 0


def test_just_under_limit_proceeds_normally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ciphertext just under 550KB proceeds to upload normally."""
    # 549_984 bytes plaintext → padded to 550_000 bytes ciphertext (16-byte aligned).
    f = tmp_path / "ok.bin"
    f.write_bytes(b"x" * 549_984)

    monkeypatch.setattr(media_mod, "_random_key", lambda: b"K" * 16)
    monkeypatch.setattr(media_mod, "_random_filekey", lambda: "aa" * 16)

    ticket_resp = _resp(200, {"ret": 0, "upload_param": "T"})
    cdn_resp = _resp(200, {}, headers={"x-encrypted-param": "P"})
    http = MagicMock(spec=httpx.Client)
    http.post.side_effect = [ticket_resp, cdn_resp]

    meta = upload_and_encrypt(
        http,
        base_url="https://ilinkai.weixin.qq.com",
        headers={},
        path=f,
        item_type="file",
        to_user_id="u",
    )

    assert meta != {}
    assert http.post.call_count == 2
