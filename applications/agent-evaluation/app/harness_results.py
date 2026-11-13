"""The result contract between a harness Job and the reconciler.

The Job writes one ConfigMap `<job>-result` with key `result.json`; the
reconciler parses it and publishes oap_agent_harness_*. Parsing is strict: a
result the reconciler does not understand (wrong schema, missing key) is a run
ERROR, never a run with zero passes.

Case statuses: `passed` and `failed` are judged outcomes of the revision; an
agent reply that is not a usable answer is `failed` too (the runner decides).
`error` is a case nothing could be learned from (judge error after retries,
transport or 4xx on the preview path, run deadline): it is counted in `errors`
and kept out of the pass ratio, so a flaky judge does not move it.

JSON keys are camelCase (`passRatio`, `expectToolsIgnored`); the dataclass
fields are their snake_case equivalents. Timestamps are Unix seconds.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields

RESULT_SCHEMA = "oap.harness.result/v1"
RESULT_KEY = "result.json"
MAX_REASON_CHARS = 300

CASE_STATUSES = ("passed", "failed", "error")
RUN_STATUSES = ("completed", "error")


class ResultSchemaError(Exception):
    """A result that cannot be understood. The run counts as an error."""


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(p.capitalize() for p in rest)


@dataclass(frozen=True)
class CaseResult:
    id: str
    status: str
    score: float | None
    reason: str
    latency_ms: int
    tags: tuple[str, ...]


@dataclass(frozen=True)
class RunResult:
    schema: str
    namespace: str
    agent: str
    revision: str
    dataset: str
    checks: tuple[str, ...]
    threshold: float
    mode: str
    trigger: str
    judge_model: str
    judge_key_present: bool
    started_at: float
    finished_at: float
    status: str
    error: str | None
    cases: int
    passed: int
    failed: int
    errors: int
    pass_ratio: float | None
    threshold_met: bool | None
    expect_tools_ignored: bool
    case_results: tuple[CaseResult, ...]


def summarize(
    *,
    namespace: str,
    agent: str,
    revision: str,
    dataset: str,
    checks,
    threshold: float,
    mode: str,
    trigger: str,
    judge_model: str,
    judge_key_present: bool,
    started_at: float,
    finished_at: float,
    case_results=(),
    status: str = "completed",
    error: str | None = None,
    expect_tools_ignored: bool = False,
) -> RunResult:
    case_results = tuple(case_results)
    cases = len(case_results)
    passed = sum(1 for c in case_results if c.status == "passed")
    failed = sum(1 for c in case_results if c.status == "failed")
    errors = sum(1 for c in case_results if c.status == "error")
    if status == "completed" and cases > 0 and errors == cases:
        # Nothing was judged (judge or preview path down for every case): an
        # outage, so a run error with no ratio, never a 0% revision. The case
        # results are kept so the reasons stay readable in result.json.
        status = "error"
        error = f"every case errored ({errors} of {cases}); first: {case_results[0].reason}"
    judged = passed + failed
    # Errors are outside the denominator: they say nothing about the revision.
    pass_ratio = passed / judged if status == "completed" and judged > 0 else None
    threshold_met = None if pass_ratio is None else pass_ratio >= threshold
    return RunResult(
        schema=RESULT_SCHEMA,
        namespace=namespace,
        agent=agent,
        revision=revision,
        dataset=dataset,
        checks=tuple(checks),
        threshold=threshold,
        mode=mode,
        trigger=trigger,
        judge_model=judge_model,
        judge_key_present=judge_key_present,
        started_at=started_at,
        finished_at=finished_at,
        status=status,
        error=error,
        cases=cases,
        passed=passed,
        failed=failed,
        errors=errors,
        pass_ratio=pass_ratio,
        threshold_met=threshold_met,
        expect_tools_ignored=expect_tools_ignored,
        case_results=case_results,
    )


def _case_to_json(case: CaseResult) -> dict:
    return {
        "id": case.id,
        "status": case.status,
        "score": case.score,
        "reason": case.reason[:MAX_REASON_CHARS],
        "latencyMs": case.latency_ms,
        "tags": list(case.tags),
    }


def to_configmap_data(run: RunResult) -> dict[str, str]:
    out: dict = {}
    for f in fields(RunResult):
        value = getattr(run, f.name)
        if f.name == "case_results":
            value = [_case_to_json(c) for c in value]
        elif f.name == "checks":
            value = list(value)
        out[_camel(f.name)] = value
    return {RESULT_KEY: json.dumps(out, sort_keys=True)}


def _need(obj: dict, key: str, where: str):
    if key not in obj:
        raise ResultSchemaError(f"{where} is missing required key {key!r}")
    return obj[key]


def parse_result(data: dict | None) -> RunResult:
    if not data or RESULT_KEY not in data:
        raise ResultSchemaError(f"result has no {RESULT_KEY}")
    try:
        raw = json.loads(data[RESULT_KEY])
    except (TypeError, ValueError) as exc:
        raise ResultSchemaError(f"{RESULT_KEY} is not JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ResultSchemaError(f"{RESULT_KEY} is not a JSON object")
    if raw.get("schema") != RESULT_SCHEMA:
        raise ResultSchemaError(
            f"result schema {raw.get('schema')!r} is not {RESULT_SCHEMA!r}"
        )

    values: dict = {}
    for f in fields(RunResult):
        values[f.name] = _need(raw, _camel(f.name), "result")
    if values["status"] not in RUN_STATUSES:
        raise ResultSchemaError(f"result status {values['status']!r} is not known")
    if not isinstance(values["case_results"], list):
        raise ResultSchemaError("caseResults is not a list")

    cases: list[CaseResult] = []
    # int(), tuple() on a wrong-typed field (`tags: 5`, `latencyMs: "x"`,
    # `checks: 7`) raise TypeError/ValueError: that is a result not understood,
    # so a run error like any other, never a crash of the pass.
    try:
        for entry in values["case_results"]:
            if not isinstance(entry, dict):
                raise ResultSchemaError("a case result is not an object")
            status = _need(entry, "status", "case result")
            if status not in CASE_STATUSES:
                raise ResultSchemaError(f"case status {status!r} is not known")
            cases.append(
                CaseResult(
                    id=str(_need(entry, "id", "case result")),
                    status=status,
                    score=_need(entry, "score", "case result"),
                    reason=str(_need(entry, "reason", "case result")),
                    latency_ms=int(_need(entry, "latencyMs", "case result")),
                    tags=tuple(_need(entry, "tags", "case result")),
                )
            )
        values["checks"] = tuple(values["checks"])
    except (TypeError, ValueError) as exc:
        raise ResultSchemaError(f"result has a wrong-typed field: {exc}") from exc
    values["case_results"] = tuple(cases)
    for key in ("cases", "passed", "failed", "errors"):
        if not isinstance(values[key], int) or isinstance(values[key], bool):
            raise ResultSchemaError(f"result {_camel(key)} is not an integer")
    try:
        values["threshold"] = float(values["threshold"])
        values["started_at"] = float(values["started_at"])
        values["finished_at"] = float(values["finished_at"])
        if values["pass_ratio"] is not None:
            values["pass_ratio"] = float(values["pass_ratio"])
    except (TypeError, ValueError) as exc:
        raise ResultSchemaError(f"result has a non-numeric field: {exc}") from exc
    return RunResult(**values)
