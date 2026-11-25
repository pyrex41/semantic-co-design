# Chapter 3. Modularity, objects, and state

Run `python3 -m experiments.patterns.sicp.check_state` from the repository root. These Python examples stage profiles into closures or emitted source. Each check changes at least one profile choice while keeping an independent expected behavior. The canonical chapter sections are linked below. [MIT Press table of contents](https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-4.html)

## Runnable patterns

| Pattern | Bound at specialization | What execution still does |
|---|---|---|
| `specialize_account` | Withdrawal fee policy. Zero-fee withdrawals bind the identity operation. | Keep each balance in its own closure, validate amounts, serialize reads and updates, and reject an overdraft. |
| `bind_transfer` | Source, target, self-transfer behavior, and ascending lock identity for an account pair. | Acquire the fixed locks, test funds, and transfer the requested amount without a withdrawal fee. Self-transfer checks available funds. |
| `specialize_table` | Public keys, numeric slots, and which keys share a mutable cell. | Allocate independent cell contents for each instance. A bound read or write touches its cell directly. |
| `specialize_memoized` | Recursive body and key projection. | Compute and cache successful values by live keys. Recursive calls use the same table. Each factory call allocates a fresh table. |
| `specialize_circuit` | Wires, probe selection, gate functions and delays, and the ordered calls a changed wire makes. | Keep signal bits and time, capture the gate's value when it fires, and schedule that value for delivery. The agenda orders deliveries by time and insertion order. |
| `specialize_constraints` | Connector positions, constants, equations, and the notification calls for each connector. | Choose an inference direction from the available values, propagate, and report contradictions. Retraction rebuilds the consequences of surviving assertions. |
| `specialize_pipeline` and `specialize_window` | Map/filter order and functions, or window width, stride and combining function. | Force enough source tails for the next result. Multiple consumers of a stream cell share its memoized tail. |
| `specialize_pairs` | Combining function and the recipe for alternating between the current row and later rows. | Enumerate pairs of source indices with the left index at most the right index. Alternation gives a later row a turn even when the current row is infinite. |
| `specialize_feedback` and `specialize_accumulator` | Step and derivative, or request-to-state transition. | Integrate from a runtime starting value or compute the sequence of states after each runtime request. Each new result waits for demand. |

The emitted table, circuit, constraint, pipeline, and window factories expose their generated Python in `.source`. Circuit wires call fixed gate functions directly. Each constraint connector enqueues fixed inference callbacks. Each pipeline applies fixed stages and loops only to find the next eligible source item. Table instance creation binds aliases to the same accessor pair.

Accounts and transfers can run across threads using the provided operations. Their locks serialize operations in one Python process. The other mutable instances and stream promises expect one driving thread. A circuit accepts integer bits, positive integer transport delays, and at most one driver per wire. Agenda budgets leave pending deliveries available on the next run. Constraint arithmetic uses `Fraction`. Repeated terminals in one equation are outside the profile. A connector accepts one assertion source at a time, and an agreement with an already inferred value acquires no ownership. A contradiction during assertion rolls back the new facts. Retraction requires the matching source token and replays surviving independent assertions, including when the equations form a cycle.

## Use the public entry points

```python
from fractions import Fraction

from experiments.patterns.sicp.state import specialize_circuit, specialize_constraints
from experiments.patterns.sicp.streams import specialize_feedback, take

build = specialize_circuit(
    ("input", "output"),
    (("not", ("input",), "output", 2),),
    probes=("output",),
)
circuit = build()
circuit.inputs["input"](1)
assert circuit.agenda.run() == 2
assert circuit.trace == [(0, "output", 0), (2, "output", 1), (2, "output", 0)]

network = specialize_constraints(
    ("raw", "scale", "scaled"),
    (("product", "raw", "scale", "scaled"),),
    constants={"scale": 5},
)()
assert network["scaled"].assert_value(12)
assert network["raw"].read() == Fraction(12, 5)
assert network["scaled"].retract()
assert network["raw"].read() is None

predict = specialize_feedback(lambda y: y, Fraction(1, 2))
assert take(predict(2), 4) == [2, 3, Fraction(9, 2), Fraction(27, 4)]
```

## Section coverage

