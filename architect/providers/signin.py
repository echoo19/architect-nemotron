"""Guided sign-in, model choice and first-run onboarding over a small prompt seam.

Both ``/login`` and the first run use these flows through a ``Prompter`` (``say``
and ``ask``, plus ``secret`` when the front end can read input without echoing).

- ``claude``: Architect never holds the claude login; it checks the CLI is on PATH
  and tells the user to run ``claude`` and ``/login`` there if calls fail.
- ``chatgpt``: runs the official ``codex login`` (OpenAI's browser OAuth flow) in
  the foreground with this terminal, then checks the login file.
- API providers: a key from the environment is used as is; otherwise the user
  pastes one. It is checked with one model-list request (a 401/403 is refused and
  nothing is stored), then stored in ``<workspace>/credentials.json``. A judge code
  pasted for Nebius is checked at the judges' relay instead and stored with the
  relay's URL (``providers.judge``).
- Local servers: the URL (Enter keeps the default) is checked with one model-list
  request. Architect never starts a local server or loads a model.
- ``custom``: a URL, an optional key, the same check.

Keys never appear in a message. Prompts state whether input is visible.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from architect.models.pricing import UNPRICED_LABEL, Price

from . import credentials, judge
from .catalog import ProviderEntry, default_base_url, find
from .discovery import Discovered, Discovery, list_models
from .host import Host
from .onboarding import (
    NEMOTRON_MODELS,
    Choice,
    ProviderError,
    bundled_price,
    onboarding_choices,
    recommended,
    register_model,
)
from .status import login_file_present

__all__ = [
    "SHOWN_MODELS",
    "Prompter",
    "SignInResult",
    "choose_model",
    "choose_provider",
    "first_run",
    "onboard",
    "price_label",
    "prompter_for_ui",
    "sign_in",
]

Level = Literal["info", "warn", "error"]
SHOWN_MODELS = 30
_URL = re.compile(r"^https?://[^\s/]+(/[^\s]*)?$")


@dataclass
class Prompter:
    say: Callable[[str, Level], None]
    ask: Callable[..., Awaitable[str | None]]  # (question, *, default=None, choices=())
    secret: Callable[[str], Awaitable[str | None]] | None = None


def prompter_for_ui(ui: Any) -> Prompter:
    """A ``Prompter`` over an ``architect.agent.events.UI`` (``emit`` + ``ask``)."""
    from architect.agent.events import Notice

    def say(text: str, level: Level = "info") -> None:
        ui.emit(Notice(text, level))

    secret = getattr(ui, "ask_secret", None)
    return Prompter(say, ui.ask, secret if callable(secret) else None)


@dataclass(frozen=True)
class SignInResult:
    ok: bool
    code: str | None = None
    models: tuple[Discovered, ...] = ()
    base_url: str | None = None
    message: str | None = None  # the failure in one sentence, for a front end to show


@dataclass
class _Flow:
    item: ProviderEntry
    workspace: Path
    prompter: Prompter
    host: Host = field(default_factory=Host)

    def say(self, text: str, level: Level = "info") -> None:
        self.prompter.say(text, level)

    async def ask(self, question: str, default: str | None = None,
                  choices: Sequence[str] = ()) -> str | None:
        return await self.prompter.ask(question, default=default, choices=choices)

    def takes_judge_code(self) -> bool:
        """Nebius, in a copy of Architect that knows the judges' relay."""
        return self.item.id == "nebius" and judge.relay_url(self.host.env) is not None

    async def ask_key(self, optional: bool = False) -> str | None:
        item = self.item
        where = credentials.path_for(self.workspace)
        ending = "Enter for none" if optional else "Enter cancels"
        what = f"{item.label} API key" + (" or judge code" if self.takes_judge_code() else "")
        if self.prompter.secret is not None:
            question = (f"Paste your {what} (hidden; stored in {where}, never "
                        f"logged; {ending})")
            answer = await self.prompter.secret(question + " ")
        else:
            question = (f"Paste your {what} (what you type is shown here; "
                        f"it is stored in {where} and never logged; {ending})")
            answer = await self.ask(question)
        if answer is None:
            return None
        return answer.strip()

    async def listing(self, *, base_url: str | None = None,
                      api_key: str | None = None) -> Discovery:
        return await list_models(self.item, workspace=self.workspace, base_url=base_url,
                                 api_key=api_key, host=self.host)


