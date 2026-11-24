# Railway interlocking

Two routes share one switch and one protected block. The layout fixes the switch position required by each route. The controller tracks the reservation, the route of the physical train, and the switch position separately. Cancelling before entry releases the reservation. After entry the train must clear the block. The switch stays in its last position until the next grant.

Each input line contains serialized events. Codes 0–3 mean request, cancel, enter and clear for route 1. Add four for route 2. Each event emits four integers: verdict (0 rejected, 1 granted, 2 cancelled, 3 entered, 4 cleared), reservation, occupying route, and switch position. Zero denotes an empty reservation or block, or the initial switch position.

Run `python3 -m examples.railway.check` in the Nix shell. The Maude search module applies the event equations from the actual build source. Breadth-first search through depth six reaches seven controller states and the check replays all 56 state/event edges. Removing the occupancy test from cancellation reaches 13 states. The shortest conflicting grant is request 1, enter 1, cancel 1, request 2. Both rebuilt mutants report route 1 still in the block with the switch set for route 2.
