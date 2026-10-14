# Phase 1 interfaces (frozen before dispatch)
Python 3.12, Pydantic v2, strict mypy, ruff. Public exports in each module __init__.py.
Do not touch another owner's files. JSONValue = recursive JSON types (pydantic.JsonValue).
All schemas extra=forbid, frozen where possible, finite nonnegative budget/cost values.

## architect.spec (schema worker also owns architect/_authority.py)
Lifecycle(str,Enum): INSTALL='install', EXPIRE='expire'.
FloorKind(str,Enum): HELD_OUT_TASKS='held_out_tasks', HUMAN_EDIT_RATE='human_edit_rate', LLM_JUDGE='llm_judge', HELD_OUT_GAME_SCORE='held_out_game_score'.
Budget(BaseModel): tokens:int=4096 (>0), cost_usd:float=0.1 (>0), compute_s:float=60 (>0).
FloorDefinition: kind:FloorKind, limit:float in [0,1], dataset:str, minimum_samples:int=30, rubric:str|None=None, judge_model:str|None=None, calibration_dataset:str|None=None. For edit rate lower is better; otherwise higher is better.
PipelineStep: name:str, preset:str, model:str, budget:Budget.
Spec: task:str (nonempty), input_schema:dict[str,JsonValue], output_schema:dict[str,JsonValue], floor:FloorDefinition, secondary_floor:FloorDefinition|None=None, budget:Budget, model_target:str='fake' (ladder sentinel exactly 'cheapest that passes'), lifecycle:Lifecycle=EXPIRE, domain:str='general', tools:tuple[str,...]=(), pipeline:tuple[PipelineStep,...]=(), provenance:dict[str,Literal['inferred','user']]={}. spec_hash property -> SHA256 canonical JSON, excludes provenance.
TestReport: spec_hash:str, artifact_digest:str, passed:bool, floor_score:float, floor_limit:float, floor_kind:FloorKind, tokens:int, cost_usd:float, compute_s:float, component_count:int, failures:tuple[str,...]=(), verification_id:str, signature:str=''. Immutable. Signature verifies all report values; only verification code may issue signed reports. Validation alone never licenses.
HarnessArtifact: spec:Spec, version:int=1, preset:str, model:str, components:tuple[str,...], params:dict[str,JsonValue]={}, directory:Path, report:TestReport|None=None. artifact_digest property binds spec, version, preset, model, components, params and code bytes at directory/harness.py. licensed read-only property checks authenticated passing report, report binding and current budget/floor. No licensed constructor input. save() -> None writes spec.json, config.json, report.json if present (code already there); load(directory:Path) -> HarnessArtifact, revalidates signature. Version paths artifacts/<hash>/<version> enforced by compiler not generic toy artifacts.
_authority: local integrity HMAC key persisted via ARCHITECT_SIGNING_KEY_FILE default .architect/signing.key (mode 0600 where supported); verify_report(report payload, signature). Private signing callable used ONLY by stage 4 code. Threat boundary: trusted Python host, untrusted model responses, no promise against malicious Python reflection or stolen local key.

## architect.models (adapter worker)
ModelRequest: prompt:str, max_output_tokens:int=128, temperature:float=0.
ModelResponse: text:str, tokens_in:int, tokens_out:int, cost_usd:float, model:str.
UsageQuote: tokens_in:int, tokens_out:int, cost_usd:float (upper bounds).
ModelAdapter(Protocol): model:str; quote(request:ModelRequest)->UsageQuote; async complete(request:ModelRequest)->ModelResponse.
AdapterError(Exception). Prices loaded from JSON configuration architect/models/prices.json (per million tokens), not code constants.
FakeAdapter(model:str='fake', responses:Sequence[ModelResponse]|None=None, fixture:Path|None=None). Fixture JSON list of response objects; deterministic sequential responses. quote uses next scripted response or deterministic default. complete increments calls:int; exhaustion raises AdapterError if explicit script. Default responds 'ok' with fixed usage for smoke. from_fixture optional convenience.
AnthropicAdapter, OpenAICompatibleAdapter, OllamaAdapter: HTTP via httpx AsyncClient; injectable transport for tests; env credentials; bound token estimates conservative, count actual server usage, configurable pricing path. No real calls in ordinary tests. Live tests skipped without explicit opt-in/key.

