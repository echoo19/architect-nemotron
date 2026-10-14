"""The receipt rules from examples/receipts/RULES.md, run against the real extractor.

These tests wait for Architect's licensed receipt agent. They are skipped while
app/extract.py sends receipts to the general model (EXTRACTOR is "general-model"),
since each would be a paid model call, and they run as they are once the agent is in
agents/receipt_reader. The receipts were written for these tests; none comes from the CORD data.
"""

import pytest
from fastapi.testclient import TestClient

from app.extract import EXTRACTOR, Receipt, extract
from app.main import app, get_store

pytestmark = [
    pytest.mark.receipt_agent,
    pytest.mark.skipif(
        EXTRACTOR == "general-model",
        reason="waits for Architect's receipt agent in agents/receipt_reader",
    ),
]

RECEIPTS = {
    "digits-only": (
        "1 PAKET NASI AYAM Rp 35.000\n1 TEH MANIS Rp 6.000\nSUBTOTAL Rp 41.000\n"
        "TAX Rp 4.100\nTOTAL Rp 45.100\nTUNAI Rp 50.000\nKEMBALI Rp 4.900",
        Receipt(total=45100, subtotal=41000, tax=4100, item_count=2),
    ),
    "decimals-stay-digits": (
        "1 Matcha Cheesecake 45,000.00\n1 Hot Americano 30,000.00\nSubTotal 75,000.00\n"
        "Tax 7,500.00\nTotal 82,500.00\nPayment 100,000.00\nChange 17,500.00",
        Receipt(total=8250000, subtotal=7500000, tax=750000, item_count=2),
    ),
    "not-printed-is-null": (
        "2 ROTI BAKAR KEJU 36.000\n1 KOPI SUSU 15.000\nTOTAL 51.000\nTUNAI 100.000\nKEMBALI 49.000",
        Receipt(total=51000, subtotal=None, tax=None, item_count=2),
    ),
    "options-belong-to-their-item": (
        "2 Iced Caffe Latte 56.000\n+Less Sugar\n+Oat Milk\n1 Butter Croissant 22.000\n"
        "+Warm\nSubtotal 78.000\nTotal 78.000\nCash 100.000\nChange 22.000",
        Receipt(total=78000, subtotal=78000, tax=None, item_count=2),
    ),
    "service-is-not-tax": (
        "1 BEEF BLACK PEPPER RICE 76,500\n2 GREEN TEA LATTE 76,000\nSUBTOTAL 152,500\n"
        "SVC CHG 5% 7,625\nTAX 16,013\nTOTAL 176,138",
        Receipt(total=176138, subtotal=152500, tax=16013, item_count=2),
    ),
}


@pytest.mark.parametrize("text, expected", list(RECEIPTS.values()), ids=list(RECEIPTS))
def test_reads_the_receipt_by_the_rules(text, expected):
    assert extract(text) == expected


def test_the_api_adds_up_what_the_agent_reads(store):
    app.dependency_overrides[get_store] = lambda: store
    try:
        client = TestClient(app)
        for name in ("digits-only", "not-printed-is-null"):
            body = {"text": RECEIPTS[name][0], "date": "2026-10-01"}
            assert client.post("/receipts", json=body).status_code == 201
        assert client.get("/totals/daily").json() == [
            {"date": "2026-10-01", "receipts": 2, "total": 96100, "missing_total": 0}
        ]
    finally:
        app.dependency_overrides.clear()
