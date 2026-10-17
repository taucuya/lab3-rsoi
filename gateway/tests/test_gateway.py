import importlib.util
import time
from pathlib import Path

import pytest

APP_PATH = Path(__file__).resolve().parent.parent / "app.py"

spec = importlib.util.spec_from_file_location("gateway_app", APP_PATH)
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)


class FakeResponse:
    def __init__(self, data=None, status_code=200):
        self._data = data if data is not None else {}
        self.status_code = status_code

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise gateway.requests.HTTPError("error")


def test_health_returns_ok():
    client = gateway.app.test_client()
    assert client.get("/manage/health").status_code == 200


def test_circuit_breaker_opens_and_recovers():
    breaker = gateway.CircuitBreaker(
        "test",
        window_size=4,
        minimum_calls=2,
        failure_rate_threshold=50,
        open_timeout=0.1,
    )
    assert breaker.state() == "closed"

    breaker.record_failure()
    # minimum_calls=2 ещё не набрано — окно не даёт права размыкать цепь
    assert breaker.state() == "closed"

    breaker.record_failure()
    assert breaker.state() == "open"
    assert breaker.allow_request() is False

    time.sleep(0.15)
    assert breaker.state() == "half-open"
    assert breaker.allow_request() is True

    breaker.record_success()
    assert breaker.state() == "closed"
    assert breaker.allow_request() is True


def test_count_based_window_slides():
    window = gateway.CountBasedSlidingWindow(3)
    window.add(True)
    window.add(True)
    window.add(True)
    assert window.calls == 3
    assert window.failures == 3
    assert window.failure_rate == pytest.approx(100.0)

    # успех вытесняет самую старую неудачу из окна
    window.add(False)
    assert window.calls == 3
    assert window.failures == 2
    assert window.failure_rate == pytest.approx(200.0 / 3.0)

    window.add(False)
    assert window.calls == 3
    assert window.failures == 1
    assert window.failure_rate == pytest.approx(100.0 / 3.0)


def test_breaker_stays_closed_below_failure_rate():
    breaker = gateway.CircuitBreaker(
        "rate",
        window_size=4,
        minimum_calls=4,
        failure_rate_threshold=50,
        open_timeout=10,
    )
    breaker.record_failure()
    breaker.record_success()
    breaker.record_success()
    breaker.record_success()  # 1 отказ из 4 = 25% < 50%
    assert breaker.state() == "closed"


def test_half_open_failure_reopens():
    breaker = gateway.CircuitBreaker(
        "reopen",
        window_size=2,
        minimum_calls=2,
        failure_rate_threshold=50,
        open_timeout=0.05,
    )
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state() == "open"

    time.sleep(0.06)
    assert breaker.state() == "half-open"

    breaker.record_failure()
    assert breaker.state() == "open"
    assert breaker.allow_request() is False


def test_metrics_reflect_window():
    breaker = gateway.CircuitBreaker(
        "metrics",
        window_size=4,
        minimum_calls=4,
        failure_rate_threshold=50,
        open_timeout=10,
    )
    breaker.record_failure()
    breaker.record_success()

    metrics = breaker.metrics()
    assert metrics["state"] == "closed"
    assert metrics["calls"] == 2
    assert metrics["failures"] == 1
    assert metrics["failureRate"] == pytest.approx(50.0)


def test_privilege_returns_503_when_bonus_unavailable(monkeypatch):
    monkeypatch.setattr(gateway, "read_call", lambda *a, **k: (None, True))
    client = gateway.app.test_client()
    resp = client.get("/api/v1/privilege", headers={"X-User-Name": "user"})
    assert resp.status_code == 503
    assert resp.get_json()["message"] == "Bonus Service unavailable"


def test_me_returns_fallback_privilege_when_bonus_unavailable(monkeypatch):
    def fake_read_call(key, url, **kwargs):
        if key == "ticket:get_tickets":
            return FakeResponse([]), False
        # bonus / flight -> unavailable
        return None, True

    monkeypatch.setattr(gateway, "read_call", fake_read_call)
    client = gateway.app.test_client()
    resp = client.get("/api/v1/me", headers={"X-User-Name": "user"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["tickets"] == []
    assert body["privilege"] == {}


def test_buy_ticket_rolls_back_when_bonus_unavailable(monkeypatch):
    deleted = []

    def fake_read_call(key, url, **kwargs):
        return FakeResponse({"flightNumber": "AFL031", "price": 1500}), False

    class FakeRequests:
        RequestException = gateway.requests.RequestException
        ConnectionError = gateway.requests.ConnectionError
        HTTPError = gateway.requests.HTTPError

        def post(self, url, **kwargs):
            if "privilege" in url:
                raise gateway.requests.ConnectionError("bonus down")
            return FakeResponse({"ticketUid": "x"}, 201)

        def delete(self, url, **kwargs):
            deleted.append(url)
            return FakeResponse({}, 204)

    monkeypatch.setattr(gateway, "read_call", fake_read_call)
    monkeypatch.setattr(gateway, "requests", FakeRequests())

    client = gateway.app.test_client()
    resp = client.post(
        "/api/v1/tickets",
        headers={"X-User-Name": "user"},
        json={"flightNumber": "AFL031", "price": 1500},
    )
    assert resp.status_code == 503
    assert resp.get_json()["message"] == "Bonus Service unavailable"
    # откат: билет должен быть удалён из Ticket Service
    assert len(deleted) == 1
