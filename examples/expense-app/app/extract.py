"""Reading receipts: the one place where the app turns receipt text into numbers.

extract(text) -> Receipt is the seam. Until Architect's receipt agent is added to this
project, it posts the text to the chat completions endpoint of a large general model on
Nebius Token Factory with the standard library's urllib, set up by three environment
variables:

    NEBIUS_API_KEY   the API key (required)
    NEBIUS_BASE_URL  the server's base URL, by default Nebius Token Factory
    NEBIUS_MODEL     the model, by default NVIDIA Nemotron 3 Ultra

NEBIUS_BASE_URL can also point at Architect's spend relay (deploy/relay), with an access
code as NEBIUS_API_KEY.

Once Add to project has written the agent into agents/receipt_reader (or
agents/receipt_reader_demo, the folder it gives the demo plan's agent), extract() uses it
instead. The agent follows the same rules (examples/receipts/RULES.md) and returns the
same four keys. It only runs if it passed its exam: when a file in its folder has changed
since, the call is refused before any model is called, and the ExtractionError says so in
the words Architect gives it ("Stopped: changed after its exam. 0 model calls."), with
what can be done about it.

EXTRACTOR says which of the two reads receipts. tests/test_rules.py is skipped while it
is "general-model", since those tests would pay for a model call each.
"""

from __future__ import annotations

import asyncio
import http.client
import importlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import ModuleType
from typing import Any

from pydantic import BaseModel, Field, ValidationError

APP_ROOT = Path(__file__).resolve().parents[1]
# Where Add to project writes the agent: the first is the folder of receipt-reader.json's
# agent, the second the one it gives receipt-reader-demo.json's. And the Architect
# settings file the agent runs with.
AGENT_DIRS = (
    APP_ROOT / "agents" / "receipt_reader",
    APP_ROOT / "agents" / "receipt_reader_demo",
)
PROFILE = APP_ROOT / "architect.json"


def agent_dir() -> Path | None:
    """The folder Architect's receipt agent is in, or None while it is not in this
    project. A folder that has lost its __init__.py still counts: the agent then says it
    is stopped, and the app does not quietly go back to the general model."""
    for folder in AGENT_DIRS:
        if (folder / "__init__.py").is_file() or (folder / "MANIFEST.json").is_file():
            return folder
    return None


def agent_added() -> bool:
    """Whether Architect's receipt agent is in this project."""
    return agent_dir() is not None


EXTRACTOR = "receipt-agent" if agent_added() else "general-model"
BASE_URL = "https://api.tokenfactory.nebius.com/v1"
MODEL = "nvidia/Nemotron-3-Ultra-550b-a55b"

# The receipt rules from examples/receipts/RULES.md, word for word (a test checks it).
RULES = """\
Return one JSON object with exactly these four keys: total, subtotal, tax, item_count.
- total: the total amount printed on the receipt, including any tax and service, on a line such as TOTAL, GRAND TOTAL, TL or PAID. A line that names a tender (cash, card, e-wallet, voucher), a payment discount or change is never the total. If no total is printed and nothing after the subtotal changes the amount (only tender and change lines, or a 0% discount), the subtotal is the total and subtotal is null.
- subtotal: the first amount printed after the items that sums them, apart from the total, whatever the receipt calls it: SUBTOTAL, ITEMS TOTAL, HARGA JUAL, DPP, NET TOTAL, the amount on an ITEMS line, a TOTAL line that a later GRAND TOTAL follows, or a bare amount under the items. A sum for one group of items (FOOD, BEVERAGE) is not the subtotal.
- tax: the tax amount printed on the receipt (TAX, VAT, PPN, PB1, PAJAK), not its rate, its base (DPP) or a service charge.
- item_count: the number of items listed on the receipt. Count each listed item once, whatever its quantity; the same item on two separate item lines counts twice. Bags and charges listed as items (plastic bag, take-away charge) count, even at price 0. Lines that belong to an item (options, toppings, extras, notes, package contents) are part of that item, even with their own quantity and price. Discount lines are not items. A void line that cancels an item is not an item; the cancelled item still counts.
- Amounts are integers: take the amount itself, without its label or rate, and delete every character that is not a digit 0-9. For example "75,000" is 75000, "Rp 12.500" is 12500, "-2.727" is 2727, "226,500.00" is 22650000 and "PB1 10% 4,818" is 4818.
- An amount the receipt does not print is null; never compute it from other amounts. A printed value without any digit, such as "-", is null.
- If the receipt prints the same field more than once, use the first (topmost) one."""  # noqa: E501


