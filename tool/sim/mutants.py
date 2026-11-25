"""Deliberately wrong simulators. Each breaks one rule of tool/sim's environment.

They exist so that the checks of the simulator have teeth: a check that claims to depend
on a rule must reject the world that breaks it (tool/sim_check.py), and an application
whose safety rests on a rule can show that it does (examples/accepted_job/check.py).
"""
from . import World


class AllEffectsOnCut(World):
    """A node that "dies between two effects" still performs every effect of the step."""

    def performed(self, effects, cut):
        return effects


class LateCut(World):
    """The node dies one effect later than the trace says."""

    def performed(self, effects, cut):
        return effects if cut is None else effects[:cut + 1]


class NoMidStepCrash(World):
    """A node only ever dies between steps."""
    mid_step_crashes = False


class SyncsNewest(World):
    """A sync makes the newest pending write durable, not the oldest."""

    def next_write(self, node):
        return -1


class PersistsSuffix(World):
    """At a crash the newest pending writes reach the disk, not the oldest."""

    def persisted(self, pending, kept):
        return pending[len(pending) - kept:], pending[:len(pending) - kept]


class KeepsCompletions(World):
    """Acknowledgements, read results and timers queued for a node survive its crash."""

    def forget(self, node):
        pass


class FaultsTakeTime(World):
    """Dropping or duplicating a message in flight advances the clock to its due time."""

    def take(self, items, content, *, happens=True):
        return super().take(items, content)


class ExactTimers(World):
    """Timers run for exactly the delay asked for: no clock rate, no drawn skew."""

    def timer_delay(self, node, delay, draw):
        return delay


class OnewayCutsBoth(World):
    """A one-way cut isolates the node in both directions, like a partition."""

    def apply(self, action, draw):
        rec = super().apply(action, draw)
        if action[0] == "oneway":
            self.blocked |= {(b, a) for a, b in self.blocked}
        return rec


class IgnoresTimerBudget(World):
    """Timer events are delivered however many the profile allows."""

    def spent_timer(self, event):
        return False


class NeverSlow(World):
    """No message is late and no disk stalls, whatever share the profile's `slow` key asks for."""

    def wait(self, kind, draw):
        return draw.delay(kind)
