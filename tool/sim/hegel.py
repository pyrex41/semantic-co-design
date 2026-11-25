"""Hegel as the source of every simulator choice.

The seeded schedule of tool/sim asks a random.Random three kinds of question:
which enabled action is next, what integer an action draws, and (by running to
quiescence) when to stop. Here a Hegel test case answers all three, so Hegel
generates the schedule, the faults and the workload, and its shrinker minimizes
a failing run. What comes out is still a sim-trace/1 record: it replays through
sim.replay with no Hegel in sight.

    HegelDraw                 the integers inside an action
    pick(tc, world, actions)  the next action, with the rates of sim.pick
    case(tc, ...)             one schedule; returns the trace and the action that trapped
    falsify(...)              N cases against Shen invariants; the shrunk trace
    replay(...)               sim.replay, and the trapping step of a trace that ends in an error

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

from . import Divergence, Draw, SimError, dumps, record, script, start
from . import replay as replay_steps

STEP = 0x73696D2D73746570            # span label of one schedule step
MORE_RATE = 0.98                     # chance per step that the schedule goes on
REORDER_RATE = 0.05                  # chance per step that something queued overtakes what is due
FAULTS = ("crash", "drop", "dup", "partition", "heal")
QUEUED = ("deliver", "local", "sync", "output")
TRAP = "sim.error"                   # the failure name of a trap or an invalid effect
# Every Hegel setting but test_cases, seed and phases. Hegel resolves a setting it is not
# given from HEGEL_* variables, a hegel.toml and CI detection, so none is left to it.
PINNED = {
    "derandomize": False, "database": "", "backend": "default",
    "stop_on_first_failure": True, "report_multiple_failures": False,
    "nondeterminism_strictness": "quiet", "unbounded_choices": False,
    "verbosity": "quiet", "show_statistics": False, "print_blob": False,
    # too_slow is a wall-clock judgement; a loaded machine must not change the verdict.
    "suppress_health_check": ["too_slow"],
}


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
    "error: ..." or, when Hegel chose to stop early, with no end (as a scripted trace).

    Returns (trace, trap). A sim-trace/1 record has no step for an action that raised a
    SimError, so `trap` is that [action, draws], or None when the trace has no error."""
    world, trace = start(native, workload, prof, inits, None)
    trap = None
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
        trap = [action, draw.used]
    return json.loads(dumps(trace)), json.loads(json.dumps(trap))


def replay(native, workload, inits, trace, trap=None):
    """sim.replay, and for a trace that ends in an error the step that trapped as well.

    sim.replay re-executes the recorded steps, which for an error trace are only the steps
    before the trap, and takes the recorded `end` on trust. With `trap` (the [action, draws]
    that falsify returns) the trapping action is applied after them and must raise the
    recorded error. Raises sim.Divergence otherwise."""
    errored = (trace["end"] or "").startswith("error: ")
    if errored != (trap is not None):
        raise Divergence("a trap goes with a trace that ends in an error, and only with one")
    again = replay_steps(native, workload, inits, trace)
    if trap is not None:
        actions = [(s["action"], s["draws"]) for s in trace["steps"] if s["action"] != ["boot"]]
        again = script(native, workload, trace["profile"], inits, actions + [tuple(trap)],
                       seed=trace["seed"])
        if dumps(again) != dumps(trace):
            raise Divergence(f"the trapping step ends {again['end']!r}, not {trace['end']!r}")
    return again


class Violation(AssertionError):
    """A generated schedule failed; carries the trace out of the Hegel run."""

    def __init__(self, trace, trap):
        super().__init__(trace["end"] or "an invariant is violated")
        self.trace, self.trap = trace, trap


def falsify(native, workload, prof, inits, *, invariants=None, traps=True, cases=100, seed=0,
            shrink=True):
    """Run `cases` Hegel-generated schedules against the compiled step and Shen invariants.

    `invariants` names the manifest invariants that must hold (default: all of them). A
    case fails when one of them is violated or, unless `traps` is false, when the run ends
    in a SimError (the failure named TRAP). The first failing case fixes the failure that
    is pursued: its trap, or the first of its violated invariants in manifest order. From
    then on a run fails only by that same failure, so Hegel's shrinker cannot wander from
    one bug to another and the first and the shrunk trace are the same failure.
    `shrink=False` leaves Hegel's shrink phase out and returns the first failure.

    Returns
      "failure"      the name pursued, or None when no case failed
      "trace"        the shrunk failing trace, or None
      "trap"         for TRAP the [action, draws] that trapped after the trace (see replay)
      "violated"     every wanted invariant the shrunk trace violates ([TRAP] for a trap)
      "first"        the failing trace as first generated, before any shrinking
      "cases"        test cases run up to the first failure (all of them when none fails)
      "exhausted"    no failure, and Hegel ran out of schedules before `cases` of them
      "trapped"      schedules that ended in a SimError while `traps` was false
      "executions"   every schedule run, shrinking included
      "evaluations"  distinct step sequences shen-jit judged
      "seconds"      wall time

    Everything but "seconds" is a function of the arguments: no example database is read
    or written, and no HEGEL_* variable, hegel.toml or CI detection reaches the run.
    """
    from .. import check                # tool.check imports tool.sim
    if cases < 1:
        raise ValueError(f"cases must be at least 1, not {cases}")
    schema = native.schema
    declared = schema.invariants
    wanted = declared["state"] + declared["trace"] if invariants is None else list(invariants)
    unknown = [n for n in wanted if n not in declared["state"] + declared["trace"]]
    if unknown:
        raise ValueError(f"not invariants of {schema.name}: {unknown}")
    state = [n for n in declared["state"] if n in wanted]
    over_trace = [n for n in declared["trace"] if n in wanted]
    counts = {"executions": 0, "cases": None, "trapped": 0}
    verdicts = {}
    target = []                         # the failure pursued, once a case has failed

    def failures(trace):
        if (trace["end"] or "").startswith("error: "):
            counts["trapped"] += not traps
            return [TRAP] if traps else []
        # The invariants see no times and no draws, and many choices give the same steps.
        seen = json.dumps(check.shen_trace(trace))
        if seen not in verdicts:
            bad, = check.invariants(schema, [trace], state=state, trace=over_trace)
            verdicts[seen] = [name for name in state + over_trace if name in dict(bad)]
        return verdicts[seen]

    def test(tc):
        counts["executions"] += 1
        trace, trap = case(tc, native, workload, prof, inits)
        names = failures(trace)
        if names and not target:
            target.append(names[0])
            counts["cases"], counts["first"] = counts["executions"], trace
        if target and target[0] in names:
            raise Violation(trace, trap)

    settings = dict(PINNED, test_cases=cases, seed=seed,
                    phases=[p for p in hegel.Phase if shrink or p is not hegel.Phase.shrink])
    unset = set(hegel.Settings.__annotations__) - {"_values"} - set(settings)
    if unset:
        raise RuntimeError(f"hegel settings left to the environment: {sorted(unset)}")
    began = time.monotonic()
    found = None
    try:
        hegel.run(test, **settings)     # no database_key: failures are neither saved nor reused
    except Violation as violation:
        found = violation
    return {"failure": target[0] if found else None,
            "trace": found and found.trace, "trap": found and found.trap,
            "violated": failures(found.trace) if found else [],
            "first": counts.get("first"),
            "cases": counts["cases"] or counts["executions"],
            "exhausted": found is None and counts["executions"] < cases,
            "trapped": counts["trapped"],
            "executions": counts["executions"], "evaluations": len(verdicts),
            "seconds": time.monotonic() - began}
