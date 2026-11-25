# tool: one Shen specification, compiled, simulated and explored

A developer writes the behavior of a node as ordinary Shen. This package

1. specializes that Shen to a native step function ([compile.py](compile.py), [pe.py](pe.py)),
2. runs the same native code under a deterministic simulator that owns the clock, the network, the disks, crashes and the schedule ([sim/](sim/)),
3. checks every simulated step against the Shen source in shen-jit and evaluates invariants written in Shen over the traces ([check.py](check.py)),
4. translates the same Shen source to Maude and searches the same environment exhaustively within a bound ([maude.py](maude.py)),
5. runs the same code, built as an import-free WebAssembly module ([wasm.py](wasm.py)), as real OS processes with real sockets, files and clock, and checks what the processes logged against the same Shen source and the same invariants ([host/](host/)).
6. packages the node with its host as a NixOS service and runs it on virtual machines, where the process kill, the power loss, the partition and the packet loss are real ones ([../nix/vm.nix](../nix/vm.nix)).

```sh
nix develop -c python3 -m tool check kernel         # the compiler's own checks
nix develop -c python3 -m tool check accepted_job   # the first application
nix develop -c python3 -m tool real accepted_job    # the same application as killed and restarted processes
nix build .#checks.x86_64-linux.real-vm -L          # the same application on NixOS machines (Linux with KVM)
```

## 1. Specification convention

A node is one pure Shen function and one initial value.

```shen
(define <init>  NodeId NodeCount -> State)
(define <step>  State Event -> [State' Effects])
```

* **State** is a list of fixed shape whose leaves are numbers, symbols or booleans. The shape never changes at run time. A bounded collection is a list with a declared number of slots; the specification keeps its own occupancy marks (a status symbol or a count) inside the slots.
* **Event** is a tagged list `[tag field ...]`. Everything nondeterministic reaches the node only as an event: a client request, a message arrival, the acknowledgement of a disk write, the result of a disk read, a timer, a clock reading, a restart.
* **Effects** is a list of tagged lists `[tag field ...]`, at most `max_effects` long. Every external action leaves the node only as an effect: send, reply, disk write, disk read, set or cancel a timer, read the clock.
* **Durable state** is what the node asked the disk to write and the disk has made durable. An acknowledgement event tells the node a write is durable; before that event the node may not rely on it. Memory is lost at a crash: the simulator replaces the state with `<init>` and delivers the `restart` event, and the specification says there what it reads back.
* The specification is ordinary Shen that computes values. It never mentions residual code, the simulator, or integers standing for symbols. `shen-jit` runs the same file unchanged, and that run is the reference for the native code.

The step must be total on every state and event the system can reach. A Shen error (no clause matches, `simple-error`, division by zero) is a *trap*: the native step returns an error code, and the simulator reports it as a failure of the run.

### The manifest

`node.json` sits next to the specification. The compiler and the simulator both read it, so the integer meaning of a symbol, an event or a state field is declared once.

```json
{
  "name": "accepted_job",
  "spec": "spec.shen", "step": "job.step", "init": "job.init",
  "symbols": ["unknown", "free", "stored", "idle", "writing", "ok", "conflict", "full"],
  "state": [{"capacity": 2, "of": ["sym", "num", "num"]},
            ["sym", "num", "num", "num", "num", "num"],
            "num"],
  "events":  {"submit":  {"role": "request",    "fields": ["num", "num", "num"]},
              "written": {"role": "disk-ack",   "fields": ["num"]}},
  "effects": {"write":   {"role": "disk-write", "fields": ["num", "num", "num", "num"]},
              "reply":   {"role": "output",     "fields": ["num", "sym", "num"]}},
  "max_effects": 2,
  "invariants": {"file": "invariants.shen", "state": ["inv.a"], "trace": ["inv.b"]}
}
```

A shape is `"num"`, `"sym"`, `"bool"`, a list of shapes, or `{"capacity": n, "of": shape}` for `n` slots. `symbols` lists every symbol that can be a runtime value; event and effect tags are appended automatically. Symbol codes are the 1-based positions in that table, so zero padding is never a symbol.

### Roles

The simulator knows nothing about an application. It acts on the *role* of each effect and produces events by role. The leading fields of an effect or event with a role are fixed; anything after them is the application's.

