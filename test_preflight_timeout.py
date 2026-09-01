import subprocess

import run_desk


def test_slow_codex_status_does_not_crashloop_an_authenticated_desk(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: r"C:\Tools\Codex\codex.exe")

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout", 30))

    monkeypatch.setattr("subprocess.run", timeout)
    check = run_desk.check_analyst_backend("codex:gpt-5.6-sol")
    assert not check.ok
    assert not check.fatal
    assert "starting provisionally" in check.detail
