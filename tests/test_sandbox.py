import os

from fixpoint.sandbox import Sandbox, tail


def test_local_run_captures_output_and_exit_code(tmp_path):
    box = Sandbox(tmp_path, mode="local", timeout=30)
    ok = box.run('python -c "print(42)"')
    assert ok.passed and "42" in ok.output
    bad = box.run('python -c "import sys; sys.exit(3)"')
    assert bad.exit_code == 3 and not bad.passed


def test_local_run_times_out(tmp_path):
    box = Sandbox(tmp_path, mode="local", timeout=2)
    result = box.run('python -c "import time; time.sleep(30)"')
    assert result.timed_out and not result.passed
    assert result.seconds < 20


def test_secrets_are_hidden_from_code_under_test(tmp_path, monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", "super-secret")
    box = Sandbox(tmp_path, mode="local", timeout=30)
    result = box.run("python -c \"import os; print(os.environ.get('NEBIUS_API_KEY', 'hidden'))\"")
    assert "hidden" in result.output and "super-secret" not in result.output
    assert os.environ["NEBIUS_API_KEY"] == "super-secret"


def test_tail_keeps_the_end():
    assert tail("abcdef", 3).endswith("def")
    assert tail("abc", 10) == "abc"