| Effect role | Fields | What the simulator does |
|---|---|---|
| `output` | any | A message to the outside world. It may be delayed, dropped or duplicated before the client sees it. |
| `send` (`"deliver": tag`) | `Dest`, payload | Queues the `recv` event `[tag Src payload]` for node `Dest`. May be delayed, dropped, duplicated, reordered or cut by a partition. |
| `disk-write` | `Token`, `Key`, value | Appends to the node's pending writes. A later *sync* makes the row durable and queues `[ack Token]`. |
| `disk-read` | `Token`, `Key` | Queues `[result Token Key Found value]` from the durable rows (`Found` is 1 or 0; a missing row reads as zeros). Pending writes are not visible to reads. |
| `timer-set` | `Id`, `Delay` | Replaces any pending timer `Id` and queues `[timer Id]` at clock + `Delay`. |
| `timer-cancel` | `Id` | Removes pending timer `Id`. |
| `clock-read` | `Token` | Queues `[clock Token Now]`. |

| Event role | Fields | Source |
|---|---|---|
| `request` | any | The workload. |
| `recv` | `Src`, payload | A `send` effect. |
| `disk-ack` | `Token` | A synced write. Dropped if the node crashed first. |
| `disk-read-result` | `Token`, `Key`, `Found`, value | A `disk-read` effect. |
| `timer` | `Id` | A `timer-set` effect. |
| `clock` | `Token`, `Now` | A `clock-read` effect. |
| `restart` | none | Delivered first after boot and after every crash. |

## 2. Compiler

`pe.py` is the partial evaluator that was `experiments/branching_vm/pe.py`, generalized. That file is now a nine-line front for it and `experiments/branching_vm/check.py` passes unchanged against it. What it gained:

* **Clause selection on runtime data.** A literal pattern, a repeated pattern variable or `=` that meets a runtime leaf becomes a residual test, and the clauses of the function become residual branches in source order. A `where` guard is evaluated only when its patterns match. When no clause is left the residual code traps, as Shen raises.
* **Typed leaves and one symbol table.** A runtime leaf is a number, a symbol or a boolean. A symbol is compared by its code from the manifest table; a number never equals a symbol. A symbol that meets runtime data and is not in the table is a compile error.
* **Joins instead of duplication.** A runtime conditional in value position evaluates both arms and joins them leaf by leaf into temporaries. Where the two arms have different shapes (two effect lists of different length) the join keeps a `Split`, resolved only where something inspects it or at the return, by re-testing the condition.
* **Sharing.** Residual expressions are hash-consed per control-flow scope and counted. A node used more than once is bound to a slot where it was created.
* **Binding times at specialization points.** A specialization point is a tail call under runtime control that re-enters a function already being unfolded. The default division is the old one (an argument holding runtime data has its numbers lifted, a fully static argument stays static). Two controls replace it where it is wrong: `division={"f": ["S", "D", None]}` fixes the binding time of an argument at the entry, and `widen=k` turns a static number into a runtime value once it has taken more than `k` distinct values at one position of one argument shape.
* **Event dispatch.** The entry of a compiled step tests the runtime event tag against each declared event kind and specializes the step once per kind with that tag static, so the specification can pattern match on events of different arity.

### Native ABI

```c
int64_t node_step(const int64_t *in, int64_t count, int64_t *out, int64_t capacity);
```

| Vector | Layout |
|---|---|
| `in` | state leaves in depth-first order, event tag code, event fields, zero padding to `state + 1 + widest event` |
| `out` | new state leaves, effect count `n`, then for each effect its tag code and its fields |

The return value is the number of integers written, or `-1` when the specification has no result (trap), `-2` for a read outside the input, `-3` when `capacity` is too small. `capacity` never needs to exceed `state + 1 + max_effects * (1 + widest effect)`. The function keeps nothing between calls, so any number of node instances share one library. `tool.compile.build` writes `node.ir.json`, `node.c`, the shared library and `node`, a line-oriented executable with the protocol of `specializer/driver.c.in` (one vector per line; a trap prints `trap <code>`).

## 3. Real host

`python3 -m tool.host host.json` runs one node as an OS process: it loads `node.wasm` in wasmtime and gives each role its real meaning. It reads the manifest and acts on roles only, like the simulator. The module still imports nothing, so everything nondeterministic reaches the node as an event, and the host writes every event down.

