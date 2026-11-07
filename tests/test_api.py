import pytest
from fastapi.testclient import TestClient

from ledgercore.api import create_app
from ledgercore.ledger import Ledger
from tests.helpers import T0, seed


@pytest.fixture
def client(tmp_path, clock):
    path = tmp_path / "api.db"
    led = Ledger(path, clock)
    seed(led, clock)
    led.close()
    with TestClient(create_app(str(path), clock)) as c:
        yield c


def buy(client, order_id="o1", gross=5000, key=None):
    return client.post(
        "/v1/templates/ticket_purchase",
        headers={"Idempotency-Key": key or f"buy-{order_id}"},
        json={"params": {"order_id": order_id, "organizer": "northbeam",
                         "event_id": "evt-1", "gross": gross}},
    )


def test_health_and_metrics(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    buy(client)
    text = client.get("/metrics").text
    assert 'ledgercore_entries_posted_total{template="ticket_purchase"} 1' in text
    assert 'ledgercore_invariant_ok{check="zero_sum_per_currency"} 1' in text
    assert "ledgercore_http_request_seconds_bucket" in text


def test_idempotency_key_required(client):
    r = client.post("/v1/templates/ticket_purchase", json={"params": {}})
    assert r.status_code == 422 and "Idempotency-Key" in r.json()["message"]


def test_create_replay_and_mismatch(client):
    r1 = buy(client, key="k1")
    assert r1.status_code == 201
    r2 = buy(client, key="k1")
    assert r2.status_code == 200 and r2.headers["Idempotent-Replayed"] == "true"
    assert r2.json()["id"] == r1.json()["id"]
    r3 = buy(client, gross=6000, key="k1")
    assert r3.status_code == 422 and r3.json()["error"] == "idempotency_mismatch"


def test_float_amount_rejected(client):
    r = buy(client, gross=50.0)
    assert r.status_code == 422 and "integer" in r.json()["message"]
    r = client.post("/v1/payouts", headers={"Idempotency-Key": "p"},
                    json={"organizer": "northbeam", "currency": "USD", "amount": 10.5})
    assert r.status_code == 422


def test_balances_statement_and_entries(client):
    buy(client, "o1")
    buy(client, "o2", 10000)
    b = client.get("/v1/organizers/northbeam/balances").json()
    assert b["escrow"] == 4651 + 9401
    acc = client.get("/v1/accounts/org:northbeam:escrow:evt-1:USD/balance").json()
    assert acc["balance"] == 14052
    st = client.get("/v1/accounts/org:northbeam:escrow:evt-1:USD/statement").json()
    assert st["closing_balance"] == 14052 and len(st["lines"]) == 4
    page = client.get("/v1/entries?limit=2").json()
    assert len(page["entries"]) == 2
    older = client.get(f"/v1/entries?limit=5&before_id={page['next_before_id']}").json()
    assert older["entries"][0]["template"] == "capital_injection"
    assert client.get("/v1/organizers/northbeam/history").json()[-1]["escrow"] == 14052


def test_point_in_time_query(client):
    buy(client, "o1")
    early = "2026-11-01T00:00:00Z"
    r = client.get(f"/v1/accounts/org:northbeam:escrow:evt-1:USD/balance?as_of={early}")
    assert r.json()["balance"] == 0


def test_payout_flow_and_overdraft(client, clock):
    from datetime import timedelta
    buy(client, "o1")
    clock.set(T0 + timedelta(days=2))
    client.post("/v1/templates/escrow_release", headers={"Idempotency-Key": "rel"},
                json={"params": {"organizer": "northbeam", "event_id": "evt-1"}})
    r = client.post("/v1/payouts", headers={"Idempotency-Key": "p1"},
                    json={"organizer": "northbeam", "currency": "USD", "amount": 99999})
    assert r.status_code == 422 and r.json()["error"] == "insufficient_funds"
    hold = client.post("/v1/payouts", headers={"Idempotency-Key": "p2"},
                       json={"organizer": "northbeam", "currency": "USD", "amount": 4000}).json()
    assert client.get("/v1/organizers/northbeam/balances").json()["pending_payouts"] == 4000
    cap = client.post(f"/v1/holds/{hold['hold_uid']}/capture",
                      headers={"Idempotency-Key": "c1"}, json={"amount": 3000})
    assert cap.status_code == 201 and cap.json()["entry"]["template"] == "organizer_payout"
    assert client.get("/v1/organizers/northbeam/balances").json()["available"] == 1651


def test_stale_version_returns_409_with_retry_hint(client):
    buy(client, "o1")
    r = client.post("/v1/templates/refund_before_payout", headers={"Idempotency-Key": "r1"},
                    json={"params": {"order_id": "o1"},
                          "expected_versions": {"org:northbeam:escrow:evt-1:USD": 0}})
    assert r.status_code == 409
    body = r.json()
    assert body["retryable"] is True and body["retry_after_ms"] > 0
    assert r.headers["Retry-After"] == "1"


def test_reverse_endpoint(client):
    e = buy(client, "o1").json()
    r = client.post(f"/v1/entries/{e['id']}/reverse", headers={"Idempotency-Key": "rev"},
                    json={"reason": "duplicate capture"})
    assert r.status_code == 201 and r.json()["reverses_entry_id"] == e["id"]
    assert client.get("/v1/invariants").json()["ok"] is True


def test_authorization_and_transfer(client):
    a = client.post("/v1/authorizations", headers={"Idempotency-Key": "a1"},
                    json={"order_id": "o5", "organizer": "northbeam", "event_id": "evt-1",
                          "amount": 9000})
    assert a.status_code == 201 and a.json()["kind"] == "card_auth"
    b = client.get("/v1/organizers/northbeam/balances").json()
    assert b["incoming_authorizations"] == 9000
    t = client.post("/v1/transfers", headers={"Idempotency-Key": "t1"},
                    json={"debit_account": "platform:fx_position:USD",
                          "credit_account": "platform:bank:USD", "amount": 100})
    assert t.status_code == 201


def test_not_found_and_ui(client):
    assert client.get("/v1/organizers/nobody/balances").status_code == 404
    assert client.get("/v1/entries/9999").status_code == 404
    assert client.post("/v1/templates/nope", headers={"Idempotency-Key": "x"},
                       json={"params": {}}).status_code == 404
    page = client.get("/")
    assert page.status_code == 200
    assert "<title>ledger-core</title>" in page.text
    names = {t["name"] for t in client.get("/v1/templates").json()}
    assert {"ticket_purchase", "chargeback", "fx_conversion"} <= names
