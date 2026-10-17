import importlib.util
from pathlib import Path

APP_PATH = Path(__file__).resolve().parent.parent / "app.py"


def _load_app(name):
    spec = importlib.util.spec_from_file_location(name, APP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_health_returns_ok():
    service = _load_app("ticket_service_app")
    client = service.app.test_client()
    resp = client.get("/manage/health")
    assert resp.status_code == 200


def test_delete_endpoint_exists():
    service = _load_app("ticket_service_app")
    rules = {rule.rule: rule.methods for rule in service.app.url_map.iter_rules()}
    assert "/tickets/<ticket_uid>" in rules
    assert "DELETE" in rules["/tickets/<ticket_uid>"]
