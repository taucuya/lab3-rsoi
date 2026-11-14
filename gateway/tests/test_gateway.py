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
        minimum_failures=2,
        open_timeout=0.1,
    )
    assert breaker.state() == "closed"

    breaker.record_failure()
    # minimum_failures=2 ещё не набрано — окно не даёт права размыкать цепь
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


def test_failure_window_is_circular():
    window = gateway.CountBasedSlidingWindow(3, failure_window=60.0)
    window.add_failure(ts=100.0)
    window.add_failure(ts=200.0)
    window.add_failure(ts=300.0)
    assert window.failures == 3

    # четвёртый отказ вытесняет самый старый таймстемп (100.0)
    window.add_failure(ts=400.0)
    assert window.failures == 3
    # остались 200, 300, 400; при now=260 (cutoff=200) свежие все три,
    # а вытесненный 100.0 в окно уже не попадает
    assert window.recent_failures(now=260.0) == 3


def test_recent_failures_counts_only_last_minute():
    window = gateway.CountBasedSlidingWindow(4, failure_window=60.0)
    window.add_failure(ts=0.0)
    window.add_failure(ts=1.0)
    window.add_failure(ts=1000.0)
    window.add_failure(ts=1001.0)

    # при now=1000 в пределах минуты (cutoff=940) только 1000 и 1001
    assert window.failures == 4
    assert window.recent_failures(now=1000.0) == 2
    # ровно половина свежих -> True
    assert window.half_failures_recent(now=1000.0) is True


def test_half_failures_recent_false_when_mostly_stale():
    window = gateway.CountBasedSlidingWindow(4, failure_window=60.0)
    window.add_failure(ts=0.0)
    window.add_failure(ts=1.0)
    window.add_failure(ts=2.0)
    window.add_failure(ts=1000.0)

    # свежий только один из четырёх
    assert window.recent_failures(now=1000.0) == 1
    assert window.half_failures_recent(now=1000.0) is False


def test_breaker_stays_closed_below_minimum_failures():
    breaker = gateway.CircuitBreaker(
        "min",
        window_size=4,
        minimum_failures=4,
        open_timeout=10,
    )
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_failure()  # 3 < 4
    assert breaker.state() == "closed"


def test_breaker_stays_closed_when_failures_stale(monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr(gateway.time, "time", lambda: clock["now"])

    breaker = gateway.CircuitBreaker(
        "stale",
        window_size=4,
        minimum_failures=3,
        failure_window=60.0,
        open_timeout=10.0,
    )
    breaker.record_failure()       # ts=0
    clock["now"] = 61.0
    breaker.record_failure()       # ts=61
    clock["now"] = 122.0
    breaker.record_failure()       # ts=122 (свежий только он)

    # 3 отказа есть, но свежий лишь 1 из 3 -> не размыкаем
    assert breaker.window.failures == 3
    assert breaker.window.half_failures_recent() is False
    assert breaker.state() == "closed"


def test_breaker_opens_when_half_failures_recent(monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr(gateway.time, "time", lambda: clock["now"])

    breaker = gateway.CircuitBreaker(
        "recent",
        window_size=4,
        minimum_failures=3,
        failure_window=60.0,
        open_timeout=10.0,
    )
    breaker.record_failure()       # ts=0
    clock["now"] = 10.0
    breaker.record_failure()       # ts=10
    clock["now"] = 20.0
    breaker.record_failure()       # ts=20

    assert breaker.state() == "open"


def test_half_open_failure_reopens():
    breaker = gateway.CircuitBreaker(
        "reopen",
        window_size=2,
        minimum_failures=2,
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
        minimum_failures=4,
        open_timeout=10,
    )
    breaker.record_failure()
    breaker.record_failure()

    metrics = breaker.metrics()
    assert metrics["state"] == "closed"
    assert metrics["failures"] == 2
    assert metrics["recentFailures"] == 2


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
