from collections import deque

from flask import Flask, request, jsonify
import requests
import uuid
import os
import time
import queue
import logging
import threading

app = Flask(__name__)

FLIGHT_SERVICE = os.getenv("FLIGHT_SERVICE_URL", "http://flight_service:8060")
TICKET_SERVICE = os.getenv("TICKET_SERVICE_URL", "http://ticket_service:8070")
BONUS_SERVICE = os.getenv("BONUS_SERVICE_URL", "http://bonus_service:8050")

REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "5"))

CB_WINDOW_SIZE = int(os.getenv("CB_WINDOW_SIZE", "10"))
CB_MINIMUM_CALLS = int(os.getenv("CB_MINIMUM_CALLS", "5"))
CB_FAILURE_RATE_THRESHOLD = float(os.getenv("CB_FAILURE_RATE_THRESHOLD", "50"))
CB_OPEN_TIMEOUT = float(os.getenv("CB_OPEN_TIMEOUT", "1"))
QUEUE_RETRY_INTERVAL = float(os.getenv("QUEUE_RETRY_INTERVAL", "0.5"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


class CountBasedSlidingWindow:
    def __init__(self, size):
        if size <= 0:
            raise ValueError("window size must be positive")
        self.size = size
        self._results = deque(maxlen=size)

    def add(self, failed):
        self._results.append(bool(failed))

    def clear(self):
        self._results.clear()

    @property
    def calls(self):
        return len(self._results)

    @property
    def failures(self):
        return sum(self._results)

    @property
    def failure_rate(self):
        if not self._results:
            return 0.0
        return 100.0 * self.failures / len(self._results)


class CircuitBreaker:
    def __init__(
        self,
        name,
        window_size=10,
        minimum_calls=None,
        failure_rate_threshold=50.0,
        open_timeout=2.0,
    ):
        self.name = name
        self.window_size = window_size
        self.minimum_calls = min(
            minimum_calls if minimum_calls is not None else window_size,
            window_size,
        )
        self.failure_rate_threshold = failure_rate_threshold
        self.open_timeout = open_timeout
        self.opened_at = None
        self.window = CountBasedSlidingWindow(window_size)
        self.lock = threading.Lock()

    def _state(self):
        if self.opened_at is None:
            return "closed"
        if time.time() - self.opened_at >= self.open_timeout:
            return "half-open"
        return "open"

    def state(self):
        with self.lock:
            return self._state()

    def allow_request(self):
        with self.lock:
            return self._state() != "open"

    def record_success(self):
        with self.lock:
            state = self._state()
            if state == "closed":
                self.window.add(failed=False)
            elif state == "half-open":
                self.opened_at = None
                self.window.clear()

    def record_failure(self):
        with self.lock:
            state = self._state()
            if state == "open":
                return
            if state == "half-open":
                self.opened_at = time.time()
                return
            self.window.add(failed=True)
            if (
                self.window.calls >= self.minimum_calls
                and self.window.failure_rate >= self.failure_rate_threshold
            ):
                self.opened_at = time.time()

    def metrics(self):
        with self.lock:
            return {
                "state": self._state(),
                "calls": self.window.calls,
                "failures": self.window.failures,
                "failureRate": round(self.window.failure_rate, 2),
            }


_breakers = {}
_breakers_lock = threading.Lock()


def get_breaker(key):
    with _breakers_lock:
        breaker = _breakers.get(key)
        if breaker is None:
            breaker = CircuitBreaker(
                key,
                window_size=CB_WINDOW_SIZE,
                minimum_calls=CB_MINIMUM_CALLS,
                failure_rate_threshold=CB_FAILURE_RATE_THRESHOLD,
                open_timeout=CB_OPEN_TIMEOUT,
            )
            _breakers[key] = breaker
        return breaker


def upstream_json(resp):
    try:
        return resp.json()
    except ValueError:
        return None


def read_call(key, url, **kwargs):
    breaker = get_breaker(key)

    if not breaker.allow_request():
        logging.warning(
            "Circuit breaker '%s' is open %s, short-circuit request", key, breaker.metrics()
        )
        return None, True

    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT, **kwargs)
    except requests.RequestException as exc:
        logging.warning("Read call '%s' failed: %s", key, exc)
        breaker.record_failure()
        return None, True

    if 500 <= resp.status_code < 600:
        breaker.record_failure()
        return None, True

    breaker.record_success()
    return resp, False

refund_queue = queue.Queue()


def refund_bonus_for_ticket(username, ticket_uid):
    priv_resp = requests.get(
        f"{BONUS_SERVICE}/privilege",
        params={"username": username},
        timeout=REQUEST_TIMEOUT,
    )
    priv_resp.raise_for_status()
    priv_data = priv_resp.json()

    fill_entry = None
    debit_entry = None
    for h in priv_data.get("history", []):
        if h["ticketUid"] != ticket_uid:
            continue
        if h["operationType"] == "FILL_IN_BALANCE":
            fill_entry = h
        elif h["operationType"] == "DEBIT_THE_ACCOUNT":
            debit_entry = h

    if debit_entry:
        resp = requests.post(
            f"{BONUS_SERVICE}/privilege/credit",
            json={
                "username": username,
                "ticketUid": ticket_uid,
                "amount": abs(debit_entry["balanceDiff"]),
                "operationType": "FILL_IN_BALANCE",
            },
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
    elif fill_entry:
        resp = requests.post(
            f"{BONUS_SERVICE}/privilege/debit",
            json={
                "username": username,
                "ticketUid": ticket_uid,
                "amount": abs(fill_entry["balanceDiff"]),
            },
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()


def refund_worker():
    while True:
        username, ticket_uid = refund_queue.get()
        while True:
            try:
                refund_bonus_for_ticket(username, ticket_uid)
                logging.info("Deferred refund applied for ticket %s", ticket_uid)
                break
            except Exception as exc:
                logging.warning(
                    "Deferred refund for ticket %s failed (%s), retry in %ss",
                    ticket_uid,
                    exc,
                    QUEUE_RETRY_INTERVAL,
                )
                time.sleep(QUEUE_RETRY_INTERVAL)
        refund_queue.task_done()


@app.route("/manage/health")
def health():
    return "OK", 200


@app.route("/api/v1/flights")
def get_flights():
    page = request.args.get("page", 1, type=int)
    size = request.args.get("size", 10, type=int)
    resp, unavailable = read_call(
        "flight:get_flights",
        f"{FLIGHT_SERVICE}/flights",
        params={"page": page, "size": size},
    )
    if unavailable:
        return jsonify({"message": "Flight Service unavailable"}), 503
    return jsonify(upstream_json(resp)), resp.status_code


def _flight_info(flight_number):
    resp, unavailable = read_call("flight:get_flight", f"{FLIGHT_SERVICE}/flights/{flight_number}")
    if unavailable or resp.status_code != 200:
        return {}
    return upstream_json(resp) or {}


def _ticket_view(t):
    flight_info = _flight_info(t["flightNumber"])
    return {
        "ticketUid": t["ticketUid"],
        "flightNumber": t["flightNumber"],
        "fromAirport": flight_info.get("fromAirport", ""),
        "toAirport": flight_info.get("toAirport", ""),
        "date": flight_info.get("date", ""),
        "price": t["price"],
        "status": t["status"],
    }


@app.route("/api/v1/tickets", methods=["GET"])
def get_tickets():
    username = request.headers.get("X-User-Name")
    if not username:
        return jsonify({"message": "X-User-Name header is required"}), 400

    resp, unavailable = read_call(
        "ticket:get_tickets", f"{TICKET_SERVICE}/tickets", params={"username": username}
    )
    if unavailable:
        return jsonify({"message": "Ticket Service unavailable"}), 503

    tickets = upstream_json(resp) or []
    return jsonify([_ticket_view(t) for t in tickets])


@app.route("/api/v1/tickets/<ticket_uid>", methods=["GET"])
def get_ticket(ticket_uid):
    username = request.headers.get("X-User-Name")
    if not username:
        return jsonify({"message": "X-User-Name header is required"}), 400

    resp, unavailable = read_call(
        "ticket:get_ticket",
        f"{TICKET_SERVICE}/tickets/{ticket_uid}",
        params={"username": username},
    )
    if unavailable:
        return jsonify({"message": "Ticket Service unavailable"}), 503
    if resp.status_code == 404:
        return jsonify({"message": "Ticket not found"}), 404

    return jsonify(_ticket_view(upstream_json(resp)))


@app.route("/api/v1/tickets", methods=["POST"])
def buy_ticket():
    username = request.headers.get("X-User-Name")
    if not username:
        return jsonify({"message": "X-User-Name header is required"}), 400

    data = request.json or {}
    flight_number = data.get("flightNumber")
    price = data.get("price")
    paid_from_balance = data.get("paidFromBalance", False)

    flight_resp, unavailable = read_call("flight:get_flight", f"{FLIGHT_SERVICE}/flights/{flight_number}")
    if unavailable:
        return jsonify({"message": "Flight Service unavailable"}), 503
    if flight_resp.status_code == 404:
        return (
            jsonify(
                {
                    "message": "Flight not found",
                    "errors": [{"field": "flightNumber", "error": "Flight not found"}],
                }
            ),
            400,
        )
    flight_info = upstream_json(flight_resp) or {}

    ticket_uid = str(uuid.uuid4())

    try:
        create_resp = requests.post(
            f"{TICKET_SERVICE}/tickets",
            json={
                "username": username,
                "ticketUid": ticket_uid,
                "flightNumber": flight_number,
                "price": price,
            },
            timeout=REQUEST_TIMEOUT,
        )
        create_resp.raise_for_status()
    except requests.RequestException as exc:
        logging.warning("Ticket Service unavailable on purchase: %s", exc)
        return jsonify({"message": "Ticket Service unavailable"}), 503

    paid_by_bonuses = 0
    paid_by_money = price

    try:
        if paid_from_balance:
            debit_resp = requests.post(
                f"{BONUS_SERVICE}/privilege/debit",
                json={"username": username, "ticketUid": ticket_uid, "amount": price},
                timeout=REQUEST_TIMEOUT,
            )
            debit_resp.raise_for_status()
            debit_data = debit_resp.json()
            paid_by_bonuses = debit_data["paidByBonuses"]
            paid_by_money = price - paid_by_bonuses
        else:
            bonus_amount = price // 10
            credit_resp = requests.post(
                f"{BONUS_SERVICE}/privilege/credit",
                json={
                    "username": username,
                    "ticketUid": ticket_uid,
                    "amount": bonus_amount,
                    "operationType": "FILL_IN_BALANCE",
                },
                timeout=REQUEST_TIMEOUT,
            )
            credit_resp.raise_for_status()
    except (requests.RequestException, KeyError, ValueError) as exc:
        logging.warning("Bonus Service unavailable on purchase, rollback ticket: %s", exc)
        try:
            requests.delete(f"{TICKET_SERVICE}/tickets/{ticket_uid}", timeout=REQUEST_TIMEOUT)
        except requests.RequestException:
            logging.error("Failed to rollback ticket %s", ticket_uid)
        return jsonify({"message": "Bonus Service unavailable"}), 503

    priv_resp, unavailable = read_call(
        "bonus:get_privilege", f"{BONUS_SERVICE}/privilege", params={"username": username}
    )
    if unavailable:
        return jsonify({"message": "Bonus Service unavailable"}), 503
    priv_data = upstream_json(priv_resp) or {}

    return jsonify(
        {
            "ticketUid": ticket_uid,
            "flightNumber": flight_number,
            "fromAirport": flight_info.get("fromAirport", ""),
            "toAirport": flight_info.get("toAirport", ""),
            "date": flight_info.get("date", ""),
            "price": price,
            "paidByMoney": paid_by_money,
            "paidByBonuses": paid_by_bonuses,
            "status": "PAID",
            "privilege": {"balance": priv_data.get("balance"), "status": priv_data.get("status")},
        }
    )


@app.route("/api/v1/tickets/<ticket_uid>", methods=["DELETE"])
def refund_ticket(ticket_uid):
    username = request.headers.get("X-User-Name")
    if not username:
        return jsonify({"message": "X-User-Name header is required"}), 400

    ticket_resp, unavailable = read_call(
        "ticket:get_ticket",
        f"{TICKET_SERVICE}/tickets/{ticket_uid}",
        params={"username": username},
    )
    if unavailable:
        return jsonify({"message": "Ticket Service unavailable"}), 503
    if ticket_resp.status_code == 404:
        return jsonify({"message": "Ticket not found"}), 404

    ticket = upstream_json(ticket_resp) or {}
    if ticket.get("status") == "CANCELED":
        return "", 204

    try:
        patch_resp = requests.patch(
            f"{TICKET_SERVICE}/tickets/{ticket_uid}",
            json={"status": "CANCELED"},
            timeout=REQUEST_TIMEOUT,
        )
        patch_resp.raise_for_status()
    except requests.RequestException as exc:
        logging.warning("Ticket Service unavailable on refund: %s", exc)
        return jsonify({"message": "Ticket Service unavailable"}), 503

    try:
        refund_bonus_for_ticket(username, ticket_uid)
    except Exception as exc:
        logging.warning("Bonus Service unavailable on refund, enqueue ticket %s: %s", ticket_uid, exc)
        refund_queue.put((username, ticket_uid))

    return "", 204


@app.route("/api/v1/me")
def get_me():
    username = request.headers.get("X-User-Name")
    if not username:
        return jsonify({"message": "X-User-Name header is required"}), 400

    tickets_resp, unavailable = read_call(
        "ticket:get_tickets", f"{TICKET_SERVICE}/tickets", params={"username": username}
    )
    if unavailable:
        return jsonify({"message": "Ticket Service unavailable"}), 503

    tickets_raw = upstream_json(tickets_resp) or []
    tickets = [_ticket_view(t) for t in tickets_raw]

    priv_resp, unavailable = read_call(
        "bonus:get_privilege", f"{BONUS_SERVICE}/privilege", params={"username": username}
    )
    if unavailable:
        privilege = {}
    else:
        priv_data = upstream_json(priv_resp) or {}
        privilege = {"balance": priv_data.get("balance"), "status": priv_data.get("status")}

    return jsonify({"tickets": tickets, "privilege": privilege})


@app.route("/api/v1/privilege")
def get_privilege():
    username = request.headers.get("X-User-Name")
    if not username:
        return jsonify({"message": "X-User-Name header is required"}), 400

    resp, unavailable = read_call(
        "bonus:get_privilege", f"{BONUS_SERVICE}/privilege", params={"username": username}
    )
    if unavailable:
        return jsonify({"message": "Bonus Service unavailable"}), 503
    return jsonify(upstream_json(resp))


threading.Thread(target=refund_worker, daemon=True).start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
