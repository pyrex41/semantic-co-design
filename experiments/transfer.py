"""Measure the incremental monitor code for the split-provider experiment."""
import inspect
from itertools import product

from observation import classify
from outbox.replay import split_publication


def adapted(records, read_provider):
    cases = ((name, (calls, count), count == 1) for name, calls, count in records)
    observe = (lambda h: h) if read_provider else (lambda h: h[0])
    return list(classify(cases, observe))


def direct(records, read_provider):
    groups = {}
    for name, calls, count in records:
        observed = (calls, count) if read_provider else calls
        witnesses = groups.setdefault(observed, {"admitted": [], "violated": []})
        witnesses["admitted" if count == 1 else "violated"].append(name)
    results = []
    for observed, witnesses in groups.items():
        if not witnesses["violated"]:
            verdict = "admitted-in-corpus"
        elif not witnesses["admitted"]:
            verdict = "violated"
        else:
            verdict = "unknown"
        results.append((observed, verdict, witnesses))
    return results


def lines(function):
    return sum(bool(line.strip()) for line in inspect.getsource(function).splitlines())


def main():
    records = []
    for tick, retention in product((9, 10, 11), (None, 10, 11)):
        calls, count = split_publication(tick, retention, observe_attempts=True)
        expected_count = 1 if retention is not None and tick < retention else 2
        if count != expected_count:
            raise AssertionError((tick, retention, count))
        records.append(((tick, retention), calls, count))
    for read_provider in (False, True):
        reused = adapted(records, read_provider)
        if reused != direct(records, read_provider):
            raise AssertionError("verdict or supporting witness differs")
        print("provider-visible", read_provider, [(obs, verdict) for obs, verdict, _ in reused])
    # Expand the environment while keeping the worker-visible history fixed.
    calls, count = split_publication(11, 12, observe_attempts=True)
    extended = records + [((11, 12), calls, count)]
    def verdict_for(rows):
        return next(verdict for obs, verdict, _ in rows if obs == calls)
    if verdict_for(adapted(records, False)) != "violated" or count != 1:
        raise AssertionError("unexpected tick 11 control")
    if adapted(extended, False) != direct(extended, False) or verdict_for(direct(extended, False)) != "unknown":
        raise AssertionError("extending provider retention must invalidate the earlier verdict")
    print("tick 11 with retention 12 added: unknown")
    print(f"incremental nonblank source lines: adapter {lines(adapted)}, direct {lines(direct)}")


if __name__ == "__main__":
    main()
