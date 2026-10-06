from app import create_app


def test_status_contract(tmp_path):
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "jukebox.db")})
    response = app.test_client().get("/api/v1/status")
    body = response.get_json()
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert body["data"]["service"]["status"] == "ok"
    assert body["data"]["playback"]["state"] in {"playing", "paused", "stopped", "unknown"}
