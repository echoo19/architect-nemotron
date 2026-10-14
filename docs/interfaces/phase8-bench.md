# Phase 8: image inputs on every adapter, a synthetic exam-page vision floor, and a measured bench-and-iterate loop

Status: contract (2026-09-26), not yet implemented. Extends `docs/interfaces/phase7-session.md` and the Phase 5/6 CLI contracts without changing any existing subcommand, verb, state or code. Where this text and the code differ after implementation, the code, `docs/usage.md` and `docs/bench.md` are authoritative.

This document is the contract for four implementers working in parallel on disjoint files (Section 18) plus one integration package. Every public signature, file path, closed label, error code, JSON shape and operator-facing string below is fixed; anything not listed is an implementation detail owned by the file's author. Line references are to the checkout at commit `b5ad2d6`.

## 1. Goal and fixed properties

Phase 8 delivers five things:

1. `ModelRequest.images` carried honestly through all six adapters (fake, anthropic, openai-compatible, ollama, claude-code, codex). The per-image reservation is the model's declared ceiling from `prices.json`, unconditionally, so `quote()` stays a true upper bound independent of any remembered provider formula. Provider formulas exist only as a labelled `estimate` column.
2. `architect/bench/`: a runner that takes a frozen, versioned suite and, for every (task, model, variant, repeat) cell, runs the real `Compiler.compile(spec, approved=True)` path (Stages 1, 2, 4) in a throwaway workspace, then writes one content-free `BenchRow` with score, licensed, cost, calls, tokens, wall time, reservation bound and evidence label.
3. A synthetic exam-page generator (rendered PNG pages with known ground truth: questions, choices, answer keys) that gives a vision-extraction floor shaped like Lectern's test digitizer (Reviewer A vision extraction) without any tenant data, code or path.
4. ARC-AGI-3 offline public preview in the suite with configurable `--arc-games` / `--arc-actions`, through a reusable `architect/games/public.py` lifted from `scripts/arc_harness.py`.
5. `architect bench run|compare|list|pages|probe`, the session verb `bench`, and a CI job that runs the whole smoke tier on the fake provider at $0 with deterministic oracle adapters.

Fixed properties, inherited from the existing seams and never weakened:

- Stage 4 (`Compiler` -> `verification`) is the only licensing authority. The bench never constructs, edits, copies or re-signs a `TestReport`; `licensed` is `HarnessArtifact.licensed` re-read from disk; the tuning replay (Section 8.3) touches only `FloorEvaluator.tuning` (public by design, `floors/base.py:144`); `FloorEvaluator.held_out` stays private (`base.py:148-149`). No artifact is installed.
- Quote before I/O: `RunContext._call` reserves `adapter.quote(request)` before `complete` (`runtime/__init__.py:391-427`) and raises `BudgetExceeded("Adapter exceeded its reserved usage")` when measured cost exceeds the quote. Image reservations are added inside `quote()` so this safety net covers images without any runtime change.
- Budgets are dollars and seconds; tokens are metered, never capped. Nothing in this phase adds a token gate.
- Telemetry stays content-free. One new column, `TelemetryRow.image_inputs: NonnegativeInt = 0`. No media type, dimension, hash, prompt, page text, case id or output ever enters telemetry or a bench row.
- `ModelRequest`, `Price`, `TelemetryRow`, `BenchRow` and every new model are `extra="forbid"`, frozen.
- Nothing under `{lectern-checkout}` is read, imported, copied or referenced. The bench references only shapes in `examples/lectern/` (already synthetic).
- Rendering is ASCII only, no ANSI, at most 100 columns, deterministic for a given input; money is labelled "API-equivalent, not a bill" for CLI providers.

## 2. File list, responsibilities, owners

| File | Responsibility | Owner |
|---|---|---|
| `architect/models/images.py` (new) | `ImageInput`, `ImageStub`, `ImageRef`; stdlib header sniffing; base64/data-URL helpers; stdlib PNG encoder | WP1 |
| `architect/models/types.py` (edit) | `ModelRequest.images`, `ModelRequest.image_detail`; re-export `ImageInput` | WP1 |
| `architect/models/pricing.py` (edit) + `prices.json` (edit) | `ImagePricing`, `Price.image`, `image_reservation`, `image_estimate`; docstring with every image number and its basis | WP1 |
| `architect/models/http.py` (edit) | quote adds reservation; Anthropic block list; OpenAI parts; Ollama `/api/chat` with images | WP1 |
| `architect/models/fake.py` (edit) | priced fake images; `RequestShape` recording | WP1 |
| `architect/models/cli.py` (edit) | `_run(argv, stdin: bytes, ...)`; Claude Code stream-json image input; Codex `-i` files; pure `_argv`/`_stdin` builders | WP2 |
| `architect/runtime/__init__.py` (edit) | `RunContext.call(images=)`, `attach_images`, `images_sent`; `TelemetryRow.image_inputs` written by `_execute` | WP2 |
| `architect/runtime/sandbox.py` (edit) + `_sandbox_worker.py` (edit) | host-side lifting before the initial message; `call` op gains `images` (sha refs); `_RPC.call(images=)` | WP2 |
| `architect/runtime/pipeline.py` (edit) | lift once at entry; attach per stage policy | WP2 |
| `architect/presets/images.py` (new) + `_execution.py` (edit) | `lift_images`, `attach_stages`; extractor prompt gains `attached_images`; self-check attaches | WP2 |
| `architect/telemetry/schema.py` (edit) | `TelemetryRow.image_inputs` | WP2 |
| `architect/floors/checkers.py` (new) + `profiles.py` (edit) | closed checker table; `FloorConfig.checker`; `floor_factory` passes the checker | WP2 |
| `architect/bench/__init__.py` (new) | package facade | WP3 |
| `architect/bench/rows.py` (new) | `BenchRow`, `write_rows`, `read_rows`, `preset_fingerprint`, `prices_sha256`, `architect_commit` | WP3 |
| `architect/bench/compare.py` (new) | `wilson`, `mcnemar_exact`, `align`, `compare`, `verdict` | WP3 |
| `architect/bench/pages.py` (new), `architect/bench/_font.py` (new) | exam-page generator: stdlib bitmap renderer (`clean`), Pillow renderer (`scan`, `dense`); `PageTruth`; `write_dataset`; manifest | WP3 |
| `architect/bench/text_tasks.py` (new) | seeded `tickets` and `invoice` generators and oracle tables | WP3 |
| `architect/bench/oracle.py` (new) | `OracleAdapter`, `NoisyOracle`, `OracleState`, `oracle_factory` | WP3 |
| `architect/bench/suite.py` (new) | frozen suite `v1`: `BenchTask`, `SUITES`, `TIERS`, `suite_digest`, spec builders, call counting | WP3 |
| `architect/bench/runner.py` (new) | `run_bench`: spec -> lifted budget -> temp workspace -> `Compiler.compile(approved=True)` -> tuning replay -> delivery replay -> `BenchRow` | WP4 |
| `architect/bench/providers.py` (new) | `build_factory`, `evidence_label`, `probe_models`, `ProbeRow` | WP4 |
| `architect/bench/render.py` (new) | `bench_table`, `compare_table`, `probe_table`, `list_table` (ASCII, <=100 cols) | WP4 |
| `architect/games/public.py` (new) | `fetch_public_games`, `stage`, `FixtureAgent`, `PreviewOptions`, `manufacture`, `close_workspace` lifted from `scripts/arc_harness.py` | WP4 |
| `scripts/arc_harness.py` (edit) | thin CLI over `architect/games/public.py`; argv and summary keys byte-for-byte unchanged | WP4 |
| `architect/cli.py` (edit) | `bench` subcommands (Section 10) | WP4 |
| `architect/session/{state,commands,codes,render,session}.py` (edit), `architect/session/bench.py` (new) | `State.BENCHING`, verb `bench`, codes, tables, handler | WP4 |
| `.github/workflows/ci.yml` (edit) | `bench-smoke` job (Linux, `--extra bench`) and a Windows smoke step | WP4 |
| `pyproject.toml` (edit) | optional extra `bench = ["pillow>=10.1,<13"]` | WP3 |
| `examples/bench/` (new) | `suite-v1/{tickets-manifest.json, invoice-manifest.json, pages-manifest.json, smoke-baseline.jsonl}`, `results/<bench_id>.jsonl`, `README.md` | WP3 (manifests), WP5 (results) |
| `tests/image_support.py` (new) | struct+zlib PNG/JPEG/WebP/GIF header builders shared by tests | WP1 |
| `tests/test_images.py`, `tests/test_models.py` (edit), `tests/test_prompt_caching.py` (edit) | Section 15 | WP1 |
| `tests/test_runtime.py`, `test_sandbox.py`, `test_presets.py`, `test_pipeline.py`, `test_floors.py`, `test_profiles.py`, `test_telemetry.py`, `test_models_cli.py` (edits) | Section 15 | WP2 |
| `tests/test_bench_pages.py`, `test_bench_text_tasks.py`, `test_bench_oracle.py`, `test_bench_compare.py`, `test_bench_rows.py`, `test_bench_suite.py` (new) | Section 15 | WP3 |
| `tests/test_bench_run.py`, `test_bench_providers.py`, `test_games_public.py`, `test_arc_harness_script.py` (edit), `test_cli.py` (edit), `test_session_bench.py`, `tests/adversarial/test_phase8_bench.py` (new) | Section 15 | WP4 |
| `tests/live/test_models_live.py` (edit) | opt-in image round trips per provider | WP5 |
| `docs/bench.md`, `docs/bench/decisions.md`, `docs/telemetry.md`, `docs/usage.md`, `docs/limitations.md`, `examples/telemetry/README.md`, `PROGRESS.md` | usage, evidence conventions, decision log | WP5 |

Package boundary: `architect/bench/` imports from `architect.models`, `architect.runtime`, `architect.compiler`, `architect.floors`, `architect.presets`, `architect.spec`, `architect.telemetry`, `architect.games`, `architect.profiles`. Nothing outside `architect/bench/`, `architect/cli.py` and `architect/session/bench.py` imports `architect.bench`. `tests/test_package_boundary.py` gains that assertion.

