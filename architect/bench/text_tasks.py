"""Seeded ``tickets`` and ``invoice`` generators with exact oracle tables.

Everything derives from ``random.Random(seed)``; the first ``n`` cases of a
larger draw equal a smaller draw with the same seed (each case consumes its
own draws in order). Several ticket templates have few or no placeholders, so
two seeds can print the same ticket; tickets are therefore split into two
disjoint pools by the sha256 of their text: ``generate_tickets`` draws only
from the ``main`` pool and ``generate_delivery_tickets`` only from the
``delivery`` pool, so a delivery draw never repeats a tuning or held-out case
at any size or seed. Invoices carry a random invoice number and random prices,
so the two seeds' draws do not collide (checked in tests). Oracle
tables map ``canonical_key(task)`` (sha256 of the canonical JSON of the task,
which the extractor preset embeds verbatim as the prompt ``input``) to the
expected answer. Manifests hold seed, ``n`` and the sha256 of the canonical
cases JSON, never the cases. All text is synthetic template output.
"""

from __future__ import annotations

import hashlib
import json
import random
from copy import deepcopy
from pathlib import Path
from typing import Literal

from pydantic import JsonValue, TypeAdapter, ValidationError

from architect.floors import EvaluationCase

from .rows import BenchError

__all__ = [
    "GENERATOR_VERSION",
    "INVOICE_ITEMS",
    "SENTIMENT_LABELS",
    "TICKET_LABELS",
    "canonical_json",
    "canonical_key",
    "cases_sha256",
    "generate_delivery_tickets",
    "generate_invoices",
    "generate_tickets",
    "ticket_pool",
    "load_sentiment_cases",
]

GENERATOR_VERSION = "v1"
TICKET_LABELS: tuple[str, ...] = ("billing", "bug", "feature", "other")
SENTIMENT_LABELS: tuple[str, ...] = ("positive", "negative", "neutral")
SENTIMENT_CASES = Path(__file__).resolve().parents[2] / "examples" / "live" / "sentiment-cases.json"

_CASES: TypeAdapter[list[EvaluationCase]] = TypeAdapter(list[EvaluationCase])