class Receipt(BaseModel):
    """What the app keeps from a receipt. Amounts are whole numbers made of the printed
    digits ("Rp 12.500" is 12500). None means the receipt does not print that amount."""

    total: int | None = Field(ge=0)
    subtotal: int | None = Field(ge=0)
    tax: int | None = Field(ge=0)
    item_count: int = Field(ge=0)


class ExtractionError(Exception):
    """The receipt could not be read. The message says why."""


class AgentStopped(ExtractionError):
    """The receipt agent refused to run and no model was called. ``sentence`` and
    ``reason`` are Architect's own words; ``next_step`` says what to do about it."""

    def __init__(self, sentence: str, reason: str, next_step: str) -> None:
        self.sentence = sentence
        self.reason = reason
        self.next_step = next_step
        super().__init__(" ".join(part for part in (sentence, reason, next_step) if part))


def extract(text: str) -> Receipt:
    """Read one receipt: with Architect's receipt agent when it is in this project, else
    with the general model."""
    if agent_added():
        return ask_receipt_agent(text)
    return ask_general_model(text)


_loaded: tuple[str, ModuleType, int] | None = None
# The two files of an agent that a retake reads; a change to any other can only be undone.
_EDITABLE = {"artifact/spec.json", "artifact/config.json"}


def _agent() -> ModuleType:
    """The package Add to project wrote into this project's agents folder.

    A package pins the exact version it was imported as. When Architect writes the folder
    again (a retake that passed, or an undo), it is imported again here, so the running
    app picks up the new version without a restart."""
    global _loaded
    folder = agent_dir()
    if folder is None:
        raise FileNotFoundError("the receipt agent is not in this project")
    name = f"agents.{folder.name}"
    stamp = (folder / "__init__.py").stat().st_mtime_ns
    if _loaded is None or _loaded[0] != name:
        _loaded = (name, importlib.import_module(name), stamp)
    elif _loaded[2] != stamp:
        _loaded = (name, importlib.reload(_loaded[1]), stamp)
    return _loaded[1]


def _stopped(exc: BaseException) -> AgentStopped | None:
    """Architect's refusal as an AgentStopped, when the agent is stopped.

    Architect says where the agent stands and what can be done: retake its exam, fix the
    file, or only undo (``standing`` reads the folder and this project's architect.json;
    no model is called). If that cannot be asked, the refusal itself still carries
    ``sentence`` and ``reason_line`` in plain words."""
    folder = agent_dir()
    if folder is not None:
        try:
            from architect.session.retake import standing

            state = standing(folder, PROFILE)
        except Exception:
            state = None
        if state is not None and state.state == "stopped":
            said = list(state.lines[1:2])
            if set(state.changed_files) <= _EDITABLE and not state.can_retake:
                # Why a change to the agent's own two files cannot be retaken as it is.
                said += [line for line in state.lines[2:3] if line != state.step]
            # A file with a mistake already ends "Fix it or undo."; no second sentence.
            step = "" if state.refusal == "edit_invalid" else state.step
            return AgentStopped(state.lines[0], " ".join(said), step)
    sentence = getattr(exc, "sentence", None)
    if not isinstance(sentence, str) or not sentence:
        return None
    reason = getattr(exc, "reason_line", None)
    kind = getattr(exc, "reason", None)
    if kind == "changed":
        step = "In Architect, retake its exam if the change allows it, or undo the change."
    elif kind == "changed_undo_only":
        step = "Undo the change in Architect."
    elif kind == "reloaded":
        step = ""
    else:
        step = "Set ARCHITECT_SIGNING_KEY_FILE to the key Architect signed it with."
    return AgentStopped(sentence, reason if isinstance(reason, str) else "", step)