## 3. Image inputs (WP1)

### 3.1 `architect/models/images.py`

```python
MediaType = Literal["image/png", "image/jpeg", "image/webp", "image/gif"]
MAX_IMAGE_BYTES = 20 * 1024 * 1024      # request-level cap; per-model caps live in ImagePricing
MAX_IMAGES_PER_REQUEST = 100
SHA256_HEX = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

class ImageInput(BaseModel):            # frozen, extra="forbid"
    data: Annotated[bytes, Field(min_length=1, max_length=MAX_IMAGE_BYTES, strict=True)]
    media_type: MediaType
    width: Annotated[int, Field(gt=0, strict=True)]
    height: Annotated[int, Field(gt=0, strict=True)]
    cache: bool = False                 # Anthropic: cache_control on this image block
    # validator: sniff(data) must equal (media_type, width, height); ValueError otherwise
    @classmethod
    def from_bytes(cls, data: bytes, *, cache: bool = False) -> "ImageInput": ...
    @property
    def sha256(self) -> str: ...        # hex of data
    @property
    def pixels(self) -> int: ...        # width * height
    def stub(self) -> "ImageStub": ...

class ImageStub(BaseModel):             # frozen, extra="forbid"; content-free reference
    sha256: SHA256_HEX
    media_type: MediaType
    width: Annotated[int, Field(gt=0, strict=True)]
    height: Annotated[int, Field(gt=0, strict=True)]
    attached: Annotated[int, Field(ge=1, strict=True)]   # 1-based position in the task

ImageRef = ImageInput | ImageStub

def sniff(data: bytes) -> tuple[MediaType, int, int]: ...
    # PNG IHDR; JPEG SOF0/1/2 (walk markers); WebP VP8/VP8L/VP8X; GIF logical screen
    # descriptor. Pure struct. Raises ValueError("unsupported_image") otherwise.
def b64(image: ImageInput) -> str: ...
def data_url(image: ImageInput) -> str: ...          # data:<media_type>;base64,<b64>
def encode_png_gray(rows: Sequence[bytes], width: int, height: int) -> bytes: ...
    # stdlib zlib, colour type 0, bit depth 8; used by the bitmap page renderer and tests
```

`ImageInput` enforces the request-level cap; per-model byte and count caps are enforced by `image_reservation` (3.3) so OpenAI/Ollama are not limited by Anthropic's 5 MB.

### 3.2 `architect/models/types.py`

```python
class ModelRequest(BaseModel):          # unchanged fields plus:
    images: tuple[ImageInput, ...] = ()                       # max MAX_IMAGES_PER_REQUEST
    image_detail: Literal["auto", "low", "high"] = "auto"     # OpenAI-shaped request hint only
```

Both fields are defaulted, so every existing constructor (runtime, bootstrap probe, `scripts/arc_harness.py` `ModelRequest(prompt="")`, tests) is unchanged and every existing spec hash is unchanged. `cache_prefix` semantics and `split()` are unchanged. `ModelResponse` and `UsageQuote` are unchanged: providers report image tokens inside `input_tokens` / `prompt_tokens` / `prompt_eval_count`, so measured cost stays honest through `Price.cost`.

### 3.3 `architect/models/pricing.py` and `prices.json`

```python
class ImagePricing(BaseModel):          # frozen, extra="forbid"
    tokens_per_image_max: Annotated[int, Field(gt=0, strict=True)]   # THE reservation, per image
    rule: Literal["fixed", "anthropic_area", "openai_tiles", "openai_patches"] = "fixed"
    area_divisor: int = 750
    tile_base: int = 85
    tile_tokens: int = 170
    patch_multiplier: float = 1.0
    detail: Literal["auto", "low", "high"] = "auto"      # copied into ModelRequest.image_detail by the bench
    max_bytes: Annotated[int, Field(gt=0, strict=True)] = 20 * 1024 * 1024
    max_images: Annotated[int, Field(gt=0, strict=True)] = 20
    basis: Literal["provider_docs", "assumption", "measured"]

class Price(BaseModel):                 # existing fields plus:
    image: ImagePricing | None = None

def image_reservation(price: Price, images: Sequence[ImageInput]) -> int:
    """Reserved input tokens for these images: len(images) * tokens_per_image_max.
    Raises AdapterError("Model is not configured for image input") when price.image is None
    and images is non-empty; AdapterError("Too many images for model") over max_images;
    AdapterError("Image exceeds model byte limit") when any image exceeds max_bytes."""

def image_estimate(price: Price, image: ImageInput, detail: str = "auto") -> int:
    """Provider-formula ESTIMATE for display only; never used for reservation.
    anthropic_area: ceil(w*h/area_divisor); openai_tiles: tile_base + tile_tokens*tiles(w,h)
    (fit within 2048, short side to 768, 512 tiles) scaled by patch_multiplier;
    openai_patches: min(ceil(w/32)*ceil(h/32), 1536)*patch_multiplier; fixed: tokens_per_image_max."""
```

`prices.json` entries (documented in the module docstring with basis):

| model | rule | tokens_per_image_max | max_bytes | max_images | basis |
|---|---|---|---|---|---|
| `fake` | fixed | 100 | 20 MiB | 100 | assumption (so a fake image costs $0.0001; synthetic costs stay nonzero) |
| `claude-sonnet-4-20250514`, `claude-haiku-4-5-20251001`, `claude-sonnet-5`, `claude-opus-5-5`, `claude-fable-5-1` | anthropic_area | 1600 | 5_000_000 | 100 | provider_docs (images over ~1.15 MP are downscaled by the provider, so 1600 is the ceiling) |
| `gpt-4o-mini` | openai_tiles, patch_multiplier 33.33 | 48170 | 20_000_000 | 20 | provider_docs |
| `gpt-5.6-terra` | fixed | 8192 | 20_000_000 | 20 | assumption (calibrated by `bench probe`) |
| `llama3.2` | none (text model) | | | | |
| `llama3.2-vision` (new, $0, `allowed_response_models: ["llama3.2-vision:latest"]`, capacity 131072, overhead 1024) | fixed | 1600 | 20_000_000 | 20 | assumption |

Quotes: `_HTTPAdapter.quote` (`http.py:66`) and `_CLIAdapter.quote` (`cli.py:178`) compute `tokens_in = prompt_bytes + input_overhead_tokens + image_reservation(self._price, request.images)` before `quote_cost`, so on Anthropic and Claude Code image tokens are reserved at the cache-write rate when a prefix may be written. `FakeAdapter.quote` adds `image_reservation` tokens at the fake input rate to the scripted usage, and `complete` returns the scripted response with `tokens_in`/`cost_usd` raised by exactly that amount (the fake "measures" the ceiling so quote == measured stays true). New:

```python
class RequestShape(BaseModel):          # frozen; content-free
    prompt_bytes: int; image_count: int; image_bytes: int; sha256s: tuple[str, ...]
class FakeAdapter:
    seen: list[RequestShape]            # appended by complete()
```

### 3.4 Adapter payloads

Anthropic (`http.py:172`): when `images == ()` the request body is byte-identical to today (plain string content; `tests/test_models.py:137` holds). Otherwise `content` is `[ {type:"text", text:<prefix>, cache_control:{type:"ephemeral"}} (only when cache_prefix) ] + [ {type:"image", source:{type:"base64", media_type, data:<b64>}} (+ cache_control when image.cache) ... ] + [ {type:"text", text:<rest>} ]`. The prefix stays a byte-stable leading text block, so cache reads are unaffected by images.

OpenAI-compatible (`http.py:246`): one user message (`tests/test_prompt_caching.py:167` holds); with images its `content` is `[{type:"text", text:prompt}, {type:"image_url", image_url:{url:data_url(image), detail:request.image_detail}} ...]`.

Ollama (`http.py:299`): text-only keeps `/api/generate` with `raw: true` (`tests/test_models.py:187`). With images it posts `/api/chat` `{model, messages:[{role:"user", content:prompt, images:[<b64 without data: prefix> ...]}], stream:false, options}` and reads `message.content`, `done`, `prompt_eval_count`, `eval_count`. The httpx timeout for an image request is `max(self._timeout, 180.0)`. Documented caveat: `/api/chat` applies the model's chat template, so local text and image runs are not prompt-identical and local cache-prefix reuse is not promised.

Claude Code (`cli.py:299`, WP2): with images, argv is `claude -p --input-format stream-json --output-format stream-json --verbose --tools "" --setting-sources "" --strict-mcp-config --no-session-persistence --disable-slash-commands --model <m> [--system-prompt S]`; stdin is one JSON line `{"type":"user","message":{"role":"user","content":[{"type":"text","text":prompt},{"type":"image","source":{"type":"base64","media_type":...,"data":...}} ...]}}` plus newline; stdout is JSONL and the parser selects the single line with `type == "result"` and applies today's checks (subtype, is_error, modelUsage, usage, total_cost_usd). Text-only argv/stdin are byte-identical to today. `_CLIAdapter._run(self, argv: list[str], stdin: bytes, cwd: Path, env: Mapping[str, str]) -> tuple[int, bytes]` replaces the `prompt: str` parameter. Pure builders for win32-runnable tests: `ClaudeCodeAdapter._argv(self, request) -> list[str]`, `ClaudeCodeAdapter._stdin(self, request) -> bytes`.

Codex (`cli.py:399`, WP2): each image is written to `<root>/image-<n>.<png|jpg|webp|gif>` (next to `instructions.md`, outside the read-only `work` cwd) and `-i <path>` is appended per image before the trailing `-`; usage parsing is unchanged (`turn.completed` input tokens include image tokens). Builder: `CodexAdapter._argv(self, root: Path, request) -> list[str]` (paths only; the caller writes the files).

Until the one-call live probe (Section 10.5) has confirmed each of the three unverified paths (Claude Code stream-json result line, Codex `-i` with stdin `-`, Ollama `/api/chat` images), the adapters are implemented as specified and fail closed with `AdapterError` on any unexpected shape; rows for those providers are labelled as usual because the adapter either produced a validated `ModelResponse` or the cell failed.