| Section | Included mechanism or scope decision |
|---|---|
| [§3.1 Assignment and Local State](https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-20.html) | A profile creates independent account objects with stateful direct operations. |
| [§3.1.1 Local state variables](https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-20.html#%25_sec_3.1.1) | The account balance is a private captured cell. Deposit and withdrawal are bound operations on it. |
| [§3.1.2 Benefits of assignment](https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-20.html#%25_sec_3.1.2) | Account calls hide their prior state from callers. A random source and Monte Carlo sampler are applications of the same private transition and stream accumulator mechanisms. |
| [§3.1.3 Costs of assignment](https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-20.html#%25_sec_3.1.3) | Same arguments can produce different results after mutation. The checker observes identity, aliasing, failed writes, and independent histories. |
| [§3.2 The Environment Model of Evaluation](https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-21.html) | Python supplies lexical environments for the state examples. Language evaluation and lexical addressing overlap chapters 4 and 5. |
| [§3.2.1 Evaluation rules](https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-21.html#%25_sec_3.2.1) | Reused by the evaluator patterns. |
| [§3.2.2 Applying simple procedures](https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-21.html#%25_sec_3.2.2) | Reused by the evaluator patterns and direct closure calls. |
| [§3.2.3 Frames as repositories of state](https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-21.html#%25_sec_3.2.3) | Captured balance and cell storage belong to each instance. Bound transfers and table aliases share access to an instance. |
| [§3.2.4 Internal definitions](https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-21.html#%25_sec_3.2.4) | Instance factories define their private operations in one lexical scope. Scheme's internal-definition semantics belong to the evaluator. |
| [§3.3 Modeling with Mutable Data](https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-22.html) | Fixed storage, circuit, and constraint schemas use mutable instance data. |
| [§3.3.1 Mutable list structure](https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-22.html#%25_sec_3.3.1) | Table aliases expose shared mutable identity. Pair surgery and cycle detection on runtime pointer graphs fall outside the fixed table profile. |
| [§3.3.2 Queues](https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-22.html#%25_sec_3.3.2) | Agenda deliveries at a given timestamp form a FIFO. Constraint work also uses a FIFO. Queue entries and lengths are runtime data. |
| [§3.3.3 Tables](https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-22.html#%25_sec_3.3.3) | Fixed keys select cells at staging. Recursive memoization binds a key policy and retains a live table for keys that arrive during execution. Key projections must map equivalent calls to the same hashable value. |
| [§3.3.4 Digital circuits](https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-22.html#%25_sec_3.3.4) | Fixed gate networks, initialized gate actions, probes, equal-time FIFO behavior, transport delay, and bounded execution of feedback loops. |
| [§3.3.5 Constraint propagation](https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-22.html#%25_sec_3.3.5) | Exact bidirectional addition and multiplication, constants, unknowns, contradiction, and retraction. The adaptation replays surviving assertions on retraction. It does local propagation without general equation solving. |
| [§3.4 Concurrency](https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-23.html) | Serialized methods and composed locks on known account pairs. |
| [§3.4.1 Time in concurrent systems](https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-23.html#%25_sec_3.4.1) | The checker runs opposing transfers concurrently and verifies the resulting balances. |
| [§3.4.2 Concurrency controls](https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-23.html#%25_sec_3.4.2) | Lock order binds from the same live identities for both transfer directions. Python provides mutual exclusion. Semaphore construction and dynamic lock discovery are outside this profile. |
| [§3.5 Streams](https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-24.html) | Stream cells support finite and infinite histories, shared demand, and delayed feedback. |
| [§3.5.1 Streams as delayed lists](https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-24.html#%25_sec_3.5.1) | Eager head, memoized delayed tail, staged map/filter, and observable demand. |
| [§3.5.2 Infinite streams](https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-24.html#%25_sec_3.5.2) | Infinite runtime sources and a stream defined from its own delayed history. Prime sieving and formal power series are separate applications of lazy producers and consumers. |
| [§3.5.3 Using the stream paradigm](https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-24.html#%25_sec_3.5.3) | Fixed moving windows for iterative and signal transforms, finite demands on infinite pair enumeration, and discrete integration. Higher-order tableaux and weighted merge are outside these profiles. |
| [§3.5.4 Streams and delayed evaluation](https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-24.html#%25_sec_3.5.4) | A discrete solver makes its initial value available before following the feedback dependency. Language-wide lazy evaluation belongs to chapter 4. |
| [§3.5.5 Functional and object modularity](https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-24.html#%25_sec_3.5.5) | A bound transition consumes a stream of requests and exposes the resulting history. The arrival or merging of concurrent request streams remains an application concern. |