def price_label(item: ProviderEntry | None, model: Discovered | str) -> str:
    """``$in/$out per M``, ``$0 local``, or ``cost unknown``."""
    name = model if isinstance(model, str) else model.id
    price: Price | None = None if isinstance(model, str) else model.price
    if item is not None and item.kind == "local":
        return "$0 local"
    if price is None:
        price = bundled_price(name, item.id if item is not None else None)
    if price is None or price.unpriced:
        return UNPRICED_LABEL
    if item is not None and item.kind == "cli":
        return f"~${price.input_per_million:g}/${price.output_per_million:g} per M (plan)"
    return f"${price.input_per_million:g}/${price.output_per_million:g} per M"


# -- per-kind flows ---------------------------------------------------------------------


async def _claude(flow: _Flow) -> SignInResult:
    if not flow.host.which(flow.item.executable or "claude"):
        flow.say("The claude CLI is not on PATH. Install Claude Code, run 'claude' once and log "
                 "in, then /login claude again.", "error")
        return SignInResult(False, "cli_missing")
    flow.say("Architect uses the claude CLI's own login. If you are not logged in yet, run "
             "'claude' in a terminal and type /login there; nothing is stored here.")
    return SignInResult(True, models=tuple(Discovered(m) for m in flow.item.models))


async def _codex(flow: _Flow) -> SignInResult:
    executable = flow.host.which(flow.item.executable or "codex")
    if not executable:
        flow.say("The codex CLI is not on PATH. Install it (npm install -g @openai/codex), then "
                 "/login chatgpt again.", "error")
        return SignInResult(False, "cli_missing")
    models = tuple(Discovered(m) for m in flow.item.models)
    if login_file_present(flow.item, flow.host):
        again = await flow.ask("ChatGPT is already signed in through codex. Sign in again?",
                               default="n", choices=("y", "n"))
        if again is None or again.strip().lower() not in ("y", "yes"):
            return SignInResult(True, models=models)
    flow.say("Opening the official ChatGPT sign-in (codex login). Finish it in your browser; "
             "codex has this terminal until it exits.")
    code = await asyncio.to_thread(flow.host.runner, [executable, "login"])
    if code != 0:
        flow.say(f"codex login did not finish (exit code {code}). Nothing changed.", "error")
        return SignInResult(False, "login_failed")
    if not login_file_present(flow.item, flow.host):
        flow.say("codex login finished but no login was found; try /login chatgpt again.",
                 "warn")
        return SignInResult(False, "login_failed")
    flow.say("ChatGPT: signed in through codex.")
    return SignInResult(True, models=models)


async def _api(flow: _Flow) -> SignInResult:
    item = flow.item
    where = credentials.source(item.id, flow.workspace, env=flow.host.env)
    if where is not None and where != "file":
        flow.say(f"{item.label}: using {where} from the environment.")
        found = await flow.listing()
        if found.code == "unauthorized":
            flow.say(f"{where} was rejected (unauthorized). Fix or unset it, then /login "
                     f"{item.id} again.", "error")
            return SignInResult(False, "unauthorized")
        return SignInResult(True, models=found.models)
    if where == "file":
        stored = credentials.get(item.id, flow.workspace, env={})
        what = "judge code" if item.id == "nebius" and judge.is_code(stored) else "key"
        again = await flow.ask(f"{item.label} already has a stored {what}. Replace it?",
                               default="n", choices=("y", "n"))
        if again is None or again.strip().lower() not in ("y", "yes"):
            found = await flow.listing()
            return SignInResult(True, models=found.models)
    if item.key_url:
        judging = ", or paste a judge code" if flow.takes_judge_code() else ""
        flow.say(f"{item.label}: create a key at {item.key_url}{judging}")
    key = await flow.ask_key()
    if not key:
        flow.say("No key entered; nothing changed.", "warn")
        return SignInResult(False, "cancelled")
    if item.id == "nebius" and judge.is_code(key):
        return await _judge_code(flow, key)
    found = await flow.listing(api_key=key)
    if found.code == "unauthorized":
        flow.say(f"{item.label} rejected that key (unauthorized); it was not stored.", "error")
        return SignInResult(False, "unauthorized")
    try:
        credentials.set_key(item.id, key, flow.workspace)
    except credentials.CredentialError as exc:
        flow.say(f"The key was not stored ({exc.code}).", "error")
        return SignInResult(False, exc.code)
    if found.ok:
        flow.say(f"{item.label}: key works and is stored ({len(found.models)} models).")
    else:
        flow.say(f"{item.label}: key stored, but it could not be checked now ({found.code}).",
                 "warn")
    return SignInResult(True, models=found.models)