## 4. Threading: runtime, sandbox, presets, telemetry, floors (WP2)

### 4.1 Runtime

```python
class RunContext:
    async def call(self, prompt: str, max_output_tokens: int | None = None, *,
                   system: str | None = None, images: Sequence[ImageRef] = ()) -> str: ...
    def attach_images(self, images: Sequence[ImageInput]) -> None: ...
        # registers sha256 -> ImageInput for this run and its descendants; idempotent
    images_sent: int                     # count of ImageInput objects passed to adapters in this lineage
```

`_call` resolves each `ImageStub` against the run's table; an unknown sha raises `AdapterError("Image reference is not attached to this run")` (failure code `image_unresolved`). `ModelRequest(..., images=tuple(resolved), image_detail=<from price.image.detail if configured else "auto">)`; `images_sent += len(resolved)` on every lineage item. `Runtime._execute` writes `image_inputs=context.images_sent` into the `TelemetryRow`. `TelemetryRow.image_inputs: NonnegativeInt = 0` (absent in older rows). `ParameterKey` is unchanged. HTTP adapter default timeout stays 60 s; the bench constructs profile adapters unchanged and the Ollama image path raises its own timeout to 180 s (3.4).

### 4.2 Presets

Task convention: an image literal anywhere in a task JSON is exactly `{"$image": {"media_type": "<MediaType>", "base64": "<...>"}}`. Its stub form is `{"$image": {"media_type", "sha256", "width", "height", "attached": <1-based>}}`. Input schemas that accept a page require only `media_type` under `$image` so both forms validate.

```python
# architect/presets/images.py
def lift_images(task: JsonValue) -> tuple[JsonValue, tuple[ImageRef, ...]]:
    """Depth-first walk; each literal becomes an ImageInput and is replaced by its stub;
    each existing stub becomes an ImageStub. Order = attached index. Bytes never remain."""
def attach_stages(spec: Spec) -> frozenset[str]:
    """spec.execution_config['images']['stages'] if present and a list of step names
    (validated: every name is a pipeline step), else {first step name}; empty set for
    single-agent specs means 'attach on every call'."""
```

`execute` (`_execution.py:247`) calls `lift_images(task)` before `_task_context`, so base64 never reaches the text prompt, the 2048-character `_bounded` policy, the per-byte quote or the pipe. New keyword `execute(..., attach_images: bool = True)`. When images are non-empty and `attach_images`: `prompt_data["attached_images"] = n` and the instruction gains the fixed sentence `" {n} image(s) are attached to this message; extract from them, not from the stub."`; every `context.call` in that execution, including the `verify_candidate` self-check (`_execution.py:339-357`), passes `images=images`. `run_pipeline` (`runtime/pipeline.py:84`) lifts once at entry so `original`, `previous` and `steps` carry stubs only, and calls each step with `attach_images=(step.name in attach_stages(spec))`; later stages are never re-billed for images unless the policy names them. `execution_config` is only size-validated (`spec/__init__.py:113-116`), so the new `images` key changes no existing spec hash and needs no schema change. Extractor tunables are unchanged (`verifier_depth` (0,1) default 1, `retries` (0,1)); no image-specific tunable is added in this phase (provider-specific knobs would inflate the Stage 2 grid on models where they are no-ops).

### 4.3 Sandbox

No AST-guard change: emitted harness source calls `execute_preset` and never `context.call(images=)`; preset code runs as real Python inside the worker against `_RPC` (`_sandbox_worker.py:186-193`) or host-side for pipeline/game/coder via the `preset` op (`sandbox.py:379-401`). Three changes:

1. `sandbox_harness(...).run` lifts images from `task` before `_encode({"source", "binding", "task"})` (`sandbox.py:334`) and calls `context.attach_images(images)`; only stubs cross the pipe, so a page never approaches `MESSAGE_LIMIT` (128 KiB).
2. Host loop `call` op (`sandbox.py:403-416`) accepts `set(message) == {"op","prompt","tokens"} | {"op","prompt","tokens","images"}`; `images` must be a list of at most 100 strings matching `^[0-9a-f]{64}$`, else `SandboxError("sandbox_invalid_call")`; the host builds `ImageStub`s from the run's table (unknown sha -> `sandbox_invalid_call`) and calls `context.call(prompt, max_output_tokens=tokens, images=stubs)`.
3. `_RPC.call(self, prompt, max_output_tokens=None, images: Sequence[ImageRef] = ())` sends `{"op":"call","prompt","tokens","images":[sha...]}` (omits `images` when empty so text-only messages are byte-identical to today); an `ImageInput` inside the worker is a protocol violation (`sandbox_invalid_call`).

`CALL_LIMIT` (64) is unchanged.

### 4.4 Floors: closed checkers

```python
# architect/floors/checkers.py (trusted host code; no model text is executed)
Checker = Callable[[JsonValue, JsonValue], bool]
CheckerName = Literal["equality", "exam_extraction_v1", "invoice_extraction_v1"]
CHECKERS: Mapping[CheckerName, Checker]
def exam_extraction_v1(expected: JsonValue, output: JsonValue) -> bool: ...
def invoice_extraction_v1(expected: JsonValue, output: JsonValue) -> bool: ...
class FieldReport(BaseModel):           # frozen; counts only
    fields: int; matched: int; per_field: dict[str, tuple[int, int]]   # name -> (matched, total)
def field_report(expected: JsonValue, output: JsonValue, *, checker: CheckerName) -> FieldReport: ...
def similar(a: str, b: str) -> float: ...   # difflib ratio on whitespace-collapsed, casefolded text
```

`exam_extraction_v1` passes only when: `output["questions"]` has the same length as expected; for every expected question matched by `number`: `answer_key` equal after upper-casing; `similar(stem) >= 0.9`; choices count equal and each choice with equal `label` has `similar(text) >= 0.9`; `answer_key_page` equal. `invoice_extraction_v1`: `vendor` similarity >= 0.9, `date` equal, `total` equal within 0.005, `line_items` count equal and each item `description` similarity >= 0.9 with `quantity` and `unit_price` equal; every expected `null` field must be `null` in the output. `FloorConfig.checker: CheckerName = "equality"` in `profiles.py:111`; `floor_factory` (`profiles.py:269-270`) passes `HeldOutTasks(definition, records, CHECKERS[config.checker] if config.checker != "equality" else None)`.

## 5. Synthetic exam pages (WP3)

### 5.1 Generator

```python
# architect/bench/pages.py
Style = Literal["clean", "scan", "dense"]
class PageTruth(BaseModel):             # frozen, extra="forbid"
    page: int
    questions: list[Question]            # [] on a key page
    answer_key_page: bool
class Question(BaseModel):
    number: int; stem: str; choices: list[Choice]; answer_key: Literal["A","B","C","D"]
class Choice(BaseModel):
    label: Literal["A","B","C","D"]; text: str
class Page(BaseModel):
    image: bytes; media_type: MediaType; width: int; height: int
    truth: PageTruth; sha256: str; style: Style; index: int
    printed_key_wrong: tuple[int, ...]   # question numbers whose printed key is deliberately wrong

def generate_pages(seed: int, count: int, style: Style = "clean", *, dpi: int = 96,
                   key_page: bool | None = None) -> list[Page]: ...
def write_dataset(directory: Path, pages: Sequence[Page], kind: Literal["vision", "digitizer"],
                  *, dataset_id: str) -> DatasetFiles: ...   # cases.json + manifest.json
def cases_for(pages, kind, *, dataset_id) -> list[EvaluationCase]: ...
```

Default page size is 816 x 1056 (US letter at 96 dpi, 0.86 MP, under Anthropic's ~1.15 MP downscale threshold so Claude scores measure the preset, not provider downscaling). `--dpi` is a suite-version knob, never varied within a suite. Everything derives from `random.Random(seed)`: header ("Unit N Quiz", "Name: ____  Date: ____", "Page n of N"), 8-12 questions (dense: 24-30 in two columns), numbering style ("1.", "1)", "Q1"), four choices A-D with label style ("A.", "(A)", "A)"), stems from closed template families (arithmetic with distractors, unit conversion, "Which word completes the sentence" from a fixed 200-word list in `_font.py`'s sibling `WORDS`, largest/smallest number, two-line passage reading). Answer key: either a footer box ("Answer Key: 1-B 2-D ...") or a trailing key page (`count+1` pages, key page truth `questions: []`, `answer_key_page: true`) chosen by seed unless `key_page` is given. On ~20% of pages one or two printed keys are deliberately wrong (`printed_key_wrong`), mirroring the digitizer's disagreements.

Renderers:

