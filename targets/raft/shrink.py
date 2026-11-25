"""Shrinking long timed traces.

`tool.check.shrink` removes one step at a time and gives up on a candidate as soon as a
later recorded action is no longer enabled. On a trace of a thousand steps that is too
slow and too strict: remove one delivery and every step that depended on it is invalid.

Here a candidate is a list of (action, draws) run *leniently*: an action that is not
enabled when its turn comes, or whose recorded draws no longer fit, is skipped. What
remains is a valid schedule. Chunks are removed largest first (delta debugging), then
single steps. The result is re-run through `sim.script`, so it is an ordinary scripted
trace that `sim.replay` verifies.
"""
import json

from tool import sim


def lenient(native, workload, prof, actions, world=sim.World):
    """Run the actions that are enabled when their turn comes.

    Returns (the actions kept, with the draws they really used; the trace they give).
    Stops at the first failure of the specification.
    """
    world, trace = sim.start(native, workload, prof, None, world)
    kept = []
    for action, draws in actions:
        action = sim.freeze(action)
        if action not in world.enabled():
            continue
        trial, draw = world.clone(), sim.ScriptDraw(draws, prof["timed"])
        try:
            rec = trial.apply(action, draw)
        except sim.Divergence:
            continue
        except sim.SimError as error:
            rec = {"kind": "error", "error": str(error)}
            trace["end"] = f"error: {error}"
        else:
            world = trial
        kept.append((action, list(draw.used)))
        trace["steps"].append(sim.record(world, len(trace["steps"]), action, list(draw.used), rec))
        if trace["end"] is not None:
            break
    if trace["end"] is None:
        trace["end"] = "quiescent" if sim.idle(world) else "open"
    return kept, json.loads(sim.dumps(trace))


def actions_of(trace):
    return [(s["action"], s["draws"]) for s in trace["steps"] if s["action"] != ["boot"]]


def shrink(native, workload, trace, fails, *, budget=3000):
    """A shorter scripted trace on which `fails(trace)` is still true.

    `fails` takes one scripted trace and returns a boolean. `budget` bounds the number of
    candidates tried. Returns (trace, candidates tried).
    """
    prof = trace["profile"]
    tried = 0

    def attempt(candidate):
        nonlocal tried
        tried += 1
        kept, result = lenient(native, workload, prof, candidate)
        return kept if fails(result) else None

    actions = attempt(actions_of(trace))
    if actions is None:
        raise ValueError("the trace does not fail when it is run as a script")
    # The shortest failing prefix, by bisection (a safety violation stays violated).
    low, high = 0, len(actions)
    while low < high and tried < budget:
        middle = (low + high) // 2
        if attempt(actions[:middle]) is not None:
            high = middle
        else:
            low = middle + 1
    actions = actions[:high]
    size = max(len(actions) // 2, 1)
    while tried < budget:
        start, removed = 0, False
        while start < len(actions) and tried < budget:
            found = attempt(actions[:start] + actions[start + size:])
            if found is not None and len(found) < len(actions):
                actions, removed = found, True
            else:
                start += size
        if size > 1:
            size //= 2
        elif not removed:
            break
    result = sim.script(native, workload, prof, actions)
    if not fails(result):
        raise ValueError("the shrunk trace does not fail when re-run as a script")
    return result, tried