```json
{"manifest": "node.json", "wasm": "node.wasm", "node": 0, "init": [1, 0, 0],
 "data": "node0", "client": ["127.0.0.1", 7000],
 "peers": [["127.0.0.1", 7100], ["127.0.0.1", 7101]]}
```

`init` is the state integers of `(<init> Node NodeCount)`; `peers` lists every node's peer address, this node's included. `tool.host.bundle` compiles a specification to such a bundle and `tool.host.Cluster` starts and kills its processes on loopback.

| Role | What the host does |
|---|---|
| `restart` | On every start of the process the state is `init` and `restart` is the first event. The host has no shutdown: every stop is a crash. |
| `request` | One JSON list per line, `["submit", 0, 7, 11]`, on the client TCP port. Only a declared `request` event with well-typed fields is delivered; anything else is dropped. |
| `output` | The effect as one JSON line to every client connected at that moment. At most once: with no client connected it is lost. |
| `send`, `recv` | One JSON line on a TCP connection to the peer port of `Dest`, carrying the `recv` event and the place of the send. At most once: if the peer cannot be reached now the message is dropped. |
| `disk-write` | Handed to the disk thread, which appends the row to `disk.log` and calls `fsync`; only when `fsync` has returned does the `disk-ack` event exist. Effects after the write in the same step do not wait for it. |
| `disk-read`, `disk-read-result` | The disk thread, in the order asked, scans `disk.log` for the last row with the key. A read therefore sees every write issued before it. |
| `timer-set`, `timer-cancel`, `timer` | A deadline on the monotonic clock, `Delay` in milliseconds. A timer is never queued, so a cancel or a replacement always wins. Timers die with the process. |
| `clock-read`, `clock` | `Now` is the monotonic clock in milliseconds. |

`fsync` is `fsync(2)`. On Darwin that does not flush the drive's cache; `HOST_FULLFSYNC=1` uses `F_FULLFSYNC`. `HOST_CRASH=point:n` makes the host `kill -9` itself on the n-th pass through one point: `log-torn` (half a log record written), `logged` (record durable, no effect acted on), `effect` (between two effects of one step), `disk-torn` (half a row written), `synced` (row durable, no acknowledgement).

### The event log

Per node the data directory holds two append-only files of checksummed records ([host/wal.py](host/wal.py)): one line each, the CRC-32 of the body in eight hex digits, a space, compact JSON.

```
events.log   {"seq": 0, "boot": {"node": 0, "nodes": 1, "init": [...], "wasm": "<sha256 of node.wasm>"}}
             {"seq": 4, "event": ["submit", 0, 7, 11], "cause": null,
              "post": [[["free", 0, 0], ["free", 0, 0]], ["writing", 0, 0, 7, 11, 0], 1],
              "effects": [["write", 0, 0, 7, 11]]}
             {"seq": 5, "event": ["written", 0], "cause": [4, 0], "post": ..., "effects": [["reply", 0, "ok", 0]]}
disk.log     [4, 0, 0, 7, 11]
```

For each event the host steps the node, appends the event with the state and effects the step returned, calls `fsync`, and only then acts on the effects. `cause` is the effect that produced the event, as `[seq, index]` (`[node, seq, index]` for a message); a request and a restart have none. A durable row begins with the place of its `disk-write` effect, so the two files can be read against each other. A step with no result is logged as `"trap": true` and the process exits.

A record is appended with one write, so a kill or a power loss can leave only a prefix of the last record: bytes after the final newline. That *torn tail* is the one kind of damage that is accepted. In `events.log` it is an event the node stepped on whose effects never left the process, and the process is dead; in `disk.log` it is a write nobody was told about. The host cuts both off when it starts, before appending. A complete line that fails its checksum is corruption, wherever it is.

### Conformance

`python3 -m tool.host.conform node.json node0 node1 ...` judges a run from the files alone ([host/conform.py](host/conform.py)).