- `clean`: stdlib only. `architect/bench/_font.py` holds `GLYPHS: dict[str, bytes]` (printable ASCII 0x20-0x7E, 8 x 13, one byte per row) scaled 2x, rendered through `encode_png_gray`. Byte-deterministic for a given seed across platforms; its sha256 for seed 7 is pinned in `tests/test_bench_pages.py`. The default test suite and the smoke tier need no Pillow.
- `scan` and `dense`: Pillow via the optional extra `bench = ["pillow>=10.1,<13"]` (Pillow 12.3.0 is already locked through the `arc` extra; `ImageFont.load_default(size=...)` uses Pillow's embedded font, no system fonts). `scan`: rotation +-1.5 degrees, Gaussian noise sigma 6, slight blur, light-grey background, JPEG re-encode at quality 70 then back to PNG. `dense`: 10-11 pt equivalent, two columns, tighter leading. Not byte-portable across Pillow builds; the manifest records `pillow_version` and `platform`.

### 5.2 Ground truth and cases

Vision case: `task = {"synthetic": true, "page": n, "images": [{"$image": {"media_type": "image/png", "base64": "..."}}]}`, `expected = PageTruth` as JSON, `case_id = "<dataset_id>-<index:03d>"`, `metadata = {"synthetic": true, "source": "rendered-synthetic-page", "style", "generator_version": "v1"}`. Digitizer case: same task; `expected = {"answer_keys": [{"id": "q<number>", "answer": "<A-D>"}], "disagreements": ["q<number>" ...], "review_required": true}` (the shape of `examples/lectern/fixtures/answer-keys.json`). Because the base64 is inside `task`, `_fingerprint` (`floors/base.py:93`, the full JSON dump) binds the exact bytes into partition membership, so Stage 4 authority covers the pixels; the partition digest sets therefore hold whole pages in memory (about 40 x 30 KB for `clean`, up to 40 x 150 KB for `scan`), which is acceptable and documented.

`manifest.json`: `{seed, count, style, dpi, generator_version, pillow_version|null, platform, pages: [{index, sha256, questions, answer_key_page}]}`. PNGs are never committed; `examples/bench/suite-v1/pages-manifest.json` holds seeds and the `clean` sha256s only. `write_dataset` never writes PNG files unless `architect bench pages --png` asks for them (operator inspection).

### 5.3 Oracle adapters (fake provider)

```python
# architect/bench/oracle.py
class OracleState:                      # built in-process by the suite for one bench run
    vision: dict[str, JsonValue]        # sha256(page bytes) -> extraction output
    text: dict[str, JsonValue]          # sha256(canonical JSON of task 'input') -> answer
    digitizer: dict[str, JsonValue]     # sha256(page bytes) -> stage-1 output; stage 2/3 derived
    floors: list[FloorPartition]        # lookup keys per floor partition; no answers
    def add_floor(self, cases: Sequence[tuple[str, str]], limit: float) -> None: ...  # (case_id, lookup_key)
def lookup_key(task: JsonValue) -> str: ...  # sha256 of the first image's bytes, else canonical_key(task)
def held_out_flips(n: int, rate: float, limit: float) -> int: ...  # max(ceil(rate*n), misses_allowed+1), <= n
def tuning_flips(n: int, rate: float) -> int: ...                  # max(ceil(rate*n), 6), <= n
class OracleAdapter(FakeAdapter):
    def __init__(self, model: str, state: OracleState, *, error_rate: float = 0.0, seed: int = 0,
                 prices_path: Path | None = None) -> None: ...
def oracle_factory(state: OracleState, *, error_rate: float = 0.0, seed: int = 0) -> Callable[[str], ModelAdapter]: ...
```

The oracle never reads floor data. For a request with images it looks up `sha256(request.images[0].data)`; for text it parses the JSON prompt (presets embed `{"instruction", "task_description", "input", "output_schema"}`, `_execution.py:280-284`) and looks up the canonical `input`; `verify_candidate` operations return a passing verdict; pipeline solver/verifier stages compute the answer from the stage-1 output they receive; unknown hashes return a schema-valid wrong answer so a broken image path or a truncated payload fails the floor loudly. `error_rate > 0` (the `NoisyOracle` variant, default 0.15 in the suite's `noisy` variant) is a mechanism check with a guaranteed, known effect size, not a realistic model of a weaker model. `BenchTask.bundle()` (and `delivery_dataset()`) registers each floor with `OracleState.add_floor` from `(case_id, lookup_key(task))` pairs only (no expected value, no held-out capability); the oracle splits them by the floor's public rule (sorted `case_id`, even index tuning, odd held-out, `architect/floors/base.py`) and flips a deterministic quota per partition, choosing cases by `sha256(f"{seed}:{key}")` order: held-out `max(ceil(rate * n), misses_allowed + 1)` capped at `n`, where `misses_allowed` is the most misses `score >= limit` tolerates, so the held-out score is strictly below the floor limit and every noisy cell is unlicensed with `primary_floor_failed`; tuning `max(ceil(rate * n), 6)` capped at `n`, so `default` vs `noisy` has at least 6 one-directional discordant pairs (`mcnemar_exact(6, 0) = 0.03125`) and compares as `regression`. A flip depends on the lookup key only, so retries, self-checks and candidates see the same answer. Pipeline solver/verifier stages never flip on their own; a stage-1 flip toggles the first question's printed-key agreement with the derived answer, so the final disagreement list always changes. Delivery sets are registered by the same rule; keys outside every registered floor (ad hoc tables) keep a per-key Bernoulli draw at `error_rate`. Usage: 80 in / 40 out text tokens plus the fake image reservation per image (nonzero, dollar-bound). The floor factory receives a separate deep copy of the cases; mutating an `expected` never changes the oracle's answer (adversarial test).

## 6. Text tasks (WP3)

```python
# architect/bench/text_tasks.py
def generate_tickets(seed: int, n: int) -> tuple[list[EvaluationCase], dict[str, JsonValue]]: ...
def generate_invoices(seed: int, n: int) -> tuple[list[EvaluationCase], dict[str, JsonValue]]: ...
```

`tickets`: support tickets from templates with labels `billing|bug|feature|other`; 25% deliberately ambiguous (two cues) with the label fixed by template so the oracle table is exact. `invoices`: messy plain-text invoices (vendor, date, line items with quantities and unit prices, totals) with 30% of cases missing one field that must stay `null`. Manifests (`examples/bench/suite-v1/tickets-manifest.json`, `invoice-manifest.json`) hold seed, n, and sha256 of the canonical cases JSON, never the cases. `sentiment-24` is `examples/live/sentiment-cases.json` as is; its oracle table is built from the file's expected labels keyed by input hash.

## 7. Suite v1 (WP3)

```python
# architect/bench/suite.py
Tier = Literal["smoke", "quick", "full", "arc"]
Kind = Literal["text", "vision", "pipeline", "game"]
TaskId = Literal["sentiment-24", "tickets-40", "invoice-40", "exam-clean-40", "exam-scan-40",
                 "digitizer-24", "digitizer-multipage-12", "arc-local-3", "arc-preview"]
Variant = Literal["default", "noisy", "images-all-stages"]
class BenchTask(BaseModel):             # frozen
    id: TaskId; kind: Kind; tiers: frozenset[Tier]; sizes: dict[Tier, int]
    floor_limit: float; checker: CheckerName; preset: PresetName; discriminates: str
    requires: frozenset[Literal["pillow", "arc"]]
    def spec(self, model: str, variant: Variant, *, route: Mapping[str, str] = {}) -> Spec: ...
    def dataset(self, seed: int, size: int) -> tuple[list[EvaluationCase], OracleState]: ...
    def delivery(self, seed: int, n: int) -> list[EvaluationCase]: ...   # seed+1000, disjoint
    def sizing_request(self, images: Sequence[ImageInput]) -> ModelRequest: ...
    def max_calls_per_run(self, variant: Variant) -> int: ...
SUITES: Mapping[str, tuple[BenchTask, ...]]      # {"v1": ...}; frozen
TIERS: Mapping[Tier, tuple[TaskId, ...]]
def suite_digest(version: str) -> str: ...      # sha256 of task ids, kinds, sizes, floors, checkers, generator versions
```

Partitions follow `FloorEvaluator` (sorted `case_id`, even -> tuning, odd -> held-out); `n` cases give `n/2` held-out. `minimum_samples` in each suite spec floor is `min(30, n//2)` is not allowed by `FloorDefinition` semantics (`base.py:127` requires both partitions >= minimum), so every suite spec sets `minimum_samples = sizes[tier] // 2` explicitly.

| # | task | kind | preset / spec | floor | sizes smoke/quick/full | discriminates |
|---|---|---|---|---|---|---|
| 1 | `sentiment-24` | text | extractor, output `{label}` | 0.90 equality | 24/24/24 | instruction following and strict JSON; the anchor with live evidence (12/12 Haiku, ~$0.0017/run) |
| 2 | `tickets-40` | text | extractor, 4 labels | 0.85 equality | 24/-/40 | terseness/self-check trade-offs on ambiguous inputs |
| 3 | `invoice-40` | text | extractor, invoice schema | 0.90 invoice_extraction_v1 | 24/40/40 | "do not invent missing values", retries; text-only twin of the vision floor |
| 4 | `exam-clean-40` | vision | extractor, one `clean` page per case, `attached_images` | 0.90 exam_extraction_v1 | 24/40/40 | vision extraction and whether verifier_depth 1 (doubles image cost) buys accuracy; the Reviewer-A-shaped floor; no Pillow needed |
| 5 | `exam-scan-40` | vision | extractor, `scan`+`dense` pages | 0.70 exam_extraction_v1 | 16/-/40 | robustness to skew, noise, two columns; requires pillow |
| 6 | `digitizer-24` | pipeline | vision-extract -> solver-rederive -> verifier-diff (the `examples/lectern/specs/test-digitizer.json` shape with `$image` input; `execution_config.images.stages=["vision-extract"]`) | 0.90 equality | 12/-/24 | pipeline routing (`--route`), only first stage tuned (`pipeline.py:34`), image re-send cost (variant `images-all-stages`) |
| 7 | `digitizer-multipage-12` | pipeline | same pipeline, 3 pages per case (test + key page), `original` carries three stubs | 0.75 equality | -/-/12 | cross-page numbering, multi-image context; full only |
| 8 | `arc-local-3` | game | `GameFactory` over `LocalGameProvider` families (`games/local.py` `FAMILIES`), strategies explorer/planner/hypothesis_tester | 1.0 local score | 3/-/3 | game presets and prompt-size limits; not an ARC result |
| 9 | `arc-preview` | game | `games/public.manufacture` on cached OFFLINE public games; `--arc-games` default 5, `--arc-actions` default 24 | 0 | arc only | action efficiency; `model_binding=online_preview`, `compliance=unverified`; requires arc extra and cache |

Tiers: `smoke` = tasks 1-6 and 8 at smoke sizes, fake provider, `max_candidates 2`; `quick` = tasks 1, 3, 4; `full` = tasks 1-8; `arc` = task 9. Variants: `default`; `noisy` (fake only, oracle error_rate 0.15; smoke runs tasks 1 and 4 in `noisy` as CI's negative control); `images-all-stages` (task 6 only). `--size N` overrides the size of every non-game task in the tier (fake and pages scale for free; live cost scales linearly). Every default is recorded in the row.

Call and cost projections (price-table projections until measured rows replace them; per-call live numbers from `examples/telemetry/session-live.jsonl`: Haiku ~540 in / 250 out ~$0.0018 per text call). Extractor grid is 4 configurations; `max_candidates 2` caps Stage 2 at 2 x tuning cases x <=4 calls; Stage 4 adds held-out x <=4 calls; typical 2 calls per case. Quick tier per model: ~90-240 calls per task. Haiku 4.5 ~$0.25 (sentiment) + $0.35 (invoice) + $0.6 (exam-clean, +1600 image tokens per call) ~ $1.2; Sonnet 5 ~ $2.5; Opus 5.5 ~ $5; Fable 5.1 ~ $12; gpt-5.6-terra via Codex ~ $1.5 (price table only; Codex reports no cost); gpt-4o-mini API ~$0.08 measured; Ollama $0. Full tier roughly 3x quick. `arc-preview` ~$1.5 per 5-game set on Haiku at 8 actions (`examples/telemetry/arc-live-evidence.json`); 24 actions is bounded by `--max-usd`. Per-call reservation for CLI adapters is the full output capacity: Haiku 4.5 $0.34 (64k out + 16k in at cache-write rate), Sonnet 5 ~$1.32, Opus 5.5 ~$2.64, Fable 5.1 ~$6.6, gpt-5.6-terra ~$1.57; the bench sizes budgets from the quote, prints the bound and refuses cells above `--max-usd` before spending.

Statistical power (stated in `docs/bench.md`): 20 held-out cases give a 95% Wilson interval of about +-0.15 around 0.85; held-out deltas are the licensing signal, paired tuning bits the comparison signal, and tuning bits are cases Stage 2 selected on (selection bias, stated plainly); for small effects raise `--size 80` or `--repeats 3`, never a judgment call.

## 8. Bench runner (WP4)

### 8.1 Configuration and result

```python
# architect/bench/runner.py
class BenchConfig(BaseModel):           # frozen, extra="forbid"
    suite: str = "v1"; tier: Tier; provider: Literal["fake", "profile"] = "profile"
    profile: Path | None = None; models: tuple[str, ...] = (); presets: tuple[PresetName, ...] = ()
    variants: tuple[Variant, ...] = ("default",); route: dict[str, str] = {}
    max_usd: float | None = None; seed: int = 7; repeats: int = 1; size: int | None = None
    max_candidates: int = 2; arc_games: int = 5; arc_actions: int = 24; arc_cache: Path | None = None
    delivery_n: int = 0; label: str = ""; out: Path; timeout_s: float = 3600.0
class BenchResult(BaseModel):
    bench_id: str; rows: tuple[BenchRow, ...]; summary: Summary; bound_usd: float; cancelled: bool
    skipped: tuple[tuple[TaskId, str, str], ...]   # (task, model, code)
async def run_bench(config: BenchConfig, *, factory: Callable[[str], ModelAdapter],
                    prices: Prices, on_progress: Callable[[BenchProgress], None] | None = None,
                    clock: Callable[[], float] = time.monotonic) -> BenchResult: ...
def bound(config: BenchConfig, *, factory, prices) -> Bound: ...   # per-cell reservation, total, calls; no spend
class BenchProgress(BaseModel):
    done: int; total: int; task_id: TaskId; model: str; stage: int | None; calls: int; cost_usd: float; elapsed_s: float
```

### 8.2 Algorithm per cell (repeat x task x model x variant)

1. `spec = task.spec(model, variant, route=config.route)`: `model_target=model`, every pipeline step `model` substituted by role (`vision`, `solver`, `verifier` -> the bench model unless `--route` maps the role), `lifecycle=expire`, `floor.dataset = f"bench-{task.id}-{suite}-{seed}"`, `floor.minimum_samples = size // 2`.
2. Budget lifting, as `session._lift_budget` does: `per_call = adapter.quote(task.sizing_request(images))`; `spec.budget.cost_usd = per_call.cost_usd * task.max_calls_per_run(variant)` (extractor: `(1+retries_max) * (1+verifier_depth_max) = 4`; pipeline: sum over steps); `spec.budget.compute_s = 120 * max_calls`; `compile_budget = Budget(cost_usd = spec.budget.cost_usd * (max_candidates * tuning + held_out + delivery_n) * 1.2, compute_s = config.timeout_s)`. The cell bound is `compile_budget.cost_usd`; `bound()` sums it across cells before any spend, and a cell over `--max-usd` is skipped with code `over_max_usd` (whole run refused when the total exceeds `--max-usd`).
3. A fresh short-prefix `tempfile.TemporaryDirectory(prefix="arch")` workspace (Windows registry path rule, as `scripts/arc_harness.py:436`) with its own `ARCHITECT_SIGNING_KEY_FILE`, `TelemetryStore`, `Runtime(store, adapter_factory=metered)`, `Compiler(root, runtime, metered, floor_factory, config=CompileConfig(strategy="grid", model_ladder=(model,), allowed_presets=(task.preset,), max_candidates=config.max_candidates, enable_rewriting=False, max_rewrites=0, compile_budget=...))`. `metered` wraps the factory with `session.observe.ObservedAdapter`/`Meter` for calls, tokens, cost and wall. `floor_factory` returns `HeldOutTasks(definition, deepcopy(cases), CHECKERS[task.checker])` for the bench dataset id only.
4. `result = await compiler.compile(spec, approved=True)` (Stages 1, 2, 4; no Stage 0 because suite specs are explicit; Stage 3 off so rows measure presets, not rewrites). `licensed = artifact.licensed` re-read from disk via `HarnessArtifact` load; `held_out_score`, `held_out_count`, `mean_cost_usd`, `failures`, `stages` from `result.report` / `result` (`spec/__init__.py:127-148`, `compiler/types.py:69-76`).
5. Tuning replay for paired statistics: when an artifact exists, each `floor.tuning` case is run through `Runtime.evaluate(spec, sandbox_harness(artifact), case.task, adapter, preset=..., components=..., params=...)` and scored one at a time with `floor.score([case], [output])`, giving one bit per case in sorted `case_id` order (`tuning_pass_bits`, hex, MSB first) and `field_report` counts summed into `field_accuracy` for extraction tasks. Held-out cases are never touched outside Stage 4 (a `HeldOutAccessError` guard test asserts it).
6. Delivery replay (optional, `delivery_n > 0`): `task.delivery(seed, n)` cases (disjoint generator seed) through the same `Runtime.evaluate` path; `delivery_score` is the pass fraction. It is a second out-of-sample signal, not a license and not a `Runtime.run`.
7. One `BenchRow` per cell; `rows.jsonl` appended after every cell (partial rows survive cancel); `summary.json` and the workspace deleted through `close_workspace` (stores and official workers closed before removal).

Game cells (`kind == "game"`) skip 3-6: `arc-local-3` builds a `GameFactory` over `LocalGameProvider` with `GameFactoryConfig(compile_config=CompileConfig(model_ladder=(model,), enable_rewriting=False, max_rewrites=0, max_candidates=...), small_model=model, limits=GameLimits(max_actions=arc_actions, max_turns=arc_actions), strategy=...)`; `arc-preview` calls `public.manufacture(PreviewOptions(games=arc_games, seed=seed, cache=arc_cache, offline=True, adapter=..., model=model, max_actions=arc_actions, cost_usd=..., compute_s=...), tmp)` and copies `score`, `score_source`, `model_binding`, `licensed`, child failure codes into the row. Missing arc extra or cache -> cell code `arc_unavailable`; a model without `Price.image` on a vision cell -> `image_not_configured`; missing Pillow on a `scan` cell -> `extra_missing`.

### 8.3 Rows

```python
# architect/bench/rows.py
Evidence = Literal["synthetic_fake", "live_api_equivalent", "live_billed", "local_zero_cost"]
Provider = Literal["fake", "anthropic", "openai", "ollama", "claude-code", "codex"]
ModelId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")]   # a profile key, never content
Code = Annotated[str, Field(pattern=r"^[a-z_]{1,64}$")]
class BenchRow(BaseModel):              # frozen, extra="forbid", allow_inf_nan=False
    bench_id: str (canonical UUID); timestamp: datetime; label: Annotated[str, Field(max_length=64, pattern=r"^[A-Za-z0-9._-]*$")]
    suite_version: SuiteVersion; suite_digest: SHA256_HEX; task_id: TaskId; tier: Tier; kind: Kind
    provider: Provider; model: ModelId; preset: PresetName; variant: Variant
    params: dict[ParameterKey, ParameterValue]; components: tuple[Category, ...]
    max_candidates: int; seed: int; size: int; repeat: int
    spec_hash: SHA256_HEX; preset_fingerprint: SHA256_HEX; prices_sha256: SHA256_HEX; architect_commit: str | None
    pages_manifest_sha256: SHA256_HEX | None      # vision/pipeline cells: sha256 of manifest.json (seed, style, pillow_version, platform)
    licensed: bool; held_out_score: Score | None; held_out_count: NonnegativeInt; floor_limit: Score
    tuning_score: Score | None; tuning_pass_bits: str | None (hex); tuning_cases: NonnegativeInt
    field_accuracy: Score | None; delivery_score: Score | None; delivery_cases: NonnegativeInt
    stages: tuple[int, ...]; failures: tuple[Code, ...]
    calls: NonnegativeInt; tokens_in: NonnegativeInt; tokens_out: NonnegativeInt; cached_tokens_in: NonnegativeInt
    cost_usd: NonnegativeFloat; reserved_usd: NonnegativeFloat; per_call_reservation_usd: NonnegativeFloat
    wall_s: NonnegativeFloat; image_inputs: NonnegativeInt; image_tokens_estimate: NonnegativeInt
    evidence: Evidence; floor_origin: Literal["synthetic_generated", "example_fixture"]
    arc: ArcFields | None            # {score: float|None, score_source: Code, model_binding: Code, games: int, actions: int, compliance: "unverified"}
def write_rows(path: Path, rows: Sequence[BenchRow]) -> None: ...     # JSONL, one row per line, sorted keys
def read_rows(path: Path) -> list[BenchRow]: ...                       # raises BenchError("bench_rows_unreadable")
def preset_fingerprint() -> str: ...    # sha256 over sorted architect/presets/*.py bytes
def prices_sha256(path: Path | None) -> str: ...
def architect_commit() -> str | None: ...   # git HEAD when available, else None
class Summary(BaseModel): per_cell: list[CellSummary]  # (task_id, model, variant): mean/min/max held_out, licensed count, mean cost, mean wall, repeats
```

No prompts, outputs, case ids, page text, media types or hashes of content ever enter a row; `tests/adversarial/test_phase8_bench.py` plants a sentinel string in a task and a case id and asserts it appears nowhere in `rows.jsonl` or `summary.json`.

`evidence` is derived from the provider: fake -> `synthetic_fake`; ollama -> `local_zero_cost`; anthropic/openai -> `live_billed`; claude-code/codex -> `live_api_equivalent`. `floor_origin` is `example_fixture` for `sentiment-24` and `synthetic_generated` otherwise.

## 9. Compare (WP3)

```python
# architect/bench/compare.py
Verdict = Literal["regression", "improvement", "inconclusive", "identical"]
def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]: ...
def mcnemar_exact(b: int, c: int) -> float: ...          # two-sided exact binomial on discordant pairs
def align(a: Sequence[BenchRow], b: Sequence[BenchRow]) -> list[tuple[BenchRow, BenchRow]]: ...
    # key (task_id, model, variant, seed, repeat, size); raises BenchError("bench_compare_mismatch")
    # on different suite_version, suite_digest, tuning_cases, or pages_manifest_sha256 for fake rows;
    # raises BenchError("bench_compare_fingerprint") when preset_fingerprint differs unless allow_fingerprint_mismatch
class CellComparison(BaseModel):
    task_id; model; variant; seed; repeat; n_held_out; base_k; cand_k; held_out_delta: float | None
    wilson_base: tuple[float, float]; wilson_cand: tuple[float, float]; licensed_base: bool; licensed_cand: bool
    discordant_b: int; discordant_c: int; mcnemar_p: float | None; cost_delta_pct: float | None
    wall_delta_pct: float | None; delivery_delta: float | None; verdict: Verdict
    # percent deltas are >= -100 or +inf ("was zero, now positive"; JSON "Infinity"); NaN refused
def pct_text(value: float | None, *, new: str = "new cost") -> str: ...  # table text; +inf -> "new cost"
class Comparison(BaseModel):
    base_id; candidate_id; suite_version: SuiteVersion; suite_digest; cells: list[CellComparison]
    per_model: dict[ModelId, Verdict]; verdict: Verdict; evidence: tuple[Evidence, ...]
def compare(a: Sequence[BenchRow], b: Sequence[BenchRow], *, allow_fingerprint_mismatch: bool = False,
            expect: Literal["identical-scores"] | None = None) -> Comparison: ...
def verdict(cell: CellComparison) -> Verdict: ...
def overall(cells: Sequence[CellComparison]) -> tuple[dict[ModelId, Verdict], Verdict]: ...
```

Cell rule (`verdict`), in order:

- `regression` if `licensed` flips true -> false; or held-out delta <= -0.05 with `mcnemar_p < 0.05` and `b + c >= 5`; or mean `cost_usd` per run rises > 20% without a held-out gain >= +0.05 (a zero-cost base that becomes positive is a +inf rise, rendered "new cost" in tables).
- `improvement` if `mcnemar_p < 0.05`, `b + c >= 5` and the candidate wins the discordant pairs; or held-out delta >= +0.10 with `n_held_out >= 20`; or scores equal (held-out and tuning bits) with cost or wall reduced >= 15%.
- `identical` if held-out, tuning bits, licensed and cost (within 1e-9) are equal.
- otherwise `inconclusive` (fewer than 5 discordant pairs always lands here or in `regression` by licensed flip).

Overall rule (`overall`): per model, `regression` if any cell of that model is `regression`; else `improvement` if any cell is `improvement` and none is `regression`; else `identical` if all identical; else `inconclusive`. The run verdict is `regression` if ANY model is `regression` (model-agnosticism is a hard requirement: a change that helps one model and hurts another is rejected), else `improvement` only if at least one model is `improvement` and every other model is `identical` or `inconclusive`, else `identical` if all identical, else `inconclusive`. `expect="identical-scores"` makes `compare` raise `BenchError("bench_expectation_failed")` when any cell's held-out score, tuning bits or licensed differ (CI regression gate on the deterministic fake oracle).

## 10. CLI surface (WP4)

```
architect bench run --tier smoke|quick|full|arc [--suite v1] [--provider fake] [--profile P]
    [--models a,b] [--presets extractor,...] [--variant default|noisy|images-all-stages ...]
    [--route vision=M,solver=M,verifier=M] [--max-usd X] [--seed N] [--repeats R] [--size N]
    [--max-candidates K] [--arc-games N] [--arc-actions N] [--arc-cache DIR] [--delivery N]
    [--label TEXT] [--out DIR] [--json] [--yes]
architect bench compare <base> <candidate> [--json] [--expect identical-scores] [--allow-fingerprint-mismatch]
architect bench list [--out DIR] [--json]            # suites/tasks/tiers with sizes, floors, expected calls and price-table cost per configured model; and recorded bench ids under DIR
architect bench pages --seed N --count K [--style clean|scan|dense] [--dpi 96] [--kind vision|digitizer] --out DIR [--png]
architect bench probe --profile P [--models a,b] [--json]
```

`<base>` / `<candidate>` are bench ids resolved under `--out` (default `.architect/bench`) or paths to `rows.jsonl`. `--provider fake` needs no profile; otherwise `--profile` is required (`bench_profile_required`). Exit codes: 0 success (a `regression` verdict is still 0; the verdict is data); 1 with `{"error": "<code>"}` on stderr for every failure below; 2 argparse; 130 interrupted (rows so far are kept and labelled `cancelled`). Output: `run` prints the bound line, progress lines, then `render.bench_table`; `--json` prints `{"bench_id", "rows_path", "summary", "bound_usd", "skipped"}`. `compare` prints `render.compare_table` or the `Comparison` JSON. `probe` prints `render.probe_table` or `[ProbeRow]`.

Closed code table (`architect/bench/__init__.py` `CODES`, mirrored into `session/codes.py`):

`bench_suite_unknown`, `bench_tier_unknown`, `bench_variant_unknown`, `bench_profile_required`, `bench_invalid_profile`, `bench_model_unconfigured`, `bench_no_models`, `bench_route_invalid`, `bench_over_max_usd`, `bench_extra_missing`, `bench_arc_unavailable`, `bench_out_unwritable`, `bench_rows_unreadable`, `bench_compare_mismatch`, `bench_compare_fingerprint`, `bench_expectation_failed`, `bench_cancelled`, `bench_provider_failed`, `bench_workspace_error`.

Cell codes (in `BenchRow.failures`, alongside compiler codes such as `primary_floor_failed`, `budget_exceeded`): `image_not_configured`, `arc_unavailable`, `over_max_usd`, `extra_missing`, `cancelled`, `image_reservation_under_configured`, `image_unresolved`, `workspace_error`.

Bound line (fixed): `bench <tier> on <n> models: <=$<bound> API-equivalent bound, ~<calls> calls, not a bill.` followed on an interactive console without `--yes` by `Continue? [y/N] ` for any provider other than fake.

### 10.5 Probe

```python
# architect/bench/providers.py
def build_factory(config: BenchConfig, profile: Profile | None, state: OracleState | None) -> Callable[[str], ModelAdapter]: ...
def evidence_label(provider: Provider) -> Evidence: ...
class ProbeRow(BaseModel):
    model; provider; image_configured: bool; basis: str | None
    text_reserved_tokens: int; text_measured_tokens: int | None
    image_reserved_tokens: int | None; image_estimate_tokens: int | None; image_measured_tokens: int | None
    reserved_usd: float; measured_usd: float | None; code: Code | None   # image_reservation_under_configured, image_not_configured, provider_*
async def probe_models(factory, prices: Prices, models: Sequence[str], *, page: ImageInput) -> list[ProbeRow]: ...
```

One text call ("Reply with the single word ok.", `max_output_tokens=16`) and one image call ("What is the page number printed on this page? Reply with the number only.", a generated `clean` page, `max_output_tokens=16`) per model through the real `Runtime.evaluate` with a control Spec so every call is metered and wallet-checked. Measured > reserved is a `BudgetExceeded` from the runtime and is reported as `image_reservation_under_configured`; `basis == "assumption"` entries are flagged. This is the calibration path for `gpt-5.6-terra`, `llama3.2-vision` and the three unverified provider paths; ceilings are only ever lowered to `max(measured) * 1.25` with the measured row cited in the pricing docstring.

## 11. Session surface (WP4)

- `State.BENCHING = "benching"` added to `State` (`session/state.py`). `_BUSY_STATES` gains it; `VOCABULARY[State.BENCHING] = frozenset({"cancel"})`; `VOCABULARY[State.READY]` gains `"bench"`; `REGISTRY_READ` is unchanged. `profile` is withheld while benching like the other busy states.
- Syntax lines (`_SYNTAX`): `bench smoke | bench quick [model,model] | bench full [model,model] | bench arc [model] | bench compare <id> <id> | bench list` with effect text `run the benchmark suite on the fake provider (smoke, free) or on profile models (paid, confirmed first)`; `cancel` in BENCHING: `stop the bench; rows so far are kept`.
- Handler `architect/session/bench.py`: `async def handle_bench(session, cmd) -> None`. `bench smoke` uses the fake provider and never prompts. Paid tiers print the bound line (10) and read `Continue? [y/N] ` on an interactive console (a `ScriptedConsole` must supply the `y` line; anything else -> `bench_cancelled` before any spend). The run is driven by `_await_work` with a `ProgressPolicy(every_calls=5, every_s=20.0)`; progress lines are `[mm:ss] bench <done>/<total> <task> <model>: stage <n>, <calls> calls, $<cost>` through `render.clock`/`render.money`; `cancel` cancels the task, keeps partial rows labelled `cancelled`, returns to READY. Completion prints `render.bench_table(rows)` and `rows written to <workspace>/bench/<bench_id>/rows.jsonl`. `bench compare <id> <id>` and `bench list` are READY-state reads (no state change) printing `compare_table` / `list_table`.
- Codes added to `session/codes.py` `CODES`: every `bench_*` code from Section 10 plus `busy_benching` ("A bench is running; wait for it, type status, or type cancel."). `explain`/`hint_for` gain one line per code; `bench_over_max_usd` hint: `raise --max-usd or drop models with: bench quick <model>`.
- `status_screen` shows `benching <done>/<total>` and the current cell while in BENCHING; `footer` spend includes bench spend.
- Tables (`architect/bench/render.py`, imported by both CLI and session): `bench_table(rows) -> str` columns `task | model | var | lic | held-out (k/n) | tuning | cost $ | calls | wall | evidence`; `compare_table(cmp) -> str` columns `task | model | held-out base->cand [wilson] | b/c p | cost % | verdict`; both end with the line `money is API-equivalent, not a bill; scores are <evidence label(s)>`.

## 12. ARC (WP4)

`architect/games/public.py` lifts `FixtureAgent`, `fetch_public_games(cache, *, offline) -> tuple[list[Path], dict[str, object]]`, `stage(games, count, seed, staged) -> dict[str, tuple[GameRef, ...]]`, `build(...)`, `manufacture(options: PreviewOptions, tmp: Path) -> dict[str, object]` and `close_workspace` out of `scripts/arc_harness.py` with the summary dictionary keys unchanged (`licensed`, `score`, `score_source`, `model_binding`, `compliance`, `budget_note`, ...). `PreviewOptions` is a frozen model with exactly the script's argument names (`games, seed, cache, adapter, model, floor_limit, cost_usd, compute_s, max_actions, offline`). The script keeps its argparse surface and prints the same JSON; `tests/test_arc_harness_script.py` gains an `--offline` run against a cached fixture asserting the summary keys are byte-for-byte those of today. The CI `arc-extra` job command is unchanged. `state_tracker`, rendered-frame images and other new game strategies are deferred to a later suite version (they must be measured by the bench, not shipped inside it).

## 13. CI (`.github/workflows/ci.yml`)

New job `bench-smoke` (ubuntu, 25 minutes): `uv sync --frozen --extra bench`; `uv run architect bench run --provider fake --tier smoke --seed 7 --variant default --variant noisy --out .architect/bench --yes --json > smoke.json`; `uv run architect bench compare examples/bench/suite-v1/smoke-baseline.jsonl .architect/bench/<id>/rows.jsonl --expect identical-scores`; and `uv run python -m architect.bench.ci_assert .architect/bench/<id>/rows.jsonl` (a small module in `architect/bench/ci_assert.py`, WP4) asserting: every `default` cell licensed; every `noisy` cell unlicensed with `primary_floor_failed`; `image_inputs > 0` on every vision and pipeline cell and `image_inputs` on `digitizer-24` equals exactly one image per case call (first stage only); no `image_reservation_under_configured`, `sandbox_*`, `schema_error` or `image_unresolved` failure; `bench compare` of the `default` rows against the `noisy` rows yields `regression` with `mcnemar_p < 0.05` on `exam-clean-40`. Both noisy assertions hold by construction, not by luck of the seed: the noisy oracle flips a fixed quota per floor partition (Section 5.3), at least `misses_allowed + 1` held-out cases and at least 6 tuning cases, for every seed and every smoke task. The `test-windows` job adds the same smoke run without the `compare` step's `pages_manifest` check being affected (the `clean` renderer is byte-deterministic; `scan` cells on Windows must produce identical scores because the oracle is in-process). Live tiers are manual (`ARCHITECT_LIVE_TESTS=1`, `ARCHITECT_LIVE_CLAUDE_CODE=1`, `ARCHITECT_LIVE_CODEX=1`). Smoke time budget: under 10 minutes on the Linux runner (real Compiler through sandbox subprocesses for 7 tasks); if measured above that, `smoke` sizes are reduced in a suite version bump, never by skipping tasks silently.

## 14. Iteration protocol

1. Baseline. `architect bench run --provider fake --tier smoke --seed 7` (free) then `architect bench run --tier quick --profile examples/live/claude-code.json --seed 7 --label base-<date>`; copy `rows.jsonl` to `examples/bench/results/<bench_id>.jsonl` and commit. `preset_fingerprint`, `prices_sha256`, `suite_digest`, `architect_commit` in every row identify what was measured.
2. Propose. A preset change (instruction text, a `ParameterRange` value, a component, an attach policy, a prompt-caching choice) on a branch. Anything under `architect/presets/*` changes `preset_fingerprint`, so an unlabelled comparison is refused. A change that needs a new suite task, size, floor or generator changes `suite_digest` and gets a fresh baseline under a new suite version.
3. Mechanism gate (free). `bench smoke` on the fake provider must pass every Section 13 assertion. A change that fails here is not measured live.
4. Measure (paid; same seed, models, `max_candidates`, size; `--repeats 3` when a repeat costs under $3): `architect bench run --tier quick ... --label cand-<name>`.
5. Decide with `architect bench compare <base> <cand>`; the verdict is code (Section 9): `regression` on any model rejects; `improvement` requires a significant paired win or a held-out gain of >= 0.10 at n >= 20 with no regression anywhere; `inconclusive` means run `--size 80` or `--repeats 3`, never a judgment call. Before a live `improvement` is accepted, one confirmation run with `--seed 11` (`--holdback`) must not be `regression`.
6. Record. Append to `docs/bench/decisions.md`: date, change summary, base and candidate bench ids, suite version and digest, models, per-cell deltas with Wilson intervals, discordant counts and p-values, per-model verdicts, overall verdict, evidence labels. Commit candidate rows next to the baseline. An accepted change becomes the new baseline for its models.
7. Cadence. `bench smoke` on every push (CI); `bench quick` before a preset change merges and after any `prices.json` or adapter change (a pricing-only change must show `identical` scores with cost deltas only); `bench full` monthly or before a release note quoting a number; `bench arc` when the ARC harness or game presets change; `bench probe` before the first live run on any newly configured model.

## 15. Tests per package

All offline (stdlib + pydantic, pytest-asyncio), runnable on Windows except POSIX subprocess CLI tests, which stay skipped on win32 as today (`tests/test_models_cli.py:18`).

WP1: `tests/test_images.py`: `sniff`/`from_bytes` on hand-built PNG/JPEG/WebP/GIF headers from `tests/image_support.py`; header/declaration mismatch rejected; `ModelRequest` count cap and `extra=forbid`; `image_reservation` = count x ceiling for every rule; `image_estimate` numbers (816x1056 -> anthropic 1149, openai_tiles gpt-4o-mini 4 tiles -> 765 x 33.33 = 25497; 200x200 -> 54); `AdapterError` for a model without `image`; per-model byte/count caps. `tests/test_models.py`: `MockTransport` handlers asserting the Anthropic block order (cached prefix, image, rest) and that text-only bodies are byte-identical to today; OpenAI parts with data URL and detail; Ollama `/api/chat` only with images and `raw:true` without, and the 180 s timeout; each with usage including image tokens and `quote.cost_usd >= response.cost_usd`; a model without image pricing raises from `quote()` before any transport call. `tests/test_prompt_caching.py`: `CachingAnthropic` image branch (1600 tokens, cacheable only with `cache_control`); prefix cache reads survive an image block. `FakeAdapter.seen` shapes and priced images.

WP2: `tests/test_models_cli.py` (POSIX): fake `claude` records argv and stdin, asserting stream-json argv, one JSON line with an image block, JSONL result-line selection among `system`/`assistant` lines; fake `codex` asserts `-i <path>` per image, file exists at call time outside `work`, trailing `-`. Win32-runnable: `ClaudeCodeAdapter._argv/_stdin`, `CodexAdapter._argv`. `tests/test_runtime.py`: `call(images=)` reserves ceiling x count, `images_sent` and `TelemetryRow.image_inputs`, `BudgetExceeded` when a fake reports more than reserved, `image_unresolved` for an unattached stub. `tests/test_sandbox.py`: a page task crosses the pipe as stubs only and never exceeds `MESSAGE_LIMIT` (adversarial: a 200 KB base64 page); sha refs resolve only from the run's table; unknown ref, non-hex, or an `images` key on a text-only message -> `sandbox_invalid_call`; the AST guard still rejects `images=` in harness source. `tests/test_presets.py`: `lift_images` stubs and index order; extractor prompt has no base64 and carries `attached_images`; self-check attaches the same images; `_bounded` unaffected; existing tests unchanged. `tests/test_pipeline.py`: only configured stages attach; `original` carries stubs; `images-all-stages` re-attaches. `tests/test_floors.py`: checkers at the 0.9 boundary, count mismatch, label case, null preservation; `field_report` counts; `FloorConfig.checker` wiring. `tests/test_telemetry.py`: `image_inputs` column default and strictness. `docs/telemetry.md` lists the column.

WP3: `tests/test_bench_pages.py`: `clean` is byte-deterministic (pinned sha256 for seed 7, count 2), valid PNG header and dims, question counts per style, key page presence, `printed_key_wrong` rate, `write_dataset` shapes validate as `EvaluationCase` and against the suite input schema; Pillow tests `pytest.importorskip("PIL")`. `tests/test_bench_text_tasks.py`: determinism, ambiguity rate, null rate, oracle table keys. `tests/test_bench_oracle.py`: answers by sha of delivered bytes; wrong bytes -> wrong answer; noisy rate ~= 0.15 over 400 draws; priced nonzero; mutated `expected` does not change the answer. `tests/test_bench_compare.py`: Wilson against known values; McNemar exact against textbook tables; alignment refuses version/digest/size/fingerprint mismatch; every verdict branch including the model-agnosticism rule; `expect` failure. `tests/test_bench_rows.py`: round trip, `extra=forbid`, closed labels, sentinel never serialised. `tests/test_bench_suite.py`: `suite_digest` stable and changes on any task edit; spec builders validate; `max_calls_per_run` per preset; `minimum_samples` sizing.

WP4: `tests/test_bench_run.py`: smoke tier on the fake provider through the real Compiler in `tmp_path` (short paths): rows validate, `default` cells licensed, `noisy` unlicensed with `primary_floor_failed`, digitizer attaches once, `arc-local-3` rows, budgets lifted from quotes, `--max-usd` refusal before spend (adapter `calls == 0`), workspace removed, `HeldOutAccessError` guard, partial rows on cancel. `tests/test_bench_providers.py`: evidence labels, model filtering, probe rows with a fake that over-reports -> `image_reservation_under_configured`. `tests/test_games_public.py` and `tests/test_arc_harness_script.py`: lifted functions and byte-for-byte summary keys with `--offline`. `tests/test_cli.py`: every `bench` argument error as a code, `--json` shapes, exit codes. `tests/test_session_bench.py`: verb parsing, BENCHING vocabulary, confirmation prompt, `ScriptedConsole` transcript of `bench smoke`, cancel returns to READY with rows labelled `cancelled`, tables <= 100 columns, telemetry content-free. `tests/adversarial/test_phase8_bench.py`: the fake cannot read expected values; a bench row cannot claim `licensed` without a signed report on disk; compare refuses different fingerprints without the flag; sentinel strings never appear in rows.

WP5: `tests/live/test_models_live.py` opt-in image round trips (anthropic, openai, ollama `llama3.2-vision`, claude-code, codex) with a generated 200x80 digit PNG asserting a correct digit and `measured <= reserved`.

Ruff, strict mypy and the coverage gate stay as in `ci.yml`.

## 16. Honesty rules

- Every `BenchRow`, table and doc prints the `evidence` label next to every number and `floor_origin` next to every score. Suite floors are synthetic by construction: a score says "extracts what this generator prints", never "digitizes Lectern uploads". `docs/bench.md` and `docs/limitations.md` say so in the same words as the existing "not live uploaded-image vision" convention; the Lectern example README keeps its statement.
- CLI-provider costs are API-equivalent estimates from `prices.json` (Codex reports no cost); rows record `prices_sha256`, `reserved_usd` and `per_call_reservation_usd` next to `cost_usd`.
- Image reservations are ceilings with a `basis`; `assumption` entries (`gpt-5.6-terra`, `llama3.2-vision`, `fake`) are printed by `bench probe` and listed in the pricing docstring until replaced from measured rows; ceilings move only downward to `max(measured) * 1.25` with a cited row. `image_tokens_estimate` is labelled "estimate" wherever it is shown.
- Stage 4 remains the only licensing authority; the bench never constructs or edits a `TestReport`, `licensed` is re-read from disk, tuning replay touches only `floor.tuning`, delivery replay uses `Runtime.evaluate`, nothing is installed, and `compare` verdicts advise the operator and change no registry, artifact or preset.
- Tuning bits are cases Stage 2 selected on (selection bias); docs and the compare table say "paired on tuning cases (selection-biased); held-out is the licensing signal". The verdict refuses to call a difference on fewer than 5 discordant pairs.
- ARC rows keep `score_source`, `model_binding=online_preview` for cloud models and `compliance=unverified`; fixture-agent scores are labelled pipeline checks; nothing is a submission or benchmark result.
- Cost figures in this document and in `docs/bench.md` are price-table projections until the first live rows replace them, and are labelled as such.
- Telemetry stays content-free (one new integer column); rows and telemetry never carry prompts, outputs, page bytes, case ids or hashes of content. `model` in rows is a profile key.
- The three provider paths that were verified from binary strings and `--help` only (Claude Code stream-json, Codex `-i`, Ollama `/api/chat` images) are recorded as unverified in `docs/limitations.md` until `bench probe` rows exist, and their adapters fail closed.

## 17. Non-goals

- No Lectern tenant data, code, paths, uploads or commands; `{lectern-checkout}` is not read. Only shapes already in `examples/lectern/` are mirrored, by construction.
- No change to existing spec hashes, preset tunables, session verbs, states or codes beyond the additions listed here; no change to the CLI `arc-extra` job command.
- No new ARC strategies, rendered-frame inputs, `verifier_depth=2`, or `image_detail`/`vision` tunables in this phase.
- No committed PNG/JPEG binaries; no system fonts; no Pillow requirement for the default test suite or the `clean` tier.
- No token caps; no cost fallback for models without an image price (they are skipped on vision cells with `image_not_configured`, never priced by guess).
- No claim of provider token formulas being correct: they never affect reservations.

## 18. Work packages

WP1 Image core (models). Files: `architect/models/images.py`, `types.py`, `pricing.py`, `prices.json`, `http.py`, `fake.py`, `tests/image_support.py`, `tests/test_images.py`, `tests/test_models.py`, `tests/test_prompt_caching.py`. Exit: all three HTTP adapters and the fake accept images; unconfigured models refuse in `quote()` before I/O; `quote >= measured` on mocked usage including image tokens; text-only request bodies byte-identical to today. Depends on nothing; its signatures (3.1-3.3) are fixed by this document so WP2-WP4 can code against them from day one.

WP2 Threading. Files: `architect/models/cli.py`, `architect/runtime/__init__.py`, `sandbox.py`, `_sandbox_worker.py`, `pipeline.py`, `architect/presets/images.py`, `_execution.py`, `architect/telemetry/schema.py`, `architect/floors/checkers.py`, `architect/profiles.py`, `docs/telemetry.md`, tests listed in Section 15. Exit: a licensed extractor artifact runs a `clean` page through the fake end to end in native and sandbox modes with no base64 in any prompt or pipe message; pipeline attaches per policy; checkers wired through `FloorConfig.checker`. Depends on WP1 types (importable stubs on day one).

WP3 Bench data and statistics. Files: `architect/bench/__init__.py` (facade only), `rows.py`, `compare.py`, `pages.py`, `_font.py`, `text_tasks.py`, `oracle.py`, `suite.py`, `pyproject.toml` (extra), `examples/bench/suite-v1/*manifest*.json`, `examples/bench/README.md`, tests listed in Section 15. Exit: `generate_pages(7, 2, "clean")` is byte-pinned; oracle answers by delivered bytes; `compare` passes textbook tables and every verdict branch; `suite_digest("v1")` stable. Depends on WP1 for `ImageInput` only; parallel with WP2.

WP4 Runner and surfaces. Files: `architect/bench/runner.py`, `providers.py`, `render.py`, `ci_assert.py`, `architect/games/public.py`, `scripts/arc_harness.py`, `architect/cli.py`, `architect/session/bench.py`, `state.py`, `commands.py`, `codes.py`, `render.py`, `session.py`, `.github/workflows/ci.yml`, tests listed in Section 15. Exit: `architect bench run --provider fake --tier smoke --seed 7` passes every Section 13 assertion locally on Windows and Linux; `bench smoke` transcript test passes; `scripts/arc_harness.py --offline` summary unchanged. Depends on WP2 and WP3; can start against their signatures with the fake provider and a single text task.

WP5 Integration and evidence (after WP1-WP4 merge; operator-run for live parts). Commit `examples/bench/suite-v1/smoke-baseline.jsonl` from the first green smoke; run `bench probe` on `claude-haiku-4-5-20251001` via the `claude` CLI (and `codex` when quota returns on 2026-10-01; `ollama llama3.2-vision` locally) to settle the three provider paths and the `gpt-5.6-terra` assumption; run `bench quick` baseline on Haiku 4.5 (~$1.2 API-equivalent projected) and one Sonnet 5 cell; commit rows to `examples/bench/results/`; write `docs/bench.md`, `docs/bench/decisions.md` (first entry: baseline), update `docs/usage.md`, `docs/limitations.md`, `examples/telemetry/README.md`, `PROGRESS.md` with the same synthetic/live labelling as Phase 7; update `tests/live/test_models_live.py`. Exit: every number quoted in docs points at a committed row with its evidence label.

## 19. Risks settled or bounded by this contract

- Provider formulas may be wrong: they never reserve; ceilings do. A ceiling that is still too low fails a cell with `BudgetExceeded` (safe) and is reported as `image_reservation_under_configured` by `bench probe`, which is the mandatory first live step per model.
- Claude Code stream-json flags and result fields, Codex `-i` with stdin `-`, and Ollama `/api/chat` images are unverified until one probe call each; adapters fail closed.
- CLI adapters reserve full output capacity per call, so bounds are large on screen; the bench prints both bound and measured and caps on the bound.
- Pillow rendering is not byte-portable; only the stdlib `clean` tier is pinned, the oracle is in-process, and manifests record Pillow version and platform; `compare` refuses fake rows across different `pages_manifest_sha256`.
- Base64 pages inside `EvaluationCase` tasks make floor JSON files a few MB and partition digest sets hold whole pages; the sandbox stub path and the per-byte quote make any leak expensive and visible, and a test asserts no base64 in prompts or pipe messages.
- `State.BENCHING` and `bench` touch the closed session vocabulary; Section 11 lists the state, verb, codes and strings before implementation, as Phase 7 did.
- Smoke through the real Compiler may exceed the CI time budget; sizes are reduced by a suite version bump, never by silent skipping.
- Windows: subprocess CLI tests stay skipped; bench workspaces use the short `arch` prefix; stores and official workers are closed before workspace deletion.

## Phase 10 amendment: routed bench

`docs/interfaces/phase10-router.md` Section 14 (authoritative: `docs/bench.md`):

- the `routed` variant, `RouteFields` on `BenchRow.route`, and `BenchRow.artifact_digest` (both optional; old rows stay readable);
- `bench run --variant routed --rounds R --seed S` (the learning curve) and the roles arm `bench roles --policy pinned|evidence` over committed fake role logs;
- evidence labels moved to `architect.routing.labels` (`Provider`, `Evidence`, the provider-to-class map); the bench re-imports them and stays a leaf;
- the cell code `route_did_not_run`: a routed row whose exact probe had no candidate is written with it and the bench exits non-zero, mirroring `forge_did_not_run`.

All smoke rows are `synthetic_fake`: they prove plumbing and decision determinism, not routing quality.
