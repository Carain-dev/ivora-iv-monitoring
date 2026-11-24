"""Blynk polling path against a local fake server (no internet, no real token)."""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

from sqlalchemy import select

from app.config import Settings
from app.db import RawReading, SessionLocal, init_db
from app.pipeline import Pipeline, classify, parse_number
from app.sources import BlynkSource, extract_pin_value

FAKE_TOKEN = "fake-token-for-tests-WXYZ"


class Fake(BaseHTTPRequestHandler):
    online = "true"
    value = 81
    posts = 0

    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        parts = u.query.split("&")
        if parts[0] != "token=" + FAKE_TOKEN:
            body, code = {"error": {"message": "Invalid token."}}, 400
        elif u.path.endswith("isHardwareConnected"):
            body, code = Fake.online, 200
        elif u.path.endswith("/get"):
            body, code = Fake.value, 200
        else:
            body, code = "nope", 404
        data = body if isinstance(body, str) else json.dumps(body)
        self.send_response(code)
        self.end_headers()
        self.wfile.write(data.encode())

    def do_POST(self):  # the client must never write to the device
        Fake.posts += 1
        self.send_response(405)
        self.end_headers()


def test_parse_and_extract():
    assert parse_number(81) == 81 and parse_number("81.5") == 81.5 and parse_number(["7"]) == 7
    assert parse_number("Load normal") is None and parse_number(True) is None and parse_number("nan") is None
    assert extract_pin_value({"v1": 5}, "V1") == 5 and extract_pin_value(5, "V1") == 5


def test_quality_order():
    cfg = Settings()
    kw = dict(weight_g=100.0, value_raw="100", prev_value_raw=None, cfg=cfg)
    assert classify(api_error="x", device_online=False, **kw) == "api_error"
    assert classify(api_error=None, device_online=False, **kw) == "device_offline"
    assert classify(api_error=None, device_online=True, **{**kw, "weight_g": 5000.0}) == "invalid"
    assert classify(api_error=None, device_online=True, **{**kw, "prev_value_raw": "100"}) == "repeated"
    assert classify(api_error=None, device_online=True, **kw) == "ok"


def test_blynk_source_polls_and_never_stores_token(monkeypatch):
    init_db()
    srv = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("BLYNK_TOKEN", FAKE_TOKEN)
    cfg = Settings()
    cfg.blynk_base_url = f"http://127.0.0.1:{srv.server_port}"
    cfg.poll_interval_s = 0.05
    cfg.online_check_interval_s = 0.2
    p = Pipeline(cfg, "bed-blynk-test", "blynk_poll", None)
    src = BlynkSource(cfg, p)
    src.start()
    time.sleep(0.6)
    Fake.online = "false"
    time.sleep(0.6)
    src.stop()
    src.join(2)
    srv.shutdown()
    with SessionLocal() as db:
        rows = db.scalars(select(RawReading).where(RawReading.device_id == "bed-blynk-test")).all()
    qualities = {r.quality for r in rows}
    assert rows and {"ok", "repeated"} & qualities and "device_offline" in qualities
    assert all(r.source == "blynk_poll" and r.weight_g == 81 for r in rows)
    assert all(FAKE_TOKEN not in (r.error or "") + (r.value_raw or "") for r in rows)
    assert Fake.posts == 0
    assert src.status.as_dict()["polls"] >= 5


def test_missing_token_does_not_poll(monkeypatch):
    monkeypatch.delenv("BLYNK_TOKEN", raising=False)
    cfg = Settings()
    src = BlynkSource(cfg, Pipeline(cfg, "bed-none", "blynk_poll", None))
    src.start()
    src.join(2)
    assert src.status.configured is False and "BLYNK_TOKEN" in src.status.message
