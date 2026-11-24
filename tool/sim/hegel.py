"""Hegel as the source of every simulator choice.

The seeded schedule of tool/sim asks a random.Random three kinds of question:
which enabled action is next, what integer an action draws, and (by running to
quiescence) when to stop. Here a Hegel test case answers all three, so Hegel
generates the schedule, the faults and the workload, and its shrinker minimizes
a failing run. What comes out is still a sim-trace/1 record: it replays through
sim.replay with no Hegel in sight.

    HegelDraw                 the integers inside an action
    pick(tc, world, actions)  the next action, with the rates of sim.pick
    case(tc, ...)             one schedule; returns the trace
    falsify(...)              N cases against Shen invariants; the shrunk trace

Every question is asked so that the simplest answer (false, 0, the lowest
value) is the quiet one: stop here, no fault, no crash inside the step, nothing
persisted, the shortest delay. Shrinking therefore moves a failing run toward
the short fault-free schedule in time order.

The core simulator does not import this module; only it imports hegel
(PyPI hegeltest, the official Python frontend over libhegel, see nix/hegel.nix).
"""
import json
import time

import hegel
from hegel import generators as gs

from . import Draw, SimError, dumps, record, start

STEP = 0x73696D2D73746570            # span label of one schedule step
MORE_RATE = 0.98                     # chance per step that the schedule goes on
REORDER_RATE = 0.05                  # chance per step that something queued overtakes what is due
FAULTS = ("crash", "drop", "dup", "partition", "heal")
QUEUED = ("deliver", "local", "sync", "output")
TRAP = "sim.error"                   # the failure name of a trap or an invalid effect


def flag(tc, rate):
    return tc.draw_silent(gs.weighted_booleans(rate))


def below(tc, n):
    return tc.draw_silent(gs.integers(0, n - 1)) if n > 1 else 0


class HegelDraw(Draw):
    """Integers drawn inside an action, each one a choice of a Hegel test case."""

    def __init__(self, tc, profile):
        super().__init__()
        self.tc, self.profile = tc, profile

    def choose(self, label, n):
        if label == "crash-after":      # 0 is "no crash"
            crash = n > 1 and flag(self.tc, self.profile["crash_mid_rate"])
            value = 1 + below(self.tc, n - 1) if crash else 0
        else:
            value = below(self.tc, n)
        self.used.append(value)
        return value

    def delay(self, kind):
        if not self.profile["timed"]:
            return 0
        low, high = self.profile["delay"][kind]
        value = self.tc.draw_silent(gs.integers(low, high))
        self.used.append(value)
        return value


def pick(tc, world, actions):
    """sim.pick with Hegel answering: a fault, a restart or the workload at the profile's
    rates, otherwise whatever is due first. None when nothing is left to do.

    One question more than sim.pick: any queued item may overtake the one that is due, as
    it may in a scripted trace. Without it no schedule could leave a due item undelivered.
    """
    prof = world.profile
    for kinds, rate in ((FAULTS, prof["fault_rate"]), (("restart",), prof["restart_rate"]),
                        (("client",), prof["client_rate"]), (QUEUED, REORDER_RATE)):
        some = [a for a in actions if a[0] in kinds]
        if len(some) > (kinds is QUEUED) and flag(tc, rate):
            return some[below(tc, len(some))]
    due = world.earliest()
    if due is not None:
        return due
    for kind in ("client", "restart"):
        some = [a for a in actions if a[0] == kind]
        if some:
            return some[below(tc, len(some))]
    return None


def case(tc, native, workload, prof, inits):
    """One schedule, every choice drawn from `tc`. The trace ends "quiescent", "max_steps",
    "error: ..." or, when Hegel chose to stop early, with no end (as a scripted trace)."""
    world, trace = start(native, workload, prof, inits, None)
    try:
        while len(trace["steps"]) < prof["max_steps"]:
            # One span per step, with the decision to go on inside it, so the shrinker
            # can delete a step whole.
            tc.start_span(STEP)
            try:
                if not flag(tc, MORE_RATE):
                    break
                action = pick(tc, world, world.enabled())
                if action is None:
                    trace["end"] = "quiescent"
                    break
                draw = HegelDraw(tc, prof)
                rec = world.apply(action, draw)
                trace["steps"].append(record(world, len(trace["steps"]), action, draw.used, rec))
            finally:
                tc.stop_span()
        else:
            trace["end"] = "max_steps"
    except SimError as error:
        trace["end"] = f"error: {error}"
    return json.loads(dumps(trace))


class Violation(AssertionError):
    """A generated schedule failed; carries the trace out of the Hegel run."""

    def __init__(self, names, trace):
        super().__init__(", ".join(names))
        self.names, self.trace = names, trace


def falsify(native, workload, prof, inits, *, invariants=None, cases=100, seed=0, database=""):
    """Run `cases` Hegel-generated schedules against the compiled step and Shen invariants.

    `invariants` names the manifest invariants that must hold (default: all of them). A
    case fails when one of them is violated or the run ends in a SimError. On failure
    Hegel shrinks the choices and the result carries the trace of the shrunk case.

    Returns {"trace": shrunk trace or None, "violated": names, "first": the failing trace
    as first generated, before any shrinking, "cases": test cases run
    before the first failure (all of them when none fails), "executions": every schedule
    run including shrinking, "evaluations": distinct step sequences shen-jit judged,
    "seconds": wall time}. With a fixed `seed` and the
    example database off (the default) the result is a function of the arguments.
    """
    from .. import check                # tool.check imports tool.sim
    schema = native.schema
    declared = schema.invariants
    wanted = declared["state"] + declared["trace"] if invariants is None else list(invariants)
    unknown = [n for n in wanted if n not in declared["state"] + declared["trace"]]
    if unknown:
        raise ValueError(f"not invariants of {schema.name}: {unknown}")
    state = [n for n in declared["state"] if n in wanted]
    over_trace = [n for n in declared["trace"] if n in wanted]
    counts = {"executions": 0, "cases": None}
    verdicts = {}

    def failures(trace):
        if (trace["end"] or "").startswith("error: "):
            return [TRAP]
        # The invariants see no times and no draws, and many choices give the same steps.
        seen = json.dumps(check.shen_trace(trace))
        if seen not in verdicts:
            bad, = check.invariants(schema, [trace], state=state, trace=over_trace)
            verdicts[seen] = [name for name, _ in bad]
        return verdicts[seen]

    def test(tc):
        counts["executions"] += 1
        trace = case(tc, native, workload, prof, inits)
        names = failures(trace)
        if names:
            if counts["cases"] is None:
                counts["cases"], counts["first"] = counts["executions"], trace
            raise Violation(names, trace)

    began = time.monotonic()
    found = None
    try:
        # too_slow is a wall-clock judgement; a loaded machine must not change the verdict.
        hegel.run(test, test_cases=cases, seed=seed, database=database, verbosity="quiet",
                  print_blob=False, suppress_health_check=[hegel.HealthCheck.too_slow])
    except Violation as violation:
        found = violation
    return {"trace": found and found.trace, "violated": found.names if found else [],
            "first": counts.get("first"),
            "cases": counts["cases"] or counts["executions"],
            "executions": counts["executions"], "evaluations": len(verdicts),
            "seconds": time.monotonic() - began}
