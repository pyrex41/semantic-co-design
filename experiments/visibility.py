"""Recompute client and storage observation verdicts on two bounded corpora."""
from accepted_job.replay import SUBMIT, execute, specialize, violation
from observation import classify
from outbox.replay import split_publication


def retry_cases():
    programs = {"accepted": SUBMIT, "duplicate": (SUBMIT[0], ("same", "duplicate"), SUBMIT[2])}
    for name, program in programs.items():
        submit, _ = specialize(program)
        history = execute(submit, "commit", "A")
        yield name, history, violation("commit", "A", history) is None


def provider_cases():
    # The retry time is held fixed. Only the provider's receipt retention differs.
    for name, retention in (("retained", 10), ("expired", 9)):
        attempts, count = split_publication(9, retention, observe_attempts=True)
        yield name, (attempts, count), count == 1


def review(name, cases, projection, expected):
    results = list(classify(cases, projection))
    if sorted(verdict for _, verdict, _ in results) != sorted(expected):
        raise AssertionError(results)
    for observed, verdict, witnesses in results:
        print(name, observed, verdict, witnesses)


if __name__ == "__main__":
    retry = list(retry_cases())
    review("retry reply", retry, lambda h: h[1], ["unknown"])
    review("retry storage", retry, lambda h: h, ["admitted-in-corpus", "violated"])
    provider = list(provider_cases())
    review("provider attempts", provider, lambda h: h[0], ["unknown"])
    review("provider effects", provider, lambda h: h, ["admitted-in-corpus", "violated"])
