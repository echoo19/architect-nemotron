import datetime as dt

from app.extract import Receipt
from app.store import Store


def post(client, text, date=None):
    body = {"text": text} if date is None else {"text": text, "date": date}
    return client.post("/receipts", json=body)


def test_a_posted_receipt_is_stored_with_what_was_read(client):
    response = post(client, "lunch", "2026-10-01")
    assert response.status_code == 201
    stored = response.json()
    numbers = {"total": 52800, "subtotal": 48000, "tax": 4800, "item_count": 2}
    assert stored == {"id": 1, "date": "2026-10-01", "text": "lunch", **numbers}
    assert client.get("/receipts/1").json() == stored


def test_the_date_defaults_to_today(client):
    assert post(client, "coffee").json()["date"] == dt.date.today().isoformat()


def test_blank_text_is_refused(client):
    assert post(client, "   ").status_code == 422
    assert client.post("/receipts", json={}).status_code == 422


def test_a_receipt_that_cannot_be_read_is_not_stored(client):
    response = post(client, "smudged")
    assert response.status_code == 502
    assert response.json()["detail"] == (
        "Could not read the receipt: the fake reader does not know this receipt"
    )
    assert client.get("/receipts").json() == []


def test_receipts_are_listed_in_the_order_they_came(client):
    post(client, "coffee", "2026-10-02")
    post(client, "lunch", "2026-10-01")
    assert [r["text"] for r in client.get("/receipts").json()] == ["coffee", "lunch"]


def test_an_unknown_receipt_is_not_found(client):
    assert client.get("/receipts/7").status_code == 404


def test_totals_are_added_up_per_day(client):
    assert client.get("/totals/daily").json() == []
    post(client, "lunch", "2026-10-02")
    post(client, "coffee", "2026-10-01")
    post(client, "lunch", "2026-10-01")
    post(client, "no total", "2026-10-01")
    assert client.get("/totals/daily").json() == [
        {"date": "2026-10-01", "receipts": 3, "total": 77800, "missing_total": 1},
        {"date": "2026-10-02", "receipts": 1, "total": 52800, "missing_total": 0},
    ]


def test_receipts_survive_a_restart(tmp_path):
    path = str(tmp_path / "expenses.db")
    receipt = Receipt(total=25000, subtotal=None, tax=None, item_count=1)
    Store(path).add(dt.date(2026, 10, 1), "coffee", receipt)
    assert [r.total for r in Store(path).receipts()] == [25000]
