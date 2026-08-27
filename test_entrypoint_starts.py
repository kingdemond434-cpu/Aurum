"""The entry point has to START. Nothing was checking that, and it stopped starting.

WHAT HAPPENED

A merge that resolved two branches' work on `run_desk.py` left two defects behind:

  1. `build_service(... provider_spec=..., ... provider_spec=...)` -- a repeated keyword,
     which is a SyntaxError. `run_desk.py` did not parse at all.
  2. `ap.add_argument("--provider", ...)` twice -- syntactically fine, and argparse raises
     `ArgumentError: conflicting option string` the moment `main()` builds the parser.

Both were in the ONE file that is the desk's front door, and the whole suite stayed green
through them: 737 tests passed while `python run_desk.py` could not reach its first line of
work. Six tests did fail, but they failed in `test_notify_health.py` and read as a Telegram
problem -- the symptom pointed at the wrong subsystem, because those were the only tests that
touched `run_desk.py` at all, and they touched it by reading its source rather than running it.

WHY THE OBVIOUS CHECKS DO NOT COVER THIS

Defect 1 is invisible to anything that greps source and invisible to any test that does not
import the module. Defect 2 survives even an import: `add_argument` runs inside `main()`, so
the module imports cleanly and fails only when someone actually invokes it. A syntax sweep
catches the first and not the second. That is the gap this file closes: the only check that
covers both is BUILDING THE PARSER AND RUNNING IT.

This is the `--help` test, and it earns its place precisely because it looks too trivial to
write. A desk that cannot start is not a degraded desk; it is an absent one, and it is absent
in a way that every other green test actively disguises.
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent

#: Every module a person or a cron line invokes directly. A library module that breaks shows up
#: in its own tests; these are the ones whose failure is silent until someone tries to run them.
ENTRY_POINTS = ["run_desk.py", "aurum_cycle.py", "run_backtest.py"]


@pytest.mark.parametrize("script", ENTRY_POINTS)
def test_the_entry_point_actually_starts(script: str) -> None:
    """`--help` is the cheapest possible proof that argument wiring is intact.

    It builds the full parser -- so a duplicate flag, a bad `choices=`, a default referencing a
    name that no longer exists, or any import error on the module's path all surface here -- and
    exits before touching a feed, a terminal, a secret or the network.
    """
    path = _ROOT / script
    if not path.exists():
        pytest.skip(f"{script} not present")
    proc = subprocess.run([sys.executable, str(path), "--help"],
                          capture_output=True, text=True, timeout=120, cwd=str(_ROOT))
    assert proc.returncode == 0, (
        f"{script} --help exited {proc.returncode}. The desk cannot start.\n"
        f"--- stderr ---\n{proc.stderr[-3000:]}")
    assert "usage:" in proc.stdout, f"{script} --help printed no usage line"


@pytest.mark.parametrize("script", ENTRY_POINTS)
def test_no_command_line_flag_is_defined_twice(script: str) -> None:
    """Names the offending flag, which `--help` alone does not.

    The `--help` test above already fails on a duplicate, but it fails with argparse's message
    at whichever flag happens to be second. Parsing the source lists every duplicate at once,
    so a merge that collided on three flags is one fix rather than three rounds.
    """
    path = _ROOT / script
    if not path.exists():
        pytest.skip(f"{script} not present")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    seen: dict[str, int] = {}
    dupes: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                    and arg.value.startswith("--"):
                if arg.value in seen:
                    dupes.append(f"{arg.value} (lines {seen[arg.value]} and {node.lineno})")
                else:
                    seen[arg.value] = node.lineno
    assert not dupes, f"{script} defines the same flag twice: {'; '.join(dupes)}"


def test_no_function_call_repeats_a_keyword_argument() -> None:
    """The SyntaxError half, swept across the tree rather than pinned to one file.

    A repeated keyword is a hard parse failure, so this cannot be checked by importing -- the
    import is what fails. It has to be read off the source of every file, and a file that will
    not parse IS the finding.
    """
    offenders: list[str] = []
    for path in sorted(_ROOT.glob("*.py")) + sorted(_ROOT.glob("golddesk/*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            offenders.append(f"{path.name}:{exc.lineno}: will not parse -- {exc.msg}")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            kw = [k.arg for k in node.keywords if k.arg is not None]
            for name in {n for n in kw if kw.count(n) > 1}:
                offenders.append(f"{path.name}:{node.lineno}: keyword '{name}' passed twice")
    assert not offenders, "\n".join(offenders)


def test_the_installed_task_launches_with_the_same_args_the_supervisor_defaults_to():
    """THE INSTALLED TASK IS THE ONE THAT ACTUALLY RUNS, AND IT HELD A STALE ARG LIST.

    Install-AurumStartup.ps1 joins its own -DeskArgs into the scheduled task's action string,
    and Start-AurumDesk.ps1 treats a non-empty -DeskArgsJoined as an OVERRIDE of its own
    default. So the installer's copy wins on every boot and the supervisor's default is dead
    text -- two lists that must agree, with no mechanism making them.

    They diverged for five days: 43dd2b8 added --wake-every-bar and --universe to the
    supervisor only, so the live desk kept launching without them and reported it truthfully in
    a banner nobody was reading ("opportunity set : single read"). Nothing failed, because a
    missing capture flag is not an error -- it is less capture, silently, forever.

    Compared as SETS: order is irrelevant to argparse and pinning it would make this test fail
    on a harmless reordering, which trains people to edit the test rather than read it.
    """
    def desk_args(script: str) -> set[str]:
        src = (_ROOT / "deploy" / "windows" / script).read_text(encoding="utf-8")
        m = re.search(r"\[string\[\]\]\s*\$DeskArgs\s*=\s*@\((.*?)\)", src, re.S)
        assert m, f"could not find the -DeskArgs default in {script}"
        return set(re.findall(r'"([^"]+)"', m.group(1)))

    installer = desk_args("Install-AurumStartup.ps1")
    supervisor = desk_args("Start-AurumDesk.ps1")
    assert installer == supervisor, (
        "the installed task and the supervisor default disagree; the INSTALLER wins at boot.\n"
        f"  only in Install-AurumStartup.ps1: {sorted(installer - supervisor)}\n"
        f"  only in Start-AurumDesk.ps1:      {sorted(supervisor - installer)}")
    # Named explicitly, so deleting a capture flag from both files at once is still a visible
    # act rather than something a set-equality check would wave through.
    assert {"--wake-every-bar", "--universe"} <= installer
