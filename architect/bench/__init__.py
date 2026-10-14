"""Bench: frozen suites, deterministic oracles, paired statistics and reports.

Suite ``v1`` is synthetic data; suite ``v2`` holds the pinned public ``receipts``
task (Phase 14), checked against its manifests before any spend.

This package facade re-exports the public surface of its modules and holds the
closed error-code table ``CODES`` (mirrored into ``architect/session/codes.py``).
Stage 4 remains the only licensing authority: nothing here constructs, edits
or re-signs a ``TestReport``; rows carry counts, labels and hashes of
configuration, never content.
"""

from .compare import (
    ArmPair,
    CellComparison,
    Comparison,
    PairedComparison,
    Verdict,
    align,
    bootstrap_ratio,
    compare,
    holm,
    mcnemar_exact,
    newcombe_paired,
    overall,
    pair_arms,
    verdict,
    wilson,
)
from .oracle import NoisyOracle, OracleAdapter, OracleState, oracle_factory
from .pages import (
    Choice,
    DatasetFiles,
    Page,
    PageTruth,
    Question,
    Style,
    cases_for,
    generate_pages,
    solve_question,
    write_dataset,
)
from .providers import (
    ProbeRow,
    build_factory,
    evidence_label,
    probe_models,
)
from .public_tasks import PublicData, PublicSettings, load_public
from .render import bench_table, compare_table, list_table, pairs_table, probe_table, report
from .router_eval import (
    LearningCurve,
    OracleOption,
    RegretCell,
    RegretTable,
    RoundPoint,
    learning_curve,
    oracle,
    regret_table,
)
from .rows import (
    ArcFields,
    BenchError,
    BenchRow,
    CellSummary,
    Code,
    Evidence,
    Kind,
    ModelId,
    Probe,
    Provider,
    RouteAction,
    RouteFields,
    Summary,
    TaskId,
    Tier,
    Variant,
    append_rows,
    architect_commit,
    decode_pass_bits,
    encode_pass_bits,
    preset_fingerprint,
    prices_sha256,
    read_rows,
    summarize,
    write_rows,
)
from .runner import (
    BenchConfig,
    BenchProgress,
    BenchResult,
    Bound,
    bound,
    run_bench,
)
from .suite import (
    SUITE_TIERS,
    SUITES,
    TIERS,
    BenchTask,
    CheckerName,
    DatasetBundle,
    suite_digest,
    tiers_for,
)
from .text_tasks import (
    generate_delivery_tickets,
    generate_invoices,
    generate_tickets,
    load_sentiment_cases,
)

__all__ = [
    "ArcFields",
    "ArmPair",
    "PairedComparison",
    "PublicData",
    "PublicSettings",
    "SUITE_TIERS",
    "bootstrap_ratio",
    "holm",
    "load_public",
    "newcombe_paired",
    "pair_arms",
    "pairs_table",
    "report",
    "tiers_for",
    "LearningCurve",
    "OracleOption",
    "Probe",
    "RegretCell",
    "RegretTable",
    "RoundPoint",
    "RouteAction",
    "RouteFields",
    "learning_curve",
    "oracle",
    "regret_table",
    "BenchConfig",
    "BenchProgress",
    "BenchResult",
    "Bound",
    "ProbeRow",
    "bench_table",
    "bound",
    "build_factory",
    "compare_table",
    "evidence_label",
    "list_table",
    "probe_models",
    "probe_table",
    "run_bench",
    "BenchError",
    "BenchRow",
    "BenchTask",
    "CELL_CODES",
    "CODES",
    "CellComparison",
    "CellSummary",
    "CheckerName",
    "Choice",
    "Code",
    "Comparison",
    "DatasetBundle",
    "DatasetFiles",
    "Evidence",
    "Kind",
    "ModelId",
    "NoisyOracle",
    "OracleAdapter",
    "OracleState",
    "Page",
    "PageTruth",
    "Provider",
    "Question",
    "SUITES",
    "Style",
    "Summary",
    "TIERS",
    "TaskId",
    "Tier",
    "Variant",
    "Verdict",
    "align",
    "append_rows",
    "architect_commit",
    "cases_for",
    "compare",
    "decode_pass_bits",
    "encode_pass_bits",
    "generate_delivery_tickets",
    "generate_invoices",
    "generate_pages",
    "generate_tickets",
    "load_sentiment_cases",
    "mcnemar_exact",
    "oracle_factory",
    "overall",
    "preset_fingerprint",
    "prices_sha256",
    "read_rows",
    "solve_question",
    "suite_digest",
    "summarize",
    "verdict",
    "wilson",
    "write_dataset",
    "write_rows",
]

# Closed code table for every bench failure an operator can see (Section 10).
CODES: frozenset[str] = frozenset(
    {
        "bench_suite_unknown",
        "bench_tier_unknown",
        "bench_variant_unknown",
        "bench_profile_required",
        "bench_invalid_profile",
        "bench_model_unconfigured",
        "bench_no_models",
        "bench_route_invalid",
        "bench_over_max_usd",
        "bench_extra_missing",
        "bench_arc_unavailable",
        "bench_out_unwritable",
        "bench_rows_unreadable",
        "bench_compare_mismatch",
        "bench_compare_fingerprint",
        "bench_expectation_failed",
        "bench_cancelled",
        "bench_provider_failed",
        "bench_workspace_error",
        # WP4 addition: the held-out guard tripped outside Stage 4; the run stops.
        "bench_held_out_access",
        # WP4 addition: the tier and filters selected no cell; nothing ran.
        "bench_no_cells",
        # WP4 addition: an Ollama model that is not on this host or not $0; its rows
        # could not honestly carry the local_zero_cost label, so nothing runs.
        "bench_model_not_local",
        # Phase 11 Section 3: a resumed run whose configuration or fingerprints changed,
        # and a routed run (one route log spans all its cells), which cannot resume.
        "bench_resume_mismatch",
        "bench_resume_unsupported",
        # Phase 14 Section 14: suite v2 flags and data, the plain arm, the audit replay,
        # and an audit directory that cannot be written (raised by ``bench run --audit``).
        "bench_task_unknown",
        "bench_split_unsupported",
        "bench_size_unsupported",
        "bench_delivery_all_unsupported",
        "bench_delivery_too_large",
        "bench_dataset_missing",
        "bench_dataset_mismatch",
        "bench_plain_variant",
        "bench_audit_unreadable",
        "audit_unwritable",
        # Phase 14 review: a public task's pinned per-run budget is below one run's
        # sized model calls, which would zero the arm; refused before any spend.
        "bench_run_budget_too_small",
    }
)

# Cell codes recorded in ``BenchRow.failures`` next to compiler codes.
CELL_CODES: frozenset[str] = frozenset(
    {
        "image_not_configured",
        "arc_unavailable",
        "over_max_usd",
        "extra_missing",
        "cancelled",
        "image_reservation_under_configured",
        "image_unresolved",
        "workspace_error",
        # Phase 9: a forge row with no measure or a template row with no submission.
        "forge_did_not_run",
        # Phase 9 (Section 14): a forge or template game cell refused under 6 GB free RAM.
        "host_memory_low",
        # Phase 10 (Section 14): a routed row with no decision, or whose exact probe
        # listed no candidate; the run fails, mirroring forge_did_not_run.
        "route_did_not_run",
    }
)
