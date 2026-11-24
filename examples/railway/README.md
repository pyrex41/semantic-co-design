# Railway interlocking

This fixture models two routes, `R1` and `R2`, sharing switch `S` and protected
block `B`. A controller grants a request only when the switch and block are
free. Events are serialized. `cancel` before train entry releases the route,
while cancellation after entry leaves the reservation until `clear`.

The executable source is [railway.py](railway.py), with equivalent Shen and
Maude specifications. Run the bounded oracle and the depth-six explorer with:

```sh
python3 examples/railway/railway.py examples/railway/fixtures/basic.json
python3 examples/railway/explore.py
```

The acceptance gate builds Shen and Maude residual programs through the shared
API, replays the same event-code stream through both native binaries, and
compares verdict, reservation, train occupancy, and switch position after each
event:

```sh
nix develop -c python3 examples/railway/check.py
nix develop -c python3 examples/railway/maude_explore.py
```

`maude_explore.py` runs a bounded search over serialized schedules and a
mutated rule that releases a reservation while its train is still occupying
the block. The mutant search supplies a shortest unsafe witness.

The static topology and transition rules are specialization inputs. Requests,
cancellations, entries, and clearances remain runtime events.
