# A generated document approval service

## Product and executable package

Deliver a self-hosted, multi-tenant application. Tenant administrators invite members, grant roles, define approval workflows and publish revisions. Authors draft documents, upload attachments, submit for review and revise rejected submissions. Reviewers work from an inbox, approve, reject or delegate when the published workflow permits it. Auditors search a chronological history and export it. Operators install, upgrade, back up, restore and diagnose an instance.

The app author writes the page and form definitions, URL and method contracts, identity mapping, access rules, workflow language and evaluator, queries, transactions, workers and migrations in the application specification. Specializing it yields a server-rendered web/API executable, a worker executable, a migration/admin command and their assets. A generic HTTP/TLS host calls generated entry points. PostgreSQL, blob storage, an OIDC identity provider and an email transport are external dependencies. Their adapters expose typed operations. No handwritten controller, ORM model, workflow worker or parallel browser implementation supplies product behavior.

## Source and build design

| Authored module | What specialization produces | What remains live |
|---|---|---|
| Routes, sessions and access | HTTP dispatch, input decoding, session lifecycle and tenant-scoped query predicates | Identity claims, session expiry, membership, role revocations and request values |
| Pages and forms | HTML renderers, accessible validation feedback, navigation, form decoding and mutation handlers | Locale, permissions, drafts, validation errors and inbox contents |
| Documents and files | Record mappings, indexes, blob intent, upload completion and download authorization | Object bytes, version, upload outcome and retention state |
| Workflow definitions and interpreter | Transition code, assignment queries, durable waits and decision explanations | Document fields, active reviewers, votes, timers and current role grants |
| Jobs and administration | Delivery/retry handlers, audit export streams, schema upgrades and health endpoints | Delivery receipts, database state and operator commands |

Use ordinary functions such as `submit(actor, document, workflow)` and `renderInbox(viewer, rows)` in the reference application. Page definitions call the same authorization and query functions as the API. Compile form identifiers, field types and routes at application build. A tenant publishing a new workflow is an application event: validate the declarative workflow, compile it in a bounded build worker with no tenant credentials, persist its source and artifact digests, then atomically activate the version. Serve the previous version during a failed build. Existing submissions retain the workflow version captured on submit. Recheck live membership on every decision. The core service and the tenant compiler have separate installation and privilege boundaries.

## State, interfaces and concurrency

Persist tenant, membership, session, workflow revision, document revision, attachment, submission, assignment, vote, audit event, command receipt and delivery intent. Include tenant identity in primary and foreign keys for scoped objects. Bind idempotency keys to tenant, actor, command and payload digest. Use an expected version for edits and decisions. Only an authorized command inside the transaction may read protected state and commit its new state; serialize membership changes against decisions where immediate revocation is promised.

Expose login/callback/logout, tenant administration, workflow publication, document CRUD and revision history, attachment upload/download, submission, reviewer inbox, decision, audit and export through generated routes. Provide accessible browser pages for each human action and a versioned JSON interface for integrations. Include CSRF protection on browser mutations, encoded HTML, paginated queries, explicit error contracts, quotas and route-level authorization. Generate the API schema from the same typed entry definitions. Exercise the deployed server from a browser and a separate HTTP client.

**One complete history.** A reviewer approves revision 7. In one transaction the handler checks the current grant and expected revision, appends the vote and audit event, updates the submission, and creates a notification intent and a command receipt. Lose the HTTP response. A replay of the same command returns the stored decision subject to current access; a changed payload under the same key conflicts. Race two final votes and verify the workflow's quorum policy, one terminal transition and one durable terminal notification intent. A transport that can deliver twice gets a stable message identity; delivery status distinguishes accepted, unknown and failed.

**Other recovery paths.** Stage uploads under a temporary identity, verify recorded length and digest, then attach by transaction. Reap abandoned objects without deleting referenced versions. Persist worker leases and retry deadlines. Support administrator inspection and redrive. Record destructive administrative actions in the audit log. Test restoring PostgreSQL and blobs to a mutually consistent point, including workflows whose old continuations are still runnable.

## Delivery sequence and exit gates

| Stage | Deliverable | Acceptance evidence |
|---|---|---|
| 1. Application contract and reference install | Schema, OIDC session, role matrix, draft/submit/review pages, reference runner using real PostgreSQL | Two browser users finish one review. Cross-tenant object IDs, changed roles, stale revisions and duplicate commands have explicit outcomes. |
| 2. Generate the same service | Dynamic branches, typed records, transactional continuations, HTML/JSON lowering and package launcher | Install generated package into a clean environment. Replay stage 1 through both browser and API. Diff HTTP outcomes, visible pages, committed rows and effects. |
| 3. Finish tenant and document lifecycle | Invites, role changes, workflow editor/publication, quorum and delegation, rejection/resubmission, attachments, inbox search and audit export | Two tenants use different policies concurrently. Publish a revised quorum during a pending review. Verify both versions, accessibility with keyboard-only operation and complete exports. |
| 4. Operate and recover | Notifications, redrive, quotas, metrics, structured tenant-safe logs, alerts, backup/restore, migration and rollback tooling | Kill the app or worker around each commit and send. Restore a previous release with pending reviews and deliveries. Reconcile audit, blob references and outbox against independent counts. |
| 5. Qualify and release | Signed reproducible packages, install/upgrade guides, admin runbooks and supported-version manifest | Load-test inbox reads alongside decisions, exports and uploads at declared tenant sizes. Record p95/p99 by endpoint, SQL query counts, lock waits, compile latency and restore time. Canary a tenant, roll back, then repeat on a clean installation. |

A frozen-toolchain acceptance change adds a two-reviewer rule above a document threshold and a corresponding browser field. The generated page, API validation, transaction, notifications and audit must agree across both sides of the threshold. Shipping requires passing every stage, including operation of an instance after the compiler worker is taken offline.
