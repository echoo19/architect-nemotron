"""The expense API: post receipt text, list the receipts, add up the totals per day.

Run it with `uv run uvicorn app.main:app`. Receipts are kept in the SQLite file that
EXPENSES_DB names (expenses.db by default).

The app's terminal says where its receipt agent stands: one line when the app starts
("Receipt agent: passed its exam. ...") and two lines whenever the agent refuses a call
because its files changed after its exam.
"""

from __future__ import annotations

import datetime as dt
import functools
import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, StringConstraints

from app.extract import AgentStopped, ExtractionError, Receipt, agent_line, extract
from app.store import DayTotal, Store, StoredReceipt

Extractor = Callable[[str], Receipt]

# Plain lines in the terminal that runs the app, beside uvicorn's own.
log = logging.getLogger("expenses")
log.setLevel(logging.INFO)
log.propagate = False
if not log.handlers:
    log.addHandler(logging.StreamHandler())


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    line = agent_line()
    if line is not None:
        log.info(line)
    yield


app = FastAPI(title="Expenses", lifespan=lifespan)


class NewReceipt(BaseModel):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20_000)]
    date: dt.date | None = None  # the day of the expense; today when left out


@functools.cache
def get_store() -> Store:
    return Store(os.environ.get("EXPENSES_DB") or "expenses.db")


def get_extractor() -> Extractor:
    return extract


StoreDep = Annotated[Store, Depends(get_store)]


@app.post("/receipts", status_code=201)
def add_receipt(
    body: NewReceipt, store: StoreDep, read: Annotated[Extractor, Depends(get_extractor)]
) -> StoredReceipt:
    try:
        receipt = read(body.text)
    except AgentStopped as exc:
        # The agent's files changed after its exam: no model was called. Say so here too.
        log.warning(exc.sentence)
        log.warning(" ".join(part for part in (exc.reason, exc.next_step) if part))
        raise HTTPException(502, f"Could not read the receipt. {exc}") from exc
    except ExtractionError as exc:
        if exc.__cause__ is not None:
            # The reply names the kind of failure only. The detail stays in this terminal.
            log.warning("Receipt agent: %s: %s", type(exc.__cause__).__name__, exc.__cause__)
        raise HTTPException(502, f"Could not read the receipt: {exc}") from exc
    return store.add(body.date or dt.date.today(), body.text, receipt)


@app.get("/receipts")
def list_receipts(store: StoreDep) -> list[StoredReceipt]:
    return store.receipts()


@app.get("/receipts/{receipt_id}")
def get_receipt(receipt_id: int, store: StoreDep) -> StoredReceipt:
    receipt = store.get(receipt_id)
    if receipt is None:
        raise HTTPException(404, "No receipt with that id")
    return receipt


@app.get("/totals/daily")
def daily_totals(store: StoreDep) -> list[DayTotal]:
    return store.daily_totals()
