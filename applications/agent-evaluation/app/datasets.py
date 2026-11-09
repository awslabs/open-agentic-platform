"""Harness datasets: platform-owned case files referenced by name.

A dataset is a chart file `files/harness/datasets/<name>.yaml`, mounted into the
reconciler and every harness Job from one ConfigMap. Validation is strict and
never silently repairs a file: a dataset with no cases, or more cases than the
platform allows, cannot score a revision, so loading it is an error rather than
a truncated or empty run that would read as a result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

# Same rule as the CUE #DatasetName and as Kubernetes DNS labels.
NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9]|-[a-z0-9])*$")
MAX_NAME_LEN = 63
MAX_CASE_ID_LEN = 40


class DatasetError(Exception):
    """A dataset that cannot be used for a run. Reported as a run error."""


@dataclass(frozen=True)
class Case:
    id: str
    input: str
    expected: str
    # Accepted and ignored in the first cut (no tool-call capture yet); a run
    # that sees them reports expectToolsIgnored so nobody reads them as checked.
    expect_tools: tuple[str, ...] | None
    tags: tuple[str, ...]


@dataclass(frozen=True)
class Dataset:
    name: str
    description: str
    cases: tuple[Case, ...]

    @property
    def uses_expect_tools(self) -> bool:
        return any(c.expect_tools is not None for c in self.cases)


def _str_list(value, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(v, str) and v for v in value
    ):
        raise DatasetError(f"{where} must be a list of non-empty strings")
    return tuple(value)


def load_dataset(path: str | Path, max_cases: int) -> Dataset:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DatasetError(f"cannot read dataset {path.name}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise DatasetError(f"dataset {path.name} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise DatasetError(f"dataset {path.name} must be a YAML mapping")

    name = raw.get("name")
    if name != path.stem:
        raise DatasetError(
            f"dataset name {name!r} must equal its file name {path.stem!r}"
        )
    if not isinstance(name, str) or len(name) > MAX_NAME_LEN or not NAME_RE.match(name):
        raise DatasetError(
            f"dataset name {name!r} must match {NAME_RE.pattern} (<= {MAX_NAME_LEN})"
        )

    cases_raw = raw.get("cases")
    if not isinstance(cases_raw, list):
        raise DatasetError(f"dataset {name}: `cases` must be a list")
    if not cases_raw:
        raise DatasetError(
            f"dataset {name} has zero cases: a dataset with no cases cannot score a revision"
        )
    if len(cases_raw) > max_cases:
        raise DatasetError(
            f"dataset {name} has {len(cases_raw)} cases, more than the platform "
            f"limit of {max_cases}; cases are never truncated"
        )

    cases: list[Case] = []
    seen: set[str] = set()
    for index, entry in enumerate(cases_raw):
        where = f"dataset {name} case #{index}"
        if not isinstance(entry, dict):
            raise DatasetError(f"{where} must be a mapping")
        case_id = entry.get("id")
        if (
            not isinstance(case_id, str)
            or len(case_id) > MAX_CASE_ID_LEN
            or not NAME_RE.match(case_id)
        ):
            raise DatasetError(
                f"{where}: id {case_id!r} must match {NAME_RE.pattern} "
                f"(<= {MAX_CASE_ID_LEN})"
            )
        if case_id in seen:
            raise DatasetError(f"dataset {name}: case id {case_id!r} is not unique")
        seen.add(case_id)
        for key in ("input", "expected"):
            value = entry.get(key)
            if not isinstance(value, str) or not value.strip():
                raise DatasetError(
                    f"dataset {name} case {case_id}: {key} must be a non-empty string"
                )
        expect_tools = None
        if entry.get("expectTools") is not None:
            expect_tools = _str_list(
                entry["expectTools"], f"dataset {name} case {case_id}: expectTools"
            )
        tags: tuple[str, ...] = ()
        if entry.get("tags") is not None:
            tags = _str_list(entry["tags"], f"dataset {name} case {case_id}: tags")
        cases.append(
            Case(
                id=case_id,
                input=entry["input"],
                expected=entry["expected"],
                expect_tools=expect_tools,
                tags=tags,
            )
        )

    return Dataset(
        name=name,
        description=str(raw.get("description") or ""),
        cases=tuple(cases),
    )


def available_datasets(directory: str | Path) -> set[str]:
    """Names of the datasets mounted in `directory`, from file stems only.

    No parsing: the reconciler only needs to tell a declaration that names a
    dataset the platform does not ship (UnknownDataset). Content errors are the
    Job's to report, as a run error.
    """
    path = Path(directory)
    try:
        return {p.stem for p in path.glob("*.yaml") if p.is_file()}
    except OSError:
        return set()
