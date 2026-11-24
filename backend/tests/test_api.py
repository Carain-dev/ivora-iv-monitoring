"""API smoke test with the simulator running fast (simulated data only)."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient


def test_api_endpoints(monkeypatch):
    monkeypatch.setenv("SIM_INTERVAL_S", "0.01")
    import importlib

    import app.config as config

    config.settings.sim_interval_s = 0.01
    config.settings.data_source = "simulator"
    main = importlib.import_module("app.main")
    with TestClient(main.app) as c:
        time.sleep(1.5)
        s = c.get("/api/status").json()
        assert s["source"] == "simulated" and s["data_label"] in ("SIMULATED", "STALE")
        assert s["latest"] is not None and s["bag_session"]["tare_g"] == 40.0
        assert "blynk_token" not in str(c.get("/api/config").json()).replace("blynk_token_set", "")
        assert isinstance(c.get("/api/measurements?minutes=5").json(), list)
        assert isinstance(c.get("/api/events").json(), list)
        assert isinstance(c.get("/api/alerts?state=all").json(), list)
        r = c.post("/api/bag-sessions", json={"tare_g": 40, "initial_g": 560, "expected_rate_g_min": 4})
        assert r.status_code == 200 and r.json()["initial_source"] == "manual"
        assert c.post("/api/bag-sessions", json={"tare_g": 100, "initial_g": 50}).status_code == 400
        assert c.post("/api/calibration", json={"known_mass_g": 200, "seconds": 5}).status_code == 200
        cal = c.get("/api/calibration?group=simulated").json()
        assert cal["n"] == 1 and "mae_g" in cal
        assert c.get("/api/calibration?group=physical").json()["n"] == 0
        m = c.post("/api/sensor-health/train-simulated?seconds=900").json()
        assert m["source_group"] == "simulated" and m["n_samples"] > 100
        assert c.get("/api/sensor-health/model").json()["trained"]
        a = c.get("/api/analytics?group=physical").json()
        assert a["evaluation"]["n_labels"] == 0
        exp = c.get("/api/export/measurements.csv?hours=1")
        assert exp.status_code == 200 and exp.text.startswith("id,")
        assert c.get("/api/blynk/check").json()["ok"] is False  # no token in tests
        alerts = c.get("/api/alerts?state=all").json()
        if alerts:
            aid = alerts[0]["id"]
            if alerts[0]["state"] != "resolved":
                assert c.post(f"/api/alerts/{aid}/acknowledge", json={"by": "tester"}).json()["state"] == "acknowledged"
            assert c.post(f"/api/alerts/{aid}/resolve").json()["state"] == "resolved"