## architect.telemetry (telemetry worker)
TelemetryRow: run_id:str, timestamp:datetime, domain:categorical, task_type:categorical, spec_hash:str, harness_version:int, lifecycle:Lifecycle, preset_origin:categorical, component_set:tuple[categorical,...], model:categorical, model_params:dict[enum-key,float|int|bool], tokens_in:int, tokens_out:int, cost_usd:float, wall_time_s:float, floor_type:FloorKind, floor_score:float|None, floor_limit:float, accepted:bool, drift_flag:bool=False.
Categoricals are closed enums or normalized deterministic SHA256 identifiers, NOT arbitrary short text. Unknown domain/task/model/component values must be hashed by categorical(value:str)->str; schema only accepts known enum values or hash prefix h_ + 32 hexadecimal chars. Enforce max 64 for all string fields except run_id/spec_hash, and nested strings too. UUID run_id and 64 hex spec_hash constraints. No prompts, results, exception messages. Numeric-only model_params fixed whitelist keys ('temperature','max_output_tokens','context_window','retries','verifier_depth','terseness'). Extra keys forbidden. Finite nonnegative usage. floor_score [0,1].
TelemetryStore(directory:Path): append(row:TelemetryRow)->None; rows()->list[TelemetryRow]. Append-only runs.jsonl + index.sqlite3. SQLite indexes metadata, no content. Reconcile JSONL rows into SQLite when opened after crash. Atomic append lock; duplicate run_id refused. Context manager/close allowed.

## architect.runtime (runtime worker)
BudgetExceeded(Exception), UnlicensedArtifactError(Exception), ArtifactExpiredError(Exception).
RunContext(adapter:ModelAdapter,budget:Budget): async call(prompt:str,max_output_tokens:int=128)->str; tokens_in:int,tokens_out:int,cost_usd:float,elapsed_s:float. Reserve conservative quote BEFORE each call; reject call which would exceed any limit; timeout compute remaining using asyncio.wait_for. Check actual usage afterward; record consumed failure usage, never next call after overrun. Adapter underreport cannot permit delivery. Track in-flight context.call tasks: a harness that returns while model work remains must fail closed, cancel/drain owned calls before the final telemetry row, and never leave model tasks running or log success before usage settles.
Harness(Protocol): async __call__(context:RunContext,task:JsonValue)->JsonValue.
RunResult: run_id:str, output:JsonValue, tokens_in:int,tokens_out:int,cost_usd:float,wall_time_s:float.
Runtime(store:TelemetryStore): async run(artifact:HarnessArtifact,task:JsonValue,adapter:ModelAdapter)->RunResult. Load and execute only the artifact directory/harness.py async run(context,task), never an unrelated caller-supplied callable. Export load_harness(directory:Path)->Harness for internal candidate evaluation. Reject unlicensed artifact before code loading/execution but still emit row, consume expire once with persisted marker in artifact dir, including failed accepted execution attempts (no repeat). Must validate code digest and model adapter binding. Always emit exactly one row, including exceptions; propagate exception after logging without content. Enforce total harness wall timeout, not just calls.
Runtime.evaluate(spec:Spec,harness:Harness,task:JsonValue,adapter:ModelAdapter,*,version:int=1,preset:str='toy',components:tuple[str,...]=())->RunResult: internal evaluation mode, budget guarded/logged but not licensed client delivery. Stage 2 and verification need this; not exposed in HTTP client.

## Tests and ownership
schema worker: architect/spec/** architect/_authority.py tests/test_spec.py
adapter worker: architect/models/** tests/test_models.py tests/live/**
telemetry worker: architect/telemetry/** tests/test_telemetry.py
runtime worker: architect/runtime/** tests/test_runtime.py
independent verifier: tests/adversarial/test_phase1.py ONLY. Attack unsigned report, budget preflight/timeout, telemetry short secret strings and nested metadata, rejection rows.
reviewer: docs/reviews/phase1.md ONLY, no fixes.
integrator: project config, docs, exit test tests/test_phase1_exit.py and integration fixes after module returns.
Exit: hand-written two-call toy evaluated against scripted fake; one call fits, second fails before adapter invocation; exactly one valid private telemetry row. Runtime.run rejects unsigned artifact. Full pytest + ruff + mypy pass.