* **Causality.** Every event other than a request or a restart names its cause, and the cause must be an effect of the right role, earlier, with the same token, key, timer or payload. An acknowledgement, a read result, a timer and a clock reading must be caused within their own segment: a kill takes unacknowledged writes, armed timers and reads in flight with it. A read result must be what `disk.log` held before the read. Every durable row must be a write the log holds, in order. No cause produces two events.
* **Refinement.** A boot record starts a segment: the state is `init` and, if the manifest has a `restart` event, it is the first event. From there each record's state before is the previous record's state after, and `check.refinement` requires the Shen source in shen-jit to give the recorded state and effects at every record.
* **Invariants.** The logs become one trace in the simulator's form, `[Kind Node Event Effects World]`, and the manifest's invariants are evaluated on it unchanged. Nodes are interleaved in an order that puts every send before its receive. A request is a `client` step followed by a `deliver`. A row enters `World` at a `sync` step placed where the host logged the acknowledgement, which is the first moment anyone outside the disk knows `fsync` returned; a row that was written but never acknowledged enters at the `crash` step that ends its segment if `disk.log` holds it, and is otherwise a lost write. The end of a log is a crash. All effects of a record count as having happened, which is the unfavourable reading when the kill came between them.

A torn tail is dropped and counted in the result (`torn`, `torn_rows`).

### The retained checks

`python3 -m tool real accepted_job` ([examples/accepted_job/real.py](examples/accepted_job/real.py)) runs the node on loopback under a client that submits two keys with two digests and retries on silence. The node is killed with `kill -9` at random moments after a request leaves, and by `HOST_CRASH` at each of the five points; files with a torn tail are copied and checked as the kill left them. After each run the logs must conform, and every reply the client received must be an effect in the log and, if `ok`, a row in `disk.log`. Teeth: a passing run's files with one state, effect, event or cause edited (checksums recomputed), one record removed, one byte changed, a complete but short final record, or one row added to `disk.log` are each rejected; each of the three mutants of `check.py` is run for real and rejected twice, by refinement against `spec.shen` and, judged against its own source, by the invariant named for it.

`python3 -m tool real heartbeat` ([examples/heartbeat/](examples/heartbeat/)) is two nodes that ping each other on timers and read the clock; it exists to exercise `send`, `recv`, the timer roles and `clock-read`, and the interleaving of two logs. Both are the flake check `real-host`.

## 4. Three tiers

The same compiled step runs in three places. Each tier judges it with the same two questions, refinement of the Shen source and the Shen invariants, and each can show something the one before cannot.

| Tier | Run | What is real | What it can show | What it cannot show |
|---|---|---|---|---|
| Simulator | `tool check <example>` | The step. Clock, network, disks, crashes and schedule belong to `sim/`. | Every schedule within a bound (Maude and the explorer agree on the reachable observations), seeded schedules that replay byte for byte, a minimal trace for each mutant. | Anything about an operating system. Its disk becomes durable when the simulator says so, and its crash is an assignment. |
| Loopback processes | `tool real <example>`, flake checks `real-host` and `packed-node` | The process, its sockets, files, `fsync` call and clock, and `kill -9`, also at five chosen points inside the host. | That the host gives the roles their meaning: a record is on disk before its effects leave, a torn record is cut and nothing else is forgiven, and logs of real runs refine the source. | That `fsync` did anything. A killed process loses its memory and the kernel keeps every byte it was handed, so a host that never synced would pass. The network is one kernel's loopback: nothing is lost, delayed or cut. The schedules are the few the kernel happened to choose. |
| NixOS machines | flake checks `real-vm` and `real-vm-mutant` | One kernel per node, a virtual network between them, a virtual disk under ext4, systemd. | That the packaged service survives what the test driver does to it from outside: `systemctl kill -s KILL`, the machine's exit (the guest's page cache is gone, so only what `fsync` pushed to the disk remains), a cut link, `iptables` drops between two nodes, `tc netem` delay and loss. | Any schedule on demand: it shows the handful it ran, once each, with no replay. The power loss is QEMU exiting, so the host's own cache and a real drive's write cache are not tested. Nothing about more than three machines, or about time longer than a test. |

A failure found in a higher tier is a log, and a log is not a schedule: to keep it, write the history as a simulator trace or a mutant, where it replays.

### Packages and the service

```
packages.<system>.accepted_job                    share/node/{node.wasm,node.json,inits.json,spec.shen,invariants.shen}
packages.<system>.accepted_job-reply-before-ack   the same from one mutant of check.py     bin/node-host
packages.<system>.heartbeat                       packed for two nodes
packages.<system>.node-runtime                    bin/node-host, bin/node-run <module under tool> ...
packages.<system>.node-conform                    bin/node-conform: host/conform.py with its shen-jit
```