def ask_receipt_agent(text: str) -> Receipt:
    """Read one receipt with the agent in this project's agents folder.

    The package checks itself each time it loads. If a file in it has changed since its
    exam, it raises before any model is called and this becomes an AgentStopped. Any other
    failure names its kind only; the error itself stays on the exception (``__cause__``)
    for the app's terminal, since it may hold a local path."""
    try:
        answer = asyncio.run(_agent().run({"receipt": text}, profile=PROFILE))
        return Receipt.model_validate(answer)
    except Exception as exc:
        stopped = _stopped(exc)
        if stopped is not None:
            raise stopped from exc
        if type(exc).__name__ == "FeatureDisabled":
            raise ExtractionError(f"the receipt agent is switched off (set {exc}=1)") from exc
        raise ExtractionError(f"the receipt agent failed ({type(exc).__name__})") from exc


def agent_line() -> str | None:
    """What the app's terminal shows about its agent when the app starts: its exam score,
    the pass mark and which attempt that was, or why it is stopped. None while the agent
    is not in this project. No model is called."""
    folder = agent_dir() if agent_added() else None
    if folder is None:
        return None
    try:
        from architect.forge_modes import exam_line

        return f"Receipt agent: {exam_line(folder)}"
    except Exception as exc:
        stopped = _stopped(exc)
        if stopped is not None:
            return f"Receipt agent: {stopped}"
        return f"Receipt agent: could not be checked ({type(exc).__name__})"


def ask_general_model(text: str) -> Receipt:
    """Send the rules and the receipt text to the model the NEBIUS_* variables name."""
    key = os.environ.get("NEBIUS_API_KEY")
    if not key:
        raise ExtractionError("NEBIUS_API_KEY is not set")
    base_url = (os.environ.get("NEBIUS_BASE_URL") or BASE_URL).rstrip("/")
    if not base_url.startswith(("https://", "http://")):
        raise ExtractionError("NEBIUS_BASE_URL is not an http(s) URL")
    reply = _post_json(
        f"{base_url}/chat/completions",
        key,
        {
            "model": os.environ.get("NEBIUS_MODEL") or MODEL,
            "messages": [
                {"role": "system", "content": RULES},
                {"role": "user", "content": text},
            ],
            "temperature": 0,
            "max_tokens": 512,
            # Nemotron reasons before it answers unless this is off. Token Factory reads it.
            "chat_template_kwargs": {"enable_thinking": False},
        },
    )
    try:
        content = reply["choices"][0]["message"].get("content")
    except (KeyError, IndexError, TypeError, AttributeError):
        raise ExtractionError("the model sent no answer") from None
    return parse_reply(content if isinstance(content, str) else None)


TIMEOUT_S = 60
RETRIES = 2  # after a 429, a 5xx or a lost connection: 1 s later, then 2 s after that


def _post_json(url: str, key: str, body: dict[str, Any]) -> Any:
    """POST ``body`` as JSON with ``key`` as the bearer token; the reply's JSON."""
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    for attempt in range(RETRIES + 1):
        if attempt:
            time.sleep(attempt)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            failure = f"HTTP {exc.code}"
            if exc.code != 429 and exc.code < 500:
                break  # a refused key or a bad request: trying again would not help
        except (OSError, http.client.HTTPException) as exc:
            failure = type(exc).__name__
        except ValueError:
            raise ExtractionError("the model's reply is not JSON") from None
    raise ExtractionError(f"the model call failed ({failure})")


def parse_reply(content: str | None) -> Receipt:
    """The receipt in a model's reply. Text around the JSON object, such as a code
    fence, is ignored."""
    text = content or ""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ExtractionError("the model did not answer with JSON")
    try:
        return Receipt.model_validate_json(text[start : end + 1])
    except ValidationError as exc:
        count = exc.error_count()
        raise ExtractionError(f"the model's answer is not a receipt ({count} problems)") from None