def canonical_json(value: JsonValue) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_key(value: JsonValue) -> str:
    """sha256 of the canonical JSON of a task; the oracle's text lookup key."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def cases_sha256(cases: list[EvaluationCase]) -> str:
    payload: JsonValue = [case.model_dump(mode="json") for case in cases]
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


_PRODUCTS = (
    "the mobile app",
    "the desktop client",
    "the web dashboard",
    "the API",
    "the browser extension",
    "the reporting page",
)
_FEATURES = (
    "dark mode",
    "CSV export",
    "two-factor login",
    "calendar sync",
    "bulk delete",
    "keyboard shortcuts",
    "a weekly digest email",
    "custom tags",
)
_ACTIONS = (
    "open the settings page",
    "upload a file",
    "click save",
    "switch accounts",
    "search for a record",
    "print a report",
)
_TEMPLATES: dict[str, tuple[str, ...]] = {
    "billing": (
        "I was charged ${amount} twice this month for {product}. Please refund the duplicate.",
        "My invoice shows ${amount} but my plan should cost ${amount2}. Why the difference?",
        "The card on file expired and my payment for {product} failed. How do I update it?",
        "Can I get a receipt for the ${amount} payment I made on the {day}th?",
        "I cancelled last month but was still billed ${amount}. Please stop the charges.",
        "Do you offer annual billing for {product}? The monthly ${amount} adds up.",
    ),
    "bug": (
        "Every time I {action} in {product} it crashes and I lose my work.",
        "The page goes blank after I {action}. Reloading does not help.",
        "{product_cap} shows an error code {code} whenever I {action}.",
        "Search returns no results in {product} even for records I can see in the list.",
        "Since the last update, {product} freezes for a minute when I {action}.",
        "Notifications arrive {day} hours late in {product}.",
    ),
    "feature": (
        "It would be great if {product} supported {feature}.",
        "Please add {feature}. Our team asks for it every week.",
        "Is {feature} on the roadmap for {product}? We would use it daily.",
        "Suggestion: {feature} would save us a lot of time when we {action}.",
        "Any plans for {feature}? Competing tools already have it.",
        "I would pay more for {product} if it had {feature}.",
    ),
    "other": (
        "Where can I find the documentation for {product}?",
        "What are your support hours on weekends?",
        "Can you point me to the status page for {product}?",
        "I would like to change the email address on my account.",
        "Is there a community forum for {product} users?",
        "How do I export my data before I move teams?",
    ),
}
# Ambiguous templates carry a cue for a second label; the label is fixed by the template.
_AMBIGUOUS: tuple[tuple[str, str], ...] = (
    ("billing", "After {product} crashed during checkout I was charged ${amount}; refund it."),
    ("bug", "The billing page in {product} shows an error code {code} when I {action}."),
    ("feature", "Not a bug, but {product} really needs {feature} on the billing page."),
    ("other", "Not a billing question: who do I contact about the newsletter for {product}?"),
    ("billing", "I know you are adding {feature}, but first fix the ${amount} double charge."),
    ("bug", "I asked for {feature} before, but right now {product} crashes when I {action}."),
    ("feature", "Support for {feature} would stop the crash reports we get when we {action}."),
    ("other", "This is not about a crash or a charge; how do I rename my workspace?"),
)


def _fill(template: str, rng: random.Random) -> str:
    product = rng.choice(_PRODUCTS)
    amount = rng.choice((9, 12, 19, 24, 29, 49, 79, 99))
    return template.format(
        product=product,
        product_cap=product[0].upper() + product[1:],
        feature=rng.choice(_FEATURES),
        action=rng.choice(_ACTIONS),
        amount=amount,
        amount2=amount - rng.choice((2, 5, 10)),
        day=rng.randint(2, 28),
        code=rng.choice(("E17", "500", "0x4a", "ERR_SYNC", "403")),
    )


def _metadata(**extra: JsonValue) -> dict[str, JsonValue]:
    base: dict[str, JsonValue] = {
        "synthetic": True,
        "source": "template",
        "generator_version": GENERATOR_VERSION,
    }
    base.update(extra)
    return base


TicketPool = Literal["main", "delivery"]


def ticket_pool(text: str) -> TicketPool:
    """The pool a ticket text belongs to: a fixed split by the first sha256 byte."""
    return "main" if hashlib.sha256(text.encode("utf-8")).digest()[0] < 128 else "delivery"


def generate_tickets(seed: int, n: int) -> tuple[list[EvaluationCase], dict[str, JsonValue]]:
    """Support tickets labelled billing|bug|feature|other; every 4th is ambiguous."""
    return _tickets(seed, n, "main")


def generate_delivery_tickets(
    seed: int, n: int
) -> tuple[list[EvaluationCase], dict[str, JsonValue]]:
    """Tickets from the ``delivery`` pool only: disjoint from every ``generate_tickets`` draw."""
    return _tickets(seed, n, "delivery")


def _tickets(
    seed: int, n: int, pool: TicketPool
) -> tuple[list[EvaluationCase], dict[str, JsonValue]]:
    if n < 0:
        raise ValueError("n must be nonnegative")
    rng = random.Random(seed)
    cases: list[EvaluationCase] = []
    table: dict[str, JsonValue] = {}
    seen: set[str] = set()
    for index in range(n):
        ambiguous = index % 4 == 3
        for _attempt in range(64):
            if ambiguous:
                label, template = _AMBIGUOUS[rng.randrange(len(_AMBIGUOUS))]
            else:
                label = TICKET_LABELS[rng.randrange(len(TICKET_LABELS))]
                template = rng.choice(_TEMPLATES[label])
            text = _fill(template, rng)
            if text not in seen and ticket_pool(text) == pool:
                break
        else:  # pragma: no cover - 40 templates x many slots never exhaust at suite sizes
            raise ValueError("Ticket templates exhausted")
        seen.add(text)
        task: JsonValue = {"ticket": text}
        expected: JsonValue = {"label": label}
        cases.append(
            EvaluationCase(
                case_id=f"tickets-{index:03d}" if pool == "main" else f"tickets-d{index:03d}",
                task=task,
                expected=expected,
                metadata=_metadata(ambiguous=ambiguous),
            )
        )
        table[canonical_key(task)] = deepcopy(expected)
    return cases, table


_VENDORS = (
    "Northwind Paper Co",
    "Bluehill Hardware",
    "Cedar Office Supply",
    "Harbor Light Printing",
    "Maple Street Catering",
    "Granite Tool Works",
    "Riverbend Textiles",
    "Summit Cleaning Services",
    "Lakeside Electrical",
    "Prairie Seed Company",
    "Ironwood Furniture",
    "Copperline Plumbing",
)
INVOICE_ITEMS: tuple[str, ...] = (
    "A4 copy paper (box)",
    "Toner cartridge",
    "Steel shelving unit",
    "Desk lamp",
    "Ethernet cable 5m",
    "Whiteboard markers",
    "Office chair",
    "Label printer",
    "Cleaning service (hour)",
    "Delivery fee",
    "Extension cord",
    "Storage bin",
)
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _date(rng: random.Random) -> tuple[str, str]:
    """Return (ISO date for the expected value, a messy printed form)."""
    year = rng.choice((2024, 2025, 2026))
    month = rng.randint(1, 12)
    day = rng.randint(1, 28)
    iso = f"{year:04d}-{month:02d}-{day:02d}"
    style = rng.randrange(3)
    if style == 0:
        printed = iso
    elif style == 1:
        printed = f"{day} {_MONTHS[month - 1]} {year}"
    else:
        printed = f"{_MONTHS[month - 1]} {day}, {year}"
    return iso, printed


def _invoice_text(
    rng: random.Random,
    vendor: str | None,
    date: str | None,
    items: list[tuple[str, int, float]],
    total: float | None,
) -> str:
    lines: list[str] = []
    header = rng.choice(("INVOICE", "Invoice", "TAX INVOICE", "Inv."))
    number = rng.randint(1000, 99999)
    lines.append(f"{header}  #{number}" if rng.random() < 0.5 else f"{header} no. {number}")
    if vendor is not None:
        lines.append(rng.choice((f"From: {vendor}", f"{vendor}", f"Vendor - {vendor}")))
    if date is not None:
        lines.append(rng.choice((f"Date: {date}", f"Issued {date}", f"dated {date}")))
    lines.append(rng.choice(("Bill to: Customer", "To: Accounts Payable", "")))
    separator = rng.choice((" x ", " @ ", "  "))
    for description, quantity, unit_price in items:
        style = rng.randrange(3)
        if style == 0:
            lines.append(f"{quantity}{separator}{description}{separator}{unit_price:.2f}")
        elif style == 1:
            lines.append(f"{description}   qty {quantity}   {unit_price:.2f} each")
        else:
            lines.append(f"- {description} ({quantity}) @ {unit_price:.2f}")
    if total is not None:
        lines.append(
            rng.choice((f"TOTAL {total:.2f}", f"Total due: {total:.2f}", f"Amount: {total:.2f}"))
        )
    lines.append(rng.choice(("Thank you", "Terms: net 30", "")))
    text = "\n".join(line for line in lines if line)
    if rng.random() < 0.3:
        text = text.replace("\n", "\n\n", 1)
    return text


def generate_invoices(seed: int, n: int) -> tuple[list[EvaluationCase], dict[str, JsonValue]]:
    """Messy plain-text invoices; every index ending in 3, 6 or 9 omits one field."""
    if n < 0:
        raise ValueError("n must be nonnegative")
    rng = random.Random(seed)
    cases: list[EvaluationCase] = []
    table: dict[str, JsonValue] = {}
    seen: set[str] = set()
    for index in range(n):
        vendor: str | None = _VENDORS[rng.randrange(len(_VENDORS))]
        iso, printed = _date(rng)
        date: str | None = iso
        items: list[tuple[str, int, float]] = []
        for _ in range(rng.randint(1, 4)):
            description = INVOICE_ITEMS[rng.randrange(len(INVOICE_ITEMS))]
            quantity = rng.randint(1, 12)
            unit_price = round(rng.uniform(2.0, 150.0), 2)
            items.append((description, quantity, unit_price))
        total: float | None = round(sum(q * p for _, q, p in items), 2)
        missing: str | None = None
        if index % 10 in {3, 6, 9}:
            missing = ("vendor", "date", "total")[(index // 10 + index) % 3]
            if missing == "vendor":
                vendor = None
            elif missing == "date":
                date = None
                printed = ""
            else:
                total = None
        text = _invoice_text(rng, vendor, printed if date is not None else None, items, total)
        if text in seen:  # pragma: no cover - random prices make collisions vanishingly rare
            raise ValueError("Invoice text collision")
        seen.add(text)
        task: JsonValue = {"invoice_text": text}
        expected: JsonValue = {
            "vendor": vendor,
            "date": date,
            "total": total,
            "line_items": [
                {"description": d, "quantity": q, "unit_price": p} for d, q, p in items
            ],
        }
        cases.append(
            EvaluationCase(
                case_id=f"invoice-{index:03d}",
                task=task,
                expected=expected,
                metadata=_metadata(missing_field=missing),
            )
        )
        table[canonical_key(task)] = deepcopy(expected)
    return cases, table


def load_sentiment_cases(
    path: Path | None = None,
) -> tuple[list[EvaluationCase], dict[str, JsonValue]]:
    """``examples/live/sentiment-cases.json`` as is, keyed by input hash."""
    try:
        cases = _CASES.validate_json((path or SENTIMENT_CASES).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, ValidationError) as exc:
        raise BenchError("bench_suite_unknown") from exc
    return cases, {canonical_key(case.task): deepcopy(case.expected) for case in cases}
