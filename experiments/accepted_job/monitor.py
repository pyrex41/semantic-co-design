"""Specialize a finite trace-language interpreter into a prefix transition table."""
from itertools import product

from replay import violation


def allowed_histories():
    # Contract domain: one tenant/key, IDs 1 or 2, observed storage after restart.
    for outcome, digest, job in product(("commit", "rollback"), "AB", (1, 2)):
        if outcome == "commit":
            rows = ((job, "A"),)
            reply = ("ok", job) if digest == "A" else ("conflict", None)
            yield (outcome, digest, rows, reply, rows)
        else:
            yield (outcome, digest, (), ("ok", job), ((job, digest),))


def interpret(language, prefix):
    if prefix in language:
        return "admitted"
    if any(trace[:len(prefix)] == prefix for trace in language):
        return "possible"
    return "violated"


def specialize(language):
    transitions = [{}]
    accepting = set()
    for trace in language:
        state = 0
        for observation in trace:
            if observation not in transitions[state]:
                transitions[state][observation] = len(transitions)
                transitions.append({})
            state = transitions[state][observation]
        accepting.add(state)

    def monitor(prefix):
        state = 0
        for observation in prefix:
            state = transitions[state].get(observation)
            if state is None:
                return "violated"
        return "admitted" if state in accepting else "possible"
    return monitor


def main():
    language = tuple(allowed_histories())
    monitor = specialize(language)
    rows = ((), ((1, "A"),), ((2, "A"),), ((1, "B"),), ((2, "B"),),
            ((1, "A"), (2, "A")))
    replies = (("ok", 1), ("ok", 2), ("conflict", None), ("declined", None))
    prefixes = set()
    complete = 0
    for trace in product(("commit", "rollback"), "AB", rows, replies, rows):
        outcome, digest, before, reply, after = trace
        expected = violation(outcome, digest, (before, reply, after)) is None
        if (monitor(trace) == "admitted") != expected:
            raise AssertionError(trace)
        complete += 1
        prefixes.update(trace[:length] for length in range(len(trace) + 1))
    counts = {}
    for prefix in prefixes:
        verdict = monitor(prefix)
        if verdict != interpret(language, prefix):
            raise AssertionError(prefix)
        counts[verdict] = counts.get(verdict, 0) + 1
    print(f"{complete} complete traces checked independently; {len(prefixes)} prefixes: {counts}")


if __name__ == "__main__":
    main()
