# Build three applications by specializing their specifications

These directories contain the implementation plans and their locally executable applications. Each product has one authored application expressed in App Shen, a first-order language with Shen syntax and a versioned value and effect contract. See the [language contract and independent conformance gate](LANGUAGE.md). A reference runner executes those definitions. The build fixes the definitions, schemas, protocol descriptions and release manifest as inputs to a general evaluator and specializes that evaluator. The resulting package owns the public entry points, state transitions, outputs and recovery behavior. Each product specification must run against real drivers in reference mode before it can serve as the native build input.

| Product | What the operator installs | Public acceptance surface |
|---|---|---|
| [Document approval service](webapp/PLAN.md) | Web server, worker and migration executable built from one application release | Browser journeys, HTTP clients, identity provider and notification receiver |
| [Spot trading engine](trading-engine/PLAN.md) | Feed, strategy, order, ledger, reconciliation and operator entry points built from one engine release | Exchange sessions, operator CLI/API, account statements and restart |
| [Router and layer-4 load balancer](router-nlb/PLAN.md) | Linux appliance package with generated forwarding, connection handling and control plane | Ethernet interfaces, routing peer, backend servers and administrative API |

## Qualification from a checkout

The ownership receipt identifies the dialect, Python ABI and actual Python, Cython and C compiler versions used. The pip qualification environment and the flake each pin their own toolchain.

Run `nix flake check -L` to build all three native packages, run the language conformance suite, execute the generated web policy process, and replay trading and router histories. This gate runs alongside the existing AcceptedJob and staged-example checks.

The full local gate opens loopback sockets and drives a real browser and PostgreSQL. Run as an unprivileged user with the PostgreSQL client/server tools, a C compiler, OpenSSL and the pinned Shen JIT on PATH. Set `SHEN_JIT` if the executable has a different name. Install the check environment and run from the repository root:

```sh
python3 -m venv /tmp/application-check-env
. /tmp/application-check-env/bin/activate
python -m pip install -r example/webapp/requirements-check.txt
python -m playwright install chromium
python example/qualify.py --full
```

Pass `--chromium PATH` for an existing browser and `--gobgp DIR` for the GoBGP 3.37.0 binaries to include independent peer qualification. The runner creates and shuts down a private PostgreSQL cluster, builds to a temporary directory and uses only local test accounts and transports. Authenticated exchange operations, cross-host failure, physical networking and production load require separate infrastructure.

## What must be built in the compiler

The [ordinary VM experiment](../experiments/ordinary_vm/README.md) derives a loop from a first-order Shen fold. Application delivery needs a larger language fragment and a different runtime ABI. The following is proposed work.

1. **Define executable application semantics.** A program declares entry points, typed records and variants, schemas and indexes, views or wire layouts, and ordinary functions over them. External entry points receive typed events. Programs may call named capabilities such as reading a stream, transacting over records or sending bytes. The reference runner supplies the concrete execution of each capability. Publish the exact integer, decimal, byte-order, exhaustion and error semantics; reject unsupported constructs at build time with source locations.
2. **Build the specializer over the evaluator.** Mark program terms, schema and entry signatures static. Mark request bodies, database values, received bytes, clock results and effect outcomes dynamic. Unfold static calls and matches. Residualize dynamic branches, loops and failure paths. At control-flow joins, carry live values and effect ordering. Memoize recursive specialization by function and abstract arguments; generalize growing static arguments so generation terminates. Put explicit limits on compile work and artifact size. An unsupported entry fails the package build.
3. **Make effects resumable.** A yielded effect has typed arguments and success, failure and unknown-outcome continuations. Generate a stable continuation tag and data layout for durable waits. Persist the continuation, its release version and any local intent in the same transaction when the contract requires it. Drivers return actual outcomes. The application spec decides how a duplicate, partial read, timeout or uncertain send changes the application. A socket write and a database commit retain their distinct failure boundaries.
4. **Lower and package the entire entry graph.** Generate native code, codecs, dispatch, storage mappings, migration entry points and process launch manifests. Generate static assets or browser code where an application declares them. Link audited libraries for TLS, transport, persistence or device access through a versioned ABI. Static HTML, icons and CSS can be authored assets. Write the product build command as a generic manifest-driven tool, with no product names or route/opcode cases in the compiler.
5. **Prove ownership of behavior.** Emit a map from every public entry and source definition to its generated artifact, plus an inventory of linked drivers and capabilities. Audit the handwritten driver layer: it may implement an atomic compare-and-write, transmit a buffer or verify a signature. A driver that knows what an approval, fill, listener or unhealthy backend means has absorbed product behavior. Move that behavior into the specification before claiming the application is generated.
6. **Build versioning into the first ABI.** A release records source, compiler, driver and schema digests. Persist data and continuation versions explicitly. Test an upgrade with old state and pending work, then test rollback against that same state. A data migration that cannot be reversed requires an independently rehearsed restore or forward-repair procedure before promotion.

## Common release procedure

The proposed CLI contract is `appc interpret <manifest>`, `appc build <manifest> --out <release-dir>` and `appc inspect <release-dir>`. The package build runs offline from pinned inputs. Real deployments receive only the release and its declared system dependencies. Credentials enter through runtime handles and stay out of compiler inputs, traces and artifacts.

For every public entry, replay recorded inputs and effect results in the reference runner and generated executable. Compare externally visible bytes or normalized semantic outputs as appropriate, committed records, ordered effects and recovery state. Separately use independently written contracts, external clients and protocol peers; the same evaluator can make the same mistake twice. Inject faults immediately before and after each durable boundary. A missing observation leaves the property unverified.

Build one source-only mutation per subsystem and repeat with frozen compiler and drivers. Inspect the residual call graph for generic application interpretation or undeclared callbacks. Measure generation time, package size, cold start, throughput, tail latency, memory and recovery time against a recorded workload and host profile. Each product plan specifies what its measurements must include. Platform, load and recovery targets are release inputs agreed before qualification; benchmark results do not establish an untested service level.

Each product can progress in parallel once the reference capability ABI is agreed. Native work follows reference execution for the same entry. A new compiler primitive needs an executable source example, semantics, an independent expected result and a regression test. The first milestone in each plan is a vertical integration gate; the final milestone is the installable product.
