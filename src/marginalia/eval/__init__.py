"""External retrieval evaluation support."""

from marginalia.eval.core import (
    EvalAnswerProbeResult,
    EvalAnswerRunResult,
    EvalImportResult,
    EvalReportCompareResult,
    EvalRunResult,
    answer_probe_to_dict,
    answer_run_to_dict,
    format_report_compare_result,
    format_answer_run_result,
    format_answer_probe_result,
    import_beir_dataset,
    report_compare_to_dict,
    run_answer_eval_dataset,
    run_answer_probe,
    run_eval_dataset,
    run_report_compare_dataset,
)

__all__ = [
    "EvalAnswerProbeResult",
    "EvalAnswerRunResult",
    "EvalImportResult",
    "EvalReportCompareResult",
    "EvalRunResult",
    "answer_probe_to_dict",
    "answer_run_to_dict",
    "format_report_compare_result",
    "format_answer_run_result",
    "format_answer_probe_result",
    "import_beir_dataset",
    "report_compare_to_dict",
    "run_answer_eval_dataset",
    "run_answer_probe",
    "run_eval_dataset",
    "run_report_compare_dataset",
]
