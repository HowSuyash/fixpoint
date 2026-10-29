import pytest

from fixpoint import workspace
from fixpoint.sandbox import Sandbox
from fixpoint.tools import Toolbox, is_test_file

TEST_CMD = "python -m pytest -q -p no:cacheprovider"


@pytest.fixture
def box(tmp_path):
    work = workspace.prepare("scheduler", tmp_path)
    return Toolbox(work, Sandbox(work, mode="local", timeout=60), TEST_CMD)


def test_is_test_file():
    assert is_test_file("tests/test_x.py")
    assert is_test_file("pkg/test_utils.py")
    assert is_test_file("pkg/utils_test.py")
    assert is_test_file("conftest.py")
    assert not is_test_file("scheduler/dates.py")
    assert not is_test_file("contest.py")


def test_paths_cannot_escape_the_repo(box):
    assert box.execute("read_file", {"path": "../outside.txt"}).startswith("ERROR")
    assert box.execute("read_file", {"path": ".git/config"}).startswith("ERROR")


def test_list_and_read(box):
    listing = box.execute("list_files", {})
    assert "scheduler/dates.py" in listing and "tests/" in listing
    assert "ISSUE.md" not in listing  # the issue is input, not part of the repo
    text = box.execute("read_file", {"path": "scheduler/dates.py", "start_line": 8, "end_line": 9})
    assert "def add_months" in text and "lines 8-9" in text


def test_search_code_falls_back_to_literal(box):
    assert "scheduler/dates.py:" in box.execute("search_code", {"pattern": "d.replace("})


def test_write_policy_by_phase(box):
    box.write_policy = "none"
    assert box.execute("write_file", {"path": "tests/test_new.py", "content": "x"}).startswith("ERROR")
    box.write_policy = "tests"
    assert box.execute("write_file", {"path": "scheduler/dates.py", "content": "x"}).startswith("ERROR")
    assert box.execute("write_file", {"path": "tests/test_new.py", "content": "def test_ok():\n    pass\n"}).startswith("wrote")
    box.write_policy = "source"
    assert "locked" in box.execute("write_file", {"path": "tests/test_new.py", "content": "x"})


def test_replace_requires_a_unique_match(box):
    box.write_policy = "source"
    assert "not found" in box.execute("replace_in_file", {"path": "scheduler/dates.py", "old": "nope", "new": "x"})
    assert "3 times" in box.execute("replace_in_file", {"path": "scheduler/dates.py", "old": "d: date", "new": "x"})
    out = box.execute("replace_in_file", {"path": "scheduler/dates.py", "old": "return d.weekday() >= 5", "new": "return d.weekday() > 4"})
    assert out == "edited scheduler/dates.py"
    assert "d.weekday() > 4" in (box.root / "scheduler/dates.py").read_text()
    assert "scheduler/dates.py" in workspace.diff(box.root)


def test_python_diff_matches_git_format(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "git_available", lambda: False)
    work = workspace.prepare("scheduler", tmp_path)
    assert not (work / ".git").exists()
    dates = work / "scheduler/dates.py"
    dates.write_text(dates.read_text().replace("d.weekday() >= 5", "d.weekday() > 4"))
    (work / "tests/test_new.py").write_text("def test_x():\n    pass\n")
    (work / "scheduler/__pycache__").mkdir()
    (work / "scheduler/__pycache__/x.pyc").write_bytes(b"\0junk")
    text = workspace.diff(work)
    assert "diff --git a/scheduler/dates.py b/scheduler/dates.py" in text
    assert "-    return d.weekday() >= 5\n+    return d.weekday() > 4\n" in text
    assert "new file mode 100644\n--- /dev/null\n+++ b/tests/test_new.py" in text
    assert "__pycache__" not in text


def test_run_tests_and_selector_validation(box):
    assert "PASSED" in box.execute("run_tests", {})
    assert box.execute("run_tests", {"target": "tests; rm -rf /"}).startswith("ERROR")


def test_web_search_without_key(box):
    assert "not configured" in box.execute("web_search", {"query": "x"})
