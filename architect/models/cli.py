"""Adapters backed by a locally logged-in coding-agent CLI (no API key needed).

Each ``complete`` spawns one subprocess in a fresh, empty temporary directory so
no project instructions or files are visible, sends the prompt on stdin, bounds
captured stdout/stderr, and kills the whole process group on timeout,
oversized output or cancellation. Errors are fixed messages; provider output
and prompt content never appear in them.

Costs are API-equivalent estimates from the configured price table (and, for
Claude Code, the CLI's own API-equivalent figure). A subscription is billed
differently, so these numbers are safety reservations, not invoices.
Temperature is not configurable through either CLI and is ignored, and so is
``response_schema`` (no per-request constrained decoding); the caller's own
parser remains the check on every reply.
``max_output_tokens`` sizes the quote only. Neither CLI takes a per-request
output limit; Claude Code's env-level cap makes it continue in extra turns,
which costs more, so it is not set.

Images: ``quote`` reserves each image at the model's configured ceiling
(``pricing.image_reservation``), so a model without an image price refuses
before any process starts. Claude Code receives images through
``--input-format stream-json`` (one user message on stdin: the prompt text,
then base64 image blocks) and answers in JSONL whose single ``result`` line is
checked exactly like today's JSON result. Codex receives each image as a file
(``-i <root>/image-<n>.<ext>``) next to its instructions, outside the read-only
working directory. Text-only requests keep their argv and stdin byte-identical.
Both image paths are unverified against the live CLIs until ``bench probe``
confirms them, so any unexpected shape fails closed with ``AdapterError``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import JsonValue, TypeAdapter, ValidationError

from .images import MediaType, b64
from .pricing import Price, image_reservation, load_prices, price_for
from .types import AdapterError, ModelRequest, ModelResponse, UsageQuote

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
_DEFAULT_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
_READ_CHUNK = 64 * 1024
_REAP_SECONDS = 5.0
_IMAGE_SUFFIX: dict[MediaType, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}

# Credentials that would make Claude Code bill an API account instead of the
# keychain/subscription login. Pass ``api_key`` to opt back in explicitly.
_ANTHROPIC_CREDENTIALS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
# Same idea for Codex: an exported key would override the ChatGPT login.
_OPENAI_CREDENTIALS = ("OPENAI_API_KEY", "CODEX_API_KEY")
# Replaces each CLI's multi-thousand-token coding-agent system prompt, which
# would otherwise be billed on every model step. ``system_prompt=None`` keeps it.
MINIMAL_SYSTEM_PROMPT = (
    "Follow the user message exactly. Reply with only the requested output, "
    "as raw text with no markdown code fences."
)
# Chat-tuned CLIs still sometimes wrap a whole reply in one fenced block.
_WHOLE_FENCE = re.compile(r"\A\s*```[A-Za-z0-9_+-]*[ \t]*\n(.*?)\n```\s*\Z", re.DOTALL)
# Markers of an enclosing Claude Code session. Inheriting them would attach the
# child to the parent's session/messaging channel or force session persistence.
_PARENT_SESSION_VARS = (
    "CLAUDECODE",
    "CLAUDE_PID",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_FORCE_SESSION_PERSISTENCE",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
)


def _object(value: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise AdapterError("Provider response must contain an object")
    return value


def _tokens(value: JsonValue) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise AdapterError("Provider response is missing valid token usage")
    return value


def _text(value: JsonValue) -> str:
    if not isinstance(value, str):
        raise AdapterError("Provider response is missing text")
    return _unfence(value)


def _unfence(text: str) -> str:
    """Strip one fence only when it encloses the entire reply; content is unchanged."""
    match = _WHOLE_FENCE.match(text)
    if match is None or "```" in match.group(1):
        return text
    return match.group(1)


def _kill(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if sys.platform != "win32":
        # The child runs in its own session, so its pid is the group id.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        process.kill()


async def _reap(process: asyncio.subprocess.Process) -> None:
    """Kill and collect the child. asyncio's ``wait`` only returns once every pipe
    has reached EOF, so leftover output is drained and discarded (bounded)."""
    if process.returncode is not None:
        return
    _kill(process)
    if process.stdin is not None:
        process.stdin.close()

    async def drain(stream: asyncio.StreamReader | None) -> None:
        while stream is not None and await stream.read(_READ_CHUNK):
            pass

    # A surviving grandchild could hold a pipe open; never wait on it forever.
    with contextlib.suppress(TimeoutError, ProcessLookupError, ConnectionError):
        async with asyncio.timeout(_REAP_SECONDS):
            await asyncio.gather(drain(process.stdout), drain(process.stderr), process.wait())


async def _read_bounded(stream: asyncio.StreamReader | None, limit: int) -> bytes:
    if stream is None:
        return b""
    chunks: list[bytes] = []
    size = 0
    while chunk := await stream.read(_READ_CHUNK):
        size += len(chunk)
        if size > limit:
            raise AdapterError("Provider output exceeded size limit")
        chunks.append(chunk)
    return b"".join(chunks)


async def _feed(stream: asyncio.StreamWriter | None, data: bytes) -> None:
    if stream is None:
        return
    # A child that exits early closes its stdin; its output decides the result.
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        stream.write(data)
        await stream.drain()
        stream.close()
        await stream.wait_closed()


class _CLIAdapter:
    # Claude Code caches its own prompt prefix, so a call may bill a cache write.
    _writes_cache = False
    # ``max_output_tokens`` sizes the quote only (module docstring): a caller that would
    # cap a reply to bound its cost must not, or the reservation shrinks and the reply
    # does not.
    output_cap_enforced = False

    def __init__(
        self,
        model: str,
        *,
        executable: str,
        timeout: float,
        prices_path: Path | None,
        max_output_bytes: int,
    ) -> None:
        self.model = model
        self._price: Price = price_for(load_prices(prices_path), model)
        if not math.isfinite(timeout) or timeout <= 0:
            raise AdapterError("Timeout must be finite and positive")
        if max_output_bytes <= 0:
            raise AdapterError("Output bound must be positive")
        if not executable:
            raise AdapterError("Provider CLI executable is required")
        self._executable = executable
        self._timeout = timeout
        self._max_output_bytes = max_output_bytes

    def quote(self, request: ModelRequest) -> UsageQuote:
        # UTF-8 bytes bound text tokens; overhead covers the CLI's own framing.
        # Images reserve the configured ceiling each; unconfigured models raise here.
        tokens_in = (
            len(request.prompt.encode("utf-8"))
            + self._price.input_overhead_tokens
            + image_reservation(self._price, request.images)
        )
        tokens_out = (
            self._price.output_capacity_tokens
            if request.max_output_tokens is None
            else request.max_output_tokens
        )
        return UsageQuote(
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=self._price.quote_cost(
                tokens_in, tokens_out, may_write_cache=self._writes_cache
            ),
        )

    def _check_images(self, request: ModelRequest) -> None:
        # ``quote`` already refused unconfigured models; fail closed here too.
        image_reservation(self._price, request.images)

    async def _run(
        self, argv: list[str], stdin: bytes, cwd: Path, env: Mapping[str, str]
    ) -> tuple[int, bytes]:
        """Run once feeding ``stdin``; return (exit code, stdout).

        Kills the group on any early exit.
        """
        options: dict[str, Any] = {}
        if sys.platform == "win32":
            # Windows has no process-group kill here; only the direct child is killed.
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"] = True
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=dict(env),
                **options,
            )
        except OSError as exc:
            raise AdapterError("Provider CLI is unavailable") from exc
        try:
            async with asyncio.timeout(self._timeout):
                stdout = await self._exchange(process, stdin)
                code = await process.wait()
        except TimeoutError as exc:
            raise AdapterError("Provider CLI timed out") from exc
        finally:
            # Also runs on CancelledError and oversized output.
            await _reap(process)
        return code, stdout

    async def _exchange(self, process: asyncio.subprocess.Process, data: bytes) -> bytes:
        stdout = asyncio.create_task(_read_bounded(process.stdout, self._max_output_bytes))
        tasks: list[asyncio.Task[bytes] | asyncio.Task[None]] = [
            stdout,
            # stderr is drained (so the child cannot block) and bounded, never shown.
            asyncio.create_task(_read_bounded(process.stderr, self._max_output_bytes)),
            asyncio.create_task(_feed(process.stdin, data)),
        ]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in tasks:
                if task.done() and (error := task.exception()) is not None:
                    raise error
            return stdout.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _base_env(self, drop: tuple[str, ...]) -> dict[str, str]:
        return {
            key: value
            for key, value in os.environ.items()
            if key not in drop and key not in _PARENT_SESSION_VARS
        }


class ClaudeCodeAdapter(_CLIAdapter):
    """Claude Code (``claude -p``) using the local keychain/subscription login.

    ``ANTHROPIC_API_KEY`` and ``ANTHROPIC_AUTH_TOKEN`` are removed from the child
    environment so an exported key cannot silently switch billing to an API
    account; pass ``api_key`` to use one deliberately. ``--bare`` is not used
    because it disables the keychain login. Tools, settings, MCP servers, skills
    and session persistence are disabled; the working directory is empty, and
    the default system prompt is replaced by ``system_prompt``.

    ``cost_usd`` is the larger of the CLI's ``total_cost_usd`` and the price
    table (cache writes and reads priced at their configured rates). Both are
    API-equivalent; subscription usage is billed differently. The whole prompt,
    including any ``cache_prefix``, is sent on stdin exactly as given.
    """

    _writes_cache = True

    def __init__(
        self,
        model: str = "claude-haiku-4-5-20251001",
        *,
        executable: str = "claude",
        timeout: float = 300.0,
        api_key: str | None = None,
        system_prompt: str | None = MINIMAL_SYSTEM_PROMPT,
        prices_path: Path | None = None,
        max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
    ) -> None:
        super().__init__(
            model,
            executable=executable,
            timeout=timeout,
            prices_path=prices_path,
            max_output_bytes=max_output_bytes,
        )
        self._api_key = api_key
        self._system_prompt = system_prompt

    def _env(self) -> dict[str, str]:
        env = self._base_env(_ANTHROPIC_CREDENTIALS)
        if self._api_key:
            env["ANTHROPIC_API_KEY"] = self._api_key
        return env

    def _argv(self, request: ModelRequest) -> list[str]:
        """Pure argv builder; text-only argv is unchanged from the pre-image adapter."""
        formats = (
            ["--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
            if request.images
            else ["--output-format", "json"]
        )
        argv = [
            self._executable,
            "-p",
            *formats,
            "--tools",
            "",
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--no-session-persistence",
            "--disable-slash-commands",
            "--model",
            self.model,
        ]
        if self._system_prompt is not None:
            argv += ["--system-prompt", self._system_prompt]
        return argv

    def _stdin(self, request: ModelRequest) -> bytes:
        """Pure stdin builder: the prompt as UTF-8, or one stream-json user line."""
        if not request.images:
            return request.prompt.encode("utf-8")
        content: list[JsonValue] = [{"type": "text", "text": request.prompt}]
        for image in request.images:
            source: dict[str, JsonValue] = {
                "type": "base64",
                "media_type": image.media_type,
                "data": b64(image),
            }
            content.append({"type": "image", "source": source})
        message = {"type": "user", "message": {"role": "user", "content": content}}
        return json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"

    @staticmethod
    def _result(stdout: bytes, streamed: bool) -> dict[str, JsonValue]:
        """The result object: the whole JSON reply, or the single JSONL ``result`` line."""
        try:
            if not streamed:
                return _JSON_OBJECT.validate_json(stdout)
            events = [
                _JSON_OBJECT.validate_json(line)
                for line in stdout.decode("utf-8").splitlines()
                if line.strip()
            ]
        except (ValueError, ValidationError) as exc:
            raise AdapterError("Provider request failed or returned invalid JSON") from exc
        results = [event for event in events if event.get("type") == "result"]
        if len(results) != 1:
            raise AdapterError("Provider reported an error")
        return results[0]

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self._check_images(request)
        argv = self._argv(request)
        with tempfile.TemporaryDirectory(prefix="architect-claude-") as work:
            code, stdout = await self._run(argv, self._stdin(request), Path(work), self._env())
        data = self._result(stdout, bool(request.images))
        if (
            code != 0
            or data.get("type") != "result"
            or data.get("subtype") != "success"
            or data.get("is_error") is not False
        ):
            raise AdapterError("Provider reported an error")
        model_usage = _object(data.get("modelUsage"))
        accepted = {self.model, *self._price.allowed_response_models}
        if not accepted.intersection(model_usage):
            raise AdapterError("Provider response model does not match adapter")
        usage = _object(data.get("usage"))
        written = _tokens(usage["cache_creation_input_tokens"]) if (
            "cache_creation_input_tokens" in usage
        ) else 0
        read = _tokens(usage["cache_read_input_tokens"]) if (
            "cache_read_input_tokens" in usage
        ) else 0
        tokens_in = _tokens(usage.get("input_tokens")) + written + read
        tokens_out = _tokens(usage.get("output_tokens"))
        reported = data.get("total_cost_usd")
        if (
            not isinstance(reported, int | float)
            or isinstance(reported, bool)
            or not math.isfinite(reported)
            or reported < 0
        ):
            raise AdapterError("Provider response is missing valid cost")
        return ModelResponse(
            text=_text(data.get("result")),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=max(
                float(reported),
                self._price.cost(
                    tokens_in, tokens_out, cache_write_tokens=written, cache_read_tokens=read
                ),
            ),
            model=self.model,
            cache_write_tokens=written,
            cache_read_tokens=read,
        )


class CodexAdapter(_CLIAdapter):
    """Codex CLI (``codex exec``) using the local ChatGPT login.

    ``OPENAI_API_KEY`` and ``CODEX_API_KEY`` are removed from the child
    environment so the saved login is used. Runs read-only, ephemeral, without
    execpolicy rules, in an empty directory. Codex reports no cost and no model
    name, so cost comes from the price table (an API-equivalent estimate) and
    the model is the one requested with ``-m``. With ``system_prompt`` set, the
    base instructions are replaced via ``model_instructions_file`` and the
    environment/permissions/apps context blocks are turned off (Codex 0.154
    config keys; unknown keys are ignored by Codex without ``--strict-config``).
    """

    def __init__(
        self,
        model: str = "gpt-5.6-terra",
        *,
        executable: str = "codex",
        timeout: float = 300.0,
        system_prompt: str | None = MINIMAL_SYSTEM_PROMPT,
        prices_path: Path | None = None,
        max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
    ) -> None:
        super().__init__(
            model,
            executable=executable,
            timeout=timeout,
            prices_path=prices_path,
            max_output_bytes=max_output_bytes,
        )
        self._system_prompt = system_prompt

    def _argv(self, root: Path, request: ModelRequest) -> list[str]:
        """Pure argv builder over paths under ``root``; the caller writes the files.

        Instructions go to ``root/instructions.md`` and each image to
        ``root/image-<n>.<ext>`` (1-based), all outside the ``root/work`` cwd;
        one ``-i <path>`` per image precedes the trailing ``-``. Text-only argv
        is unchanged from the pre-image adapter.
        """
        argv = [
            self._executable,
            "exec",
            "--json",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--ignore-rules",
            "-m",
            self.model,
            "-o",
            str(root / "last-message.txt"),
        ]
        if self._system_prompt is not None:
            instructions = root / "instructions.md"
            for override in (
                f"model_instructions_file={json.dumps(str(instructions))}",
                "include_environment_context=false",
                "include_permissions_instructions=false",
                "include_apps_instructions=false",
            ):
                argv += ["-c", override]
        for path in self._image_paths(root, request):
            argv += ["-i", str(path)]
        argv.append("-")
        return argv

    @staticmethod
    def _image_paths(root: Path, request: ModelRequest) -> list[Path]:
        return [
            root / f"image-{index}.{_IMAGE_SUFFIX[image.media_type]}"
            for index, image in enumerate(request.images, start=1)
        ]

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self._check_images(request)
        with tempfile.TemporaryDirectory(prefix="architect-codex-") as root:
            work = Path(root) / "work"
            work.mkdir()
            output = Path(root) / "last-message.txt"
            argv = self._argv(Path(root), request)
            if self._system_prompt is not None:
                (Path(root) / "instructions.md").write_text(self._system_prompt, encoding="utf-8")
            paths = self._image_paths(Path(root), request)
            for path, image in zip(paths, request.images, strict=True):
                path.write_bytes(image.data)
            env = self._base_env(_OPENAI_CREDENTIALS)
            code, stdout = await self._run(argv, request.prompt.encode("utf-8"), work, env)
            tokens_in, tokens_out, cached = self._usage(stdout)
            if code != 0:
                raise AdapterError("Provider reported an error")
            text = _unfence(self._read_output(output))
        return ModelResponse(
            text=text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=self._price.cost(tokens_in, tokens_out, cache_read_tokens=cached),
            model=self.model,
            cache_read_tokens=cached,
        )

    def _usage(self, stdout: bytes) -> tuple[int, int, int]:
        completed = 0
        tokens_in = tokens_out = cached_in = 0
        failed = False
        failures: list[str] = []
        try:
            lines = stdout.decode("utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise AdapterError("Provider request failed or returned invalid JSON") from exc
        for line in lines:
            if not line.strip():
                continue
            try:
                event = _JSON_OBJECT.validate_json(line)
            except (ValueError, ValidationError) as exc:
                raise AdapterError("Provider request failed or returned invalid JSON") from exc
            kind = event.get("type")
            if kind == "turn.completed":
                usage = _object(event.get("usage"))
                cached = usage.get("cached_input_tokens", 0)
                input_tokens = _tokens(usage.get("input_tokens"))
                # OpenAI reports cached input as a subset of input_tokens.
                if _tokens(cached) > input_tokens:
                    raise AdapterError("Provider response is missing valid token usage")
                tokens_in += input_tokens
                cached_in += _tokens(cached)
                tokens_out += _tokens(usage.get("output_tokens"))
                completed += 1
            elif kind in ("error", "turn.failed"):
                failed = failed or kind == "turn.failed"
                message = event.get("message")
                error = event.get("error")
                if isinstance(error, dict):
                    message = error.get("message")
                failures.append(message if isinstance(message, str) else "")
        if any("usage limit" in message.lower() for message in failures):
            raise AdapterError("Provider usage limit reached")
        # Transient reconnect notices may precede a successful turn.
        if failed or completed == 0:
            raise AdapterError("Provider reported an error")
        return tokens_in, tokens_out, cached_in

    def _read_output(self, output: Path) -> str:
        try:
            with output.open("rb") as handle:
                data = handle.read(self._max_output_bytes + 1)
        except OSError as exc:
            raise AdapterError("Provider response is missing text") from exc
        if len(data) > self._max_output_bytes:
            raise AdapterError("Provider output exceeded size limit")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AdapterError("Provider response is missing text") from exc