`share/node` is what [host/pack.py](host/pack.py) writes: the module, the manifest, the state integers of `(<init> Node Nodes)` for every node, and the sources conformance is judged against. `bin/node-host <node> <data directory> <client host:port> <peer host:port> ...` runs one node of it; it refuses a number of peers the package was not packed for. [../nix/module.nix](../nix/module.nix) (`nixosModules.default`) turns that into a unit:

```nix
services.shen-node.job = {
  package = self.packages.${system}.accepted_job;
  node = 0; clientPort = 7000; peerPort = 7100;      # peers = [ "node0:7100" "node1:7100" ] for several
  openFirewall = true;
};
```

The unit is `shen-node-<name>.service`, with user `shen-node`, state directory `/var/lib/shen-node/<name>` (the event log and the durable file), `Restart=always` after 100 ms and no start limit: the host has no shutdown, so systemd's restart is the specification's `restart` event.

Two commands are the client side. `node-run host.ask <host:port> <seconds> '<request>' [received.json]` sends one request and prints the output that answers it. `node-run examples.accepted_job.load <record.json> <host:port> <answers> <pause> <seconds> [seed]` is the retrying client of `real.py`: a submission is sent under a new request number until that number is answered. Both keep every output they read, and `node-conform --received N=FILE` requires each of them to be an output effect in the log of node `N`. The host makes a record durable before acting on its effects, so an output a client holds that no record holds means a record was lost after `fsync` returned.

### The machine tests

[../nix/vm.py](../nix/vm.py) is the script of `real-vm`: a client machine and two node machines. Each node machine runs its own AcceptedJob node (one node, its own disk; AcceptedJob has no messages between nodes) and one of the two heartbeat nodes, which are one system and the only place `send` and `recv` cross the network.

| Step | Fault | Asserted while running |
|---|---|---|
| 1 | `node0.crash()` directly after the client received its first `ok`, then `start()` | The durable file is not empty after boot. |
| 2 | `systemctl kill -s KILL` six times under a workload | systemd restarted the node at least three times; a submission had to be retried. |
| 3 | `node0.block()` for four seconds under a workload, `unblock()` | No submission is answered while the link is cut; the workload finishes after. |
| 4 | `tc qdisc add dev eth1 root netem delay 60ms loss 15%` on node1, a kill under it | The fastest answer takes at least 50 ms, and longer than without the qdisc. |
| 5 | `iptables -I INPUT -s <peer> -j DROP` on both heartbeat nodes, then removed | Neither count of pings received moves during the partition; both move after. |
| 6 | netem between the heartbeat nodes, a kill of one | Pings still arrive. |
| 7 | `node1.crash()` and `start()` with node0 still pinging | Pings resume in both directions. |
| 8 | `stop` to both | A cancelled timer stays silent. |

Then the units are stopped, every node's `events.log` and `disk.log` and the client's records are copied out of the machines into the test's output, and `node-conform` runs on them on the driver: three runs (AcceptedJob on node0, on node1, and the two heartbeat logs together), each with `--wasm` and `--received`. The test passes only if all three exit 0 with the expected segments. Steps 1 and 7 are where `fsync` is tested: an `ok` that reached the client, or a ping that reached the other node, must be in a log that lost its machine.

`real-vm-mutant` ([../nix/vm-mutant.py](../nix/vm-mutant.py)) is the teeth. One node machine runs the package compiled from `reply-before-ack`; the test passes only if `node-conform` on its log exits non-zero with a refinement failure against `spec.shen`, and, judged against the mutant's own source, reports `inv.ok-is-durable`.

`packed-node` ([../nix/packed.py](../nix/packed.py)) is the part of this that runs anywhere: the same packages, launchers, client commands and `node-conform` invocations on loopback, with `kill -9` and the script in the place of systemd. It checks that the mutant is rejected for those two reasons, that one forged output in `--received` is rejected, and that a two-node package refuses to start as one node. It says nothing about the machines.

```sh
nix build .#checks.x86_64-linux.real-vm .#checks.x86_64-linux.real-vm-mutant -L   # or aarch64-linux; needs KVM
nix build .#checks.x86_64-linux.real-vm.driverInteractive && ./result/bin/nixos-test-driver   # step by hand
```

The logs are in `result/node0/job`, `result/node1/heartbeat`, `result/client` and so on.
