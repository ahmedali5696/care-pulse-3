"""Profile one existing Ordinary pytest invocation in a private disposable runner."""

import cProfile
import inspect
import json
import sys
from collections import defaultdict
from pathlib import Path
from time import perf_counter

import pytest


class OrdinaryProfile:
    def __init__(self):
        self.selected = 0
        self.cases = {}
        self.fixture_setup = defaultdict(list)
        self.generator_names = {}
        self.generator_teardown = defaultdict(float)

    def pytest_collection_finish(self, session):
        self.selected = len(session.items)

    @pytest.hookimpl(wrapper=True)
    def pytest_fixture_setup(self, fixturedef):
        if inspect.isgeneratorfunction(fixturedef.func):
            self.generator_names[fixturedef.func.__code__] = fixturedef.argname
        start = perf_counter()
        try:
            return (yield)
        finally:
            self.fixture_setup[fixturedef.argname].append(perf_counter() - start)

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_teardown(self):
        # Generator resumption includes its finally block. Registered callbacks
        # outside the generator remain in the ordinary per-case teardown time.
        timer = cProfile.Profile()
        try:
            with timer:
                return (yield)
        finally:
            for entry in timer.getstats():
                name = self.generator_names.get(entry.code)
                if name:
                    self.generator_teardown[name] += entry.totaltime

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_makereport(self, item, call):
        report = yield
        # The ordinal distinguishes parameter cases without exporting parameter IDs.
        case = self.cases.setdefault(
            item, {"file": item.path.name, "test": item.originalname, "phases": {}}
        )
        case["phases"][call.when] = {
            "seconds": round(report.duration, 6),
            "outcome": "xfail-marked"
            if hasattr(report, "wasxfail")
            else report.outcome,
        }
        return report

    def complete(self):
        return (
            self.selected > 0
            and len(self.cases) == self.selected
            and all(
                set(case["phases"]) == {"setup", "call", "teardown"}
                and all(
                    phase["outcome"] == "passed" for phase in case["phases"].values()
                )
                for case in self.cases.values()
            )
        )


def main(output, args):
    profile = OrdinaryProfile()
    with output.open("x", encoding="utf-8") as stream:
        status = pytest.main(args, plugins=[profile])
        json.dump(
            {
                "kind": "PROFILING_ONLY_NOT_ACCEPTANCE",
                "pytest_exit": int(status),
                "selected": profile.selected,
                "cases_in_execution_order": list(profile.cases.values()),
                "fixture_setup": {
                    name: {"count": len(times), "seconds": round(sum(times), 6)}
                    for name, times in sorted(profile.fixture_setup.items())
                },
                "generator_teardown_seconds": {
                    name: round(seconds, 6)
                    for name, seconds in sorted(profile.generator_teardown.items())
                },
            },
            stream,
        )
    return int(status) or int(not profile.complete())


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit(
            "usage: python -m ops.profile_ordinary PRIVATE_JSON PYTEST_ARGS"
        )
    raise SystemExit(main(Path(sys.argv[1]), sys.argv[2:]))