async def _judge_code(flow: _Flow, code: str) -> SignInResult:
    """A judge code pasted as the Nebius key: checked and stored with the judges' relay.

    The user has already agreed to replace any stored key, so the code may replace one.
    """
    result = await judge.use_code(code, flow.workspace, host=flow.host, replace=True)
    if not result.ok:
        message = judge.explain(result.code)
        flow.say(message, "error")
        return SignInResult(False, result.code, message=message)
    flow.say(judge.summary(result, flow.workspace))
    return SignInResult(True, models=result.models, base_url=result.base_url)


async def _url(flow: _Flow, current: str | None) -> str | None:
    item = flow.item
    for _ in range(3):
        answer = await flow.ask(f"{item.label} URL", default=current)
        if answer is None:
            return None
        url = answer.strip().rstrip("/")
        if not url:
            return None
        if _URL.fullmatch(url):
            return url
        flow.say("That is not an http(s) URL, e.g. http://localhost:8000/v1", "warn")
    return None


async def _server(flow: _Flow) -> SignInResult:
    item = flow.item
    current = (credentials.get_base_url(item.id, flow.workspace)
               or default_base_url(item, flow.host.env))
    url = await _url(flow, current)
    if url is None:
        flow.say("No URL; nothing changed.", "warn")
        return SignInResult(False, "cancelled")
    key: str | None = None
    if item.id == "custom":
        key = await flow.ask_key(optional=True)
        if key is None:
            return SignInResult(False, "cancelled")
    found = await flow.listing(base_url=url, api_key=key)
    if found.code in ("unauthorized", "no_key") and not key:
        flow.say(f"{url} asks for a key.")
        key = await flow.ask_key(optional=True)
        if key:
            found = await flow.listing(base_url=url, api_key=key)
    if not found.ok:
        if item.kind == "local":
            flow.say(f"Nothing answered at {url} ({found.code}). Start {item.label} yourself "
                     f"(Architect never starts a local server or model), then /login {item.id} "
                     "again.", "error")
        else:
            flow.say(f"{url} did not answer a model list ({found.code}); nothing stored.",
                     "error")
        return SignInResult(False, found.code, base_url=url)
    try:
        if url != item.base_url or credentials.get_base_url(item.id, flow.workspace):
            credentials.set_base_url(item.id, url, flow.workspace)
        if key:
            credentials.set_key(item.id, key, flow.workspace)
    except credentials.CredentialError as exc:
        flow.say(f"Could not save the connection ({exc.code}).", "error")
        return SignInResult(False, exc.code)
    cost = "$0, " if item.kind == "local" else ""
    flow.say(f"{item.label}: reachable at {url} ({cost}{len(found.models)} models).")
    return SignInResult(True, models=found.models, base_url=url)


async def sign_in(
    item: ProviderEntry, workspace: Path, prompter: Prompter, *, host: Host | None = None
) -> SignInResult:
    """Run the provider's sign-in flow; never raises for user or network failures."""
    flow = _Flow(item, workspace, prompter, host or Host())
    if item.sign_in == "claude_cli":
        return await _claude(flow)
    if item.sign_in == "codex_login":
        return await _codex(flow)
    if item.sign_in == "api_key":
        return await _api(flow)
    return await _server(flow)


# -- choosing ---------------------------------------------------------------------------


def _numbered(item: ProviderEntry, models: Sequence[Discovered], limit: int) -> str:
    lines = [f"  {index}. {model.id}  ({price_label(item, model)})"
             for index, model in enumerate(models[:limit], start=1)]
    if len(models) > limit:
        lines.append(f"  ... {len(models) - limit} more: type a name or a word to filter")
    return "\n".join(lines)


async def choose_model(
    item: ProviderEntry,
    models: Sequence[Discovered],
    prompter: Prompter,
    *,
    allow_skip: bool = False,
) -> Discovered | None:
    """Ask for one model: a number, an exact name, or a word that filters the list.

    Enter takes the provider's default (when listed or when nothing could be
    listed), else the first model; with ``allow_skip`` Enter skips instead. When
    nothing could be listed, a typed name is accepted as is.
    """
    names = {model.id: model for model in models}
    default = item.default_model if (item.default_model in names or not models) else (
        models[0].id if models else None)
    if allow_skip:
        default = None
    shown = list(models)
    for _ in range(4):
        if shown:
            prompter.say(f"{item.label} models:\n" + _numbered(item, shown, SHOWN_MODELS), "info")
        hint = "Enter skips" if allow_skip else "Enter = suggested"
        answer = await prompter.ask(f"Model (number, name or a word to filter; {hint})",
                                    default=default, choices=())
        if answer is None:
            return None
        text = answer.strip()
        if not text:
            return None
        if text.isdigit() and 1 <= int(text) <= min(len(shown), SHOWN_MODELS):
            return shown[int(text) - 1]
        if text in names:
            return names[text]
        matches = [model for model in models if text.lower() in model.id.lower()]
        if len(matches) == 1:
            return matches[0]
        if matches:
            shown = matches
            continue
        if not models:
            return Discovered(text)
        prompter.say(f"No {item.label} model matches '{text}'.", "warn")
        shown = list(models)
    return None


