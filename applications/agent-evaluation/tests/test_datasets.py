"""Dataset validation: strict, never truncated, never silently empty."""

from __future__ import annotations

import pytest

from app.datasets import DatasetError, available_datasets, load_dataset


def write(tmp_path, body: str, name="smoke-v1"):
    path = tmp_path / f"{name}.yaml"
    path.write_text(body)
    return path


VALID = """\
name: smoke-v1
description: d
cases:
  - id: one
    input: hi
    expected: hello
    tags: [greeting]
  - id: two
    input: what time
    expected: a time
"""


def test_valid_dataset_loads(tmp_path):
    ds = load_dataset(write(tmp_path, VALID), max_cases=50)
    assert ds.name == "smoke-v1"
    assert [c.id for c in ds.cases] == ["one", "two"]
    assert ds.cases[0].tags == ("greeting",)
    assert not ds.uses_expect_tools


def test_zero_cases_is_an_error(tmp_path):
    with pytest.raises(DatasetError, match="no cases cannot score a revision"):
        load_dataset(write(tmp_path, "name: smoke-v1\ncases: []\n"), max_cases=50)


def test_too_many_cases_is_an_error_not_a_truncation(tmp_path):
    with pytest.raises(DatasetError, match="never truncated"):
        load_dataset(write(tmp_path, VALID), max_cases=1)


def test_duplicate_case_id(tmp_path):
    body = VALID.replace("id: two", "id: one")
    with pytest.raises(DatasetError, match="not unique"):
        load_dataset(write(tmp_path, body), max_cases=50)


@pytest.mark.parametrize("bad_id", ["One", "a_b", "-a", "a" * 41])
def test_bad_case_id(tmp_path, bad_id):
    body = VALID.replace("id: two", f"id: {bad_id}")
    with pytest.raises(DatasetError, match="id"):
        load_dataset(write(tmp_path, body), max_cases=50)


@pytest.mark.parametrize("field", ["input", "expected"])
def test_missing_input_or_expected(tmp_path, field):
    body = VALID.replace(f"    {field}: what time\n", "").replace(
        f"    {field}: a time\n", ""
    )
    with pytest.raises(DatasetError, match=field):
        load_dataset(write(tmp_path, body), max_cases=50)


def test_name_must_equal_file_stem(tmp_path):
    with pytest.raises(DatasetError, match="file name"):
        load_dataset(write(tmp_path, VALID, name="other"), max_cases=50)


def test_expect_tools_is_accepted_and_flagged(tmp_path):
    body = VALID.replace("    tags: [greeting]\n", "    expectTools: [get_time]\n")
    ds = load_dataset(write(tmp_path, body), max_cases=50)
    assert ds.cases[0].expect_tools == ("get_time",)
    assert ds.uses_expect_tools


def test_available_datasets_reads_file_stems(tmp_path):
    write(tmp_path, VALID)
    (tmp_path / "broken.yaml").write_text(":::")
    (tmp_path / "notes.txt").write_text("x")
    assert available_datasets(tmp_path) == {"smoke-v1", "broken"}
    assert available_datasets(tmp_path / "missing") == set()
