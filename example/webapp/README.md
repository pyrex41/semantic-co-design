# Document Desk

Document Desk is a server-rendered document approval service backed by PostgreSQL. The seven Shen modules define identity mapping, permissions, forms and views, schema migrations, commands, queries, workflows, the delivery worker, and operator operations. The [application manifest](manifest.json) supplies typed HTTP contracts and maps browser, API, and process entry points to the same definitions.

## Build and install

Use a Python environment with CPython development headers and a C toolchain. The host must supply PostgreSQL tools compatible with the server used for backup and restore. Provision an OIDC client and an SMTP transport, and assign the service a persistent database and private object directory.

From the repository root, install the build environment and compile the release into a new directory:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r example/webapp/requirements-build.txt
python example/webapp/appc.py check example/webapp/manifest.json
python example/webapp/appc.py build example/webapp/manifest.json --out /tmp/document-desk-release
python example/webapp/appc.py inspect /tmp/document-desk-release
```

To build offline, provision the listed wheels in advance and add `--no-index --find-links /path/to/wheels`. The build uses local sources and the installed C and Python toolchain. The core package contains the `serve`, `worker`, and `admin` launchers and the `lib` directory. Install the separate `policy` launcher with `policy-lib`. The runtime environment needs only [runtime requirements](requirements.txt) and the same CPython ABI. The package adds an ownership map with source locations, specialized instances, capabilities, and source, driver, and binary digests. The emitted C and Cython files support inspection.

Seal and verify the full package before selecting the parts to install. Keep production signing keys in the operator's established signing environment.

```sh
python example/webapp/appc.py seal /tmp/document-desk-release --private-key /path/to/private.pem
python example/webapp/appc.py verify /tmp/document-desk-release --public-key /path/to/public.pem
```

Configure the process environment before running any launchers.

| Setting | Use |
| --- | --- |
| `SC_DATABASE` | PostgreSQL connection string. Use a dedicated database. |
| `SC_BLOBS` | Private persistent object directory shared by the server and worker. |
| `SC_ORIGIN` | Externally visible HTTPS origin without a trailing slash. Register its `/callback` URL with OIDC. |
| `SC_OIDC_ISSUER`, `SC_OIDC_CLIENT`, `SC_OIDC_SECRET` | OIDC issuer and client credentials. The adapter uses authorization code flow with PKCE, state and nonce, an RS256 ID token, and a verified email. The secret is optional for a public client. |
| `SC_SMTP_HOST`, `SC_SMTP_PORT`, `SC_SMTP_FROM` | SMTP transport settings. The default port is 587 and the adapter requires STARTTLS. |
| `SC_SMTP_USER`, `SC_SMTP_PASSWORD`, `SC_SMTP_ID_HOST` | Optional SMTP credentials and message identity domain. Keep the message identity domain stable across releases. |
| `SC_POLICY` | Path to the installed policy launcher, optionally with arguments. Defaults to the release's sibling `policy` launcher. |

Run these commands in the runtime environment:

```sh
cd /tmp/document-desk-release
./admin migrate
./admin provision '{"tenant":"cedar","name":"Cedar Office","email":"owner@example.com"}'
./serve --listen 127.0.0.1 --port 8080
```

In a separate process with the same settings, run `./worker`. Terminate TLS at the reverse proxy, forward requests to the bound loopback address, and open the configured public origin. The administrator signs in with their invited address, invites authors and reviewers, and publishes a workflow before authors submit a draft. The acceptance fixture uses `SC_INSECURE_LOCAL=1` and `SC_SMTP_PLAINTEXT_LOCAL=1` with loopback dependencies.

The JSON interface is under `/api/v1`. Its OpenAPI contract is at `/api/v1/openapi.json`. Browser sign-in issues a session cookie. `GET /api/v1/session` supplies the session's CSRF value for clients using that cookie. JSON mutations take `Idempotency-Key` and, for cookie authentication, `X-CSRF-Token`. Resource edits and votes require the expected revision or submission version. List responses with an `items` array use the `next` value as the `after` query cursor. Document, inbox, and audit views accept `q` for search. Reusing a key with the same request returns its recorded result while the caller remains authorized. Reusing the key with a different request returns 409.

## Operate and recover

- `./admin doctor` and `GET /health` report the installed schema. `GET /metrics` reports per-route counts and elapsed time sums. Request logs use route names and status codes.
- `./admin backup '{"path":"/backups/snapshot-1"}'` takes a PostgreSQL dump and copies the corresponding objects while application transactions are blocked. Restore the bundle into an empty database and object directory with `./admin restore '{"path":"/backups/snapshot-1"}'`. Verify the restored service and pending jobs before switching traffic.
- Run `./admin migrate` before starting upgraded processes. The v2 migration adds indexes while retaining v1 schemas and existing workflow, submission, and delivery state.
- Tenant administrators can inspect deliveries and redrive failed or unknown sends. Each retry uses the same SMTP message ID. An expired worker lease becomes unknown and is eligible for retry after a delay. A lost transport acknowledgement can result in duplicate delivery.
- Install the policy launcher separately and restrict its environment and privileges. The server passes the compiler a restricted environment and at most 8 KiB of input. The worker has a two-second CPU budget and a ten-second wall-clock limit. The regular server continues servicing old workflow revisions while the policy compiler is offline. A failed publication leaves the current revision active.

## What the build specializes

`appc interpret` runs the authored definitions directly with the same PostgreSQL, HTTP, and infrastructure operations. Run the reference server from the repository root with the same database and identity settings:

```sh
export SC_POLICY='python example/webapp/appc.py interpret example/webapp/manifest.json policy'
python example/webapp/appc.py interpret example/webapp/manifest.json serve --port 8080
```

The specializer fixes manifest entry arguments, resolves calls, clones functions for known scalar arguments, eliminates fixed branches and clause tests, and emits direct native functions for the surviving graph. Request values, queries, permissions, and transport outcomes remain dynamic. The release's native extension invokes generic named capabilities through a CPython object ABI. The packaged host loads the generated extension and invokes its compiled entry wrappers. The authored fragment supports named first-order functions, scalar and variable patterns, lists and records, local bindings, control flow, signed 64-bit arithmetic primitives and named capabilities. It uses checked source arities and bounded specialization. Recursive calls use the execution stack.

Tenant workflow artifacts encode a versioned decision table with amount threshold, low and high quorum, delegation, and expiry. The generated evaluator applies the captured version for each submission. The starter workflow requires one reviewer through an amount of 10,000 and two above it. It allows delegation and expires after 72 hours. Protected transactions acquire tenant locks before checking current membership, so role changes and decisions serialize.

## Reproduce the local evidence

```sh
python -m pip install -r example/webapp/requirements-check.txt
python -m playwright install chromium
python example/webapp/check.py --database 'host=127.0.0.1 user=test_runner dbname=postgres'
```

Use an account on a disposable PostgreSQL instance that can create databases. The check uses separate ephemeral databases for interpreted and generated services. It drives Chromium, a separate HTTP client, a signed local OIDC peer, and a local SMTP receiver. It compares independent observable outcomes, using PostgreSQL to check committed votes, audit events, and delivery intents. Pass `--chromium /path/to/browser` to use an installed Chromium executable and `--release /path/to/release` to reuse a built package. Add `--frozen-change` to rebuild a baseline from which the amount field and threshold rule are removed, then compare the field, API, committed quorum, notification intents, and audit under the same compiler and drivers.

Run the recovery check against a built release to exercise delegation, expiry, staged-object cleanup, and worker recovery after a process is killed between SMTP DATA and acknowledgement. Run the same command without `--release` for the reference service.

```sh
python example/webapp/check_recovery.py \
  --database 'host=127.0.0.1 user=test_runner dbname=postgres' \
  --release /tmp/document-desk-release
```

The local gate ran on Darwin arm64, CPython 3.9, and PostgreSQL 18.6. The reference and generated services agreed on the revision-7 approval, role revocation, retry, and restore outcomes. The frozen-source check changed the browser amount field and moved the quorums from one on both sides of the threshold to one at the threshold and two above it. Two build directories matched across all 19 release files. A temporary signing key verified the release and detected a modified stylesheet.

Production qualification still needs the intended OIDC, SMTP, proxy and storage setup, signer and distribution policy, defined workload and platform targets, systematic faults at all persistence and transport boundaries, and a canary upgrade and rollback at those targets. See the [product plan](PLAN.md).

The identity adapter follows OpenID Connect Core and OAuth security best current practice. [OpenID Connect Core](https://openid.net/specs/openid-connect-core-1_0.html), [OAuth security best current practice](https://www.rfc-editor.org/rfc/rfc9700.html).