def _choice_line(choice: Choice) -> str:
    tag = "  (recommended)" if choice.recommended else ""
    return f"  {choice.index}. {choice.provider.label}: {choice.status.detail}{tag}"


async def choose_provider(
    choices: Sequence[Choice], prompter: Prompter, *, question: str = "Provider"
) -> ProviderEntry | None:
    """Show the numbered providers and read one (number or id; Enter = recommended)."""
    prompter.say("Providers:\n" + "\n".join(_choice_line(c) for c in choices), "info")
    best = recommended(choices)
    for _ in range(3):
        answer = await prompter.ask(
            f"{question} (number or name; Enter = {best.provider.id if best else 'none'})",
            default=best.provider.id if best else None, choices=())
        if answer is None:
            return None
        text = answer.strip().lower()
        if not text:
            return None
        if text.isdigit():
            picked = next((c.provider for c in choices if c.index == int(text)), None)
        else:
            picked = find(text)
        if picked is not None:
            return picked
        prompter.say(f"'{text}' is not in the list.", "warn")
    return None


async def onboard(
    prompter: Prompter,
    workspace: Path,
    profile_path: Path,
    *,
    host: Host | None = None,
    probe_local: bool = True,
) -> str:
    """The first run: pick a provider, sign in, pick a model, write the profile.

    Returns the profile model key (the conductor's model). Raises
    ``ProviderError("setup_cancelled")`` when the user backs out and
    ``ProviderError("no_provider")`` after three failed sign-ins.
    """
    host = host or Host()
    choices = await onboarding_choices(workspace, host=host, probe_local=probe_local)
    prompter.say("First run: Architect can use any of these. Detected status is shown; local "
                 "models are $0 and run on this machine only if you pick one.", "info")
    for _ in range(3):
        item = await choose_provider(choices, prompter)
        if item is None:
            raise ProviderError("setup_cancelled")
        result = await sign_in(item, workspace, prompter, host=host)
        if not result.ok:
            prompter.say("Pick another provider, or press Ctrl+C to stop.", "warn")
            continue
        model = await choose_model(item, result.models, prompter)
        if model is None:
            raise ProviderError("setup_cancelled")
        fresh = not await asyncio.to_thread(profile_path.exists)
        key = register_model(profile_path, item.id, model.id, workspace=workspace,
                             base_url=result.base_url, price=model.price, host=host)
        label = price_label(item, model)
        prompter.say(f"Profile written: {profile_path} ({key} via {item.label}, {label}).",
                     "info")
        if fresh and item.id == "nebius" and key in NEMOTRON_MODELS:
            prompter.say(f"Nemotron roles: you talk to {key}, Super writes each agent's spec, "
                         "and built agents run on Lightning, else Super. Builds tune a built-in "
                         "strategy (a preset) on the practice cases; if you choose Forge, Super "
                         "writes a custom harness instead. Change these in the profile or with "
                         "/model.",
                         "info")
        if label == UNPRICED_LABEL:
            prompter.say("This model has no known price: conversation and subagents run on a "
                         "time budget, and build mode will not compile on it.", "warn")
        return key
    raise ProviderError("no_provider")


async def first_run(
    ui: Any,
    workspace: Path,
    *,
    profile_name: str = "session-profile.json",
    host: Host | None = None,
    probe_local: bool = True,
) -> Path:
    """What ``app.py`` calls on the first run: ``onboard`` over an agent ``UI``.

    Writes ``<workspace>/<profile_name>`` (the session profile ``discover_profile``
    finds next time) and returns its path. Raises ``ProviderError`` with
    ``setup_cancelled`` (the user backed out) or ``no_provider``.
    """
    path = workspace / profile_name
    await onboard(prompter_for_ui(ui), workspace, path, host=host, probe_local=probe_local)
    return path
