"""The guard against accidentally faking the result.

Leaking the true salary date into the planner is the single easiest way to produce a
number that looks great and means nothing. These tests are structural: they fail on
import graphs and source text, so they catch the leak before it can ever run.
"""

from __future__ import annotations

import ast
import importlib
import pkgutil
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "app"

#: Modules that make decisions. None of them may see the simulator.
AGENT_PACKAGES = ("app.plan", "app.diagnose", "app.propose", "app.policy")


def _modules_under(package: str) -> list[str]:
    path = APP / package.split(".", 1)[1]
    if not path.exists():
        return []
    found = [package] if (path / "__init__.py").exists() else []
    found += [f"{package}.{m.name}" for m in pkgutil.iter_modules([str(path)])]
    return found


def _imports_in(source: str) -> set[str]:
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _transitive_imports(module: str, seen: set[str] | None = None) -> set[str]:
    seen = seen if seen is not None else set()
    if module in seen:
        return seen
    seen.add(module)
    try:
        mod = importlib.import_module(module)
    except ModuleNotFoundError:
        return seen
    source_file = getattr(mod, "__file__", None)
    if not source_file or not source_file.endswith(".py"):
        return seen
    for name in _imports_in(Path(source_file).read_text(encoding="utf-8")):
        if name.startswith("app."):
            _transitive_imports(name, seen)
    return seen


def test_agent_cannot_see_simulator():
    """docs/03-SIMULATOR.md. The planner must infer the inflow phase from observable
    history, never read `latent.inflow_day`."""
    for package in AGENT_PACKAGES:
        for module in _modules_under(package):
            reached = _transitive_imports(module)
            leaked = {m for m in reached if m.startswith("app.sim")}
            assert not leaked, f"{module} transitively imports {leaked}"


#: Ways an agent module could reach latent truth. Matched against code, not prose — a
#: docstring saying "never latent truth" is the opposite of a violation.
LATENT_ACCESS = ("latent_truth", ".latent[", ".latent.", "world.latent",
                 ".inflow_day", "burn_rate", "dispute_prone", "latent.balance")


def code_only(source: str) -> str:
    """Executable code with every comment and string literal removed.

    Prose is not a violation. These modules necessarily *discuss* latent truth — "nothing
    here may read latent.hardship", "scored against latent_truth in the evaluator" — and a
    docstring saying so is the opposite of the thing being guarded against. Matching on
    raw text made this check fire on its own warnings, twice.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            node.value = ""
    return ast.unparse(tree)


def test_agent_never_queries_latent_truth():
    """The latent table sits in the same database the agent reads, so an import check
    alone would not catch a raw SQL query. Check for access patterns in the code too."""
    offenders = []
    for package in AGENT_PACKAGES:
        directory = APP / package.split(".", 1)[1]
        for path in directory.rglob("*.py") if directory.exists() else []:
            code = code_only(path.read_text(encoding="utf-8"))
            for pattern in LATENT_ACCESS:
                if pattern in code:
                    offenders.append(f"{path.name}: {pattern}")
    assert not offenders, f"agent modules reaching latent state: {offenders}"


def test_simulator_may_import_domain():
    """The dependency runs one way only, and that way must actually work."""
    reached = _transitive_imports("app.sim.world")
    assert "app.domain.models" in reached
    assert not any(m.startswith(AGENT_PACKAGES) for m in reached)


#: `provisional_gate` mints an unconditional ALLOW. M5 removed it from the runner, which
#: now calls `app.policy.evaluate`; what remains is a simulator-only fixture used by the
#: rail tests to exercise the adapter without standing up a full policy context. This set
#: must never grow again, and an agent module must never appear in it.
PROVISIONAL_GATE_USERS = {"app/sim/world.py"}


def test_provisional_gate_has_not_spread():
    hits = {p.relative_to(APP.parent).as_posix() for p in APP.rglob("*.py")
            if "provisional_gate" in p.read_text(encoding="utf-8")}
    assert hits == PROVISIONAL_GATE_USERS, (
        f"unexpected users of provisional_gate: {hits ^ PROVISIONAL_GATE_USERS}. "
        "It mints unconditional ALLOWs; M5 must remove it from the runner, not add users.")


def test_provisional_gate_is_unreachable_from_agent_modules():
    for package in AGENT_PACKAGES:
        directory = APP / package.split(".", 1)[1]
        for path in directory.rglob("*.py") if directory.exists() else []:
            assert "provisional_gate" not in path.read_text(encoding="utf-8"), path


def test_nothing_outside_the_clock_module_reads_real_time():
    """CLAUDE.md rule 6: one Clock, simulated IST. `wall_clock()` in clock.py is the
    single permitted reader, and only the ledger's provenance field consumes it."""
    offenders = []
    for path in APP.rglob("*.py"):
        if path.name == "clock.py":
            continue
        source = path.read_text(encoding="utf-8")
        if "datetime.now(" in source or "date.today(" in source:
            offenders.append(path)
    assert not offenders, f"real-time reads outside clock.py: {offenders}"


def test_no_float_money_fields():
    """CLAUDE.md rule 7: money is integer paise. A `_paise` field typed float is a bug."""
    offenders = []
    for path in APP.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
                    and node.target.id.endswith("_paise")
                    and ast.unparse(node.annotation).startswith("float")):
                offenders.append(f"{path}:{node.lineno} {node.target.id}")
    assert not offenders, offenders
