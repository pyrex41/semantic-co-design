# Inferred semantic library

This library is inferred only from `EXAMPLES.md`; it is not a general system
specification.

## Definitions

- **Request identity**: the tuple `(tenant, key, payload)` supplied by a client
  for job submission. Tenant is part of the lookup scope; the same key in two
  tenants denotes different requests.
- **Successful submission**: a committed job row together with the receipt
  returned for that job.
- **Idempotent retry**: retrying the same request identity after an unknown
  reply outcome must resolve to the original committed job, not create another
  job.
- **Scoped lookup**: a lookup is constrained by tenant and key (and therefore
  cannot return another tenant's receipt).
- **Writer reservation**: acquiring the transaction's writer slot before the
  scoped absence lookup, so overlapping decisions are serialized.
- **Publication attempt**: an attempted external send of a pending job.
- **Attempted mark**: durable state recording that the send was attempted; in
  the intended fixture it commits atomically with publication.
- **Stable provider identity**: an identity reused across replayed sends so the
  provider can recognize a duplicate; provider receipts may expire.

## Laws / invariants

1. **Submission idempotence**: for one request identity, at most one durable
   job is created. A retry returns the existing receipt if the first commit
   succeeded; if it rolled back, the retry may create the first job.
2. **Concurrent uniqueness**: overlapping clients must not both observe absence
   and commit jobs for one request identity. Reserve the writer slot before the
   lookup (or provide an equivalent serialization guarantee).
3. **Tenant isolation**: a receipt found for one tenant must never satisfy a
   request from another tenant, even when their keys match.
4. **Outbox replay suppression (intended fixture)**: publication and the
   attempted mark are one commit unit. A later send publishes only if the prior
   unit rolled back or no mark is visible; after a committed mark, replay is
   suppressed.
5. **Unknown-outcome caution**: an attempt count cannot establish whether an
   external effect committed. Verification requires durable publication state,
   provider receipt/identity behavior, or equivalent effect evidence.
6. **Provider receipt boundary**: stable identity suppresses duplicate effects
   only while the provider retains the relevant receipt (illustrated by
   ten-tick retention: replay at tick nine yielded one effect, tick ten yielded
   two). No receipt means replay can yield two effects.

## Assumptions exposed by the examples

- A transaction can commit or roll back as a unit and its durable rows are
  inspectable.
- Clients may lose replies and retry; the client-visible receipt alone is not
  proof of uniqueness.
- Concurrent transactions can both read absence unless writer ordering is
  deliberately established.
- The provider's external effect is not automatically atomic with the worker's
  durable mark in the split-provider design.
- Provider deduplication, if present, depends on stable identity and finite
  receipt retention.

## Evidence limits

The examples establish these behaviors for the stated fixtures and timing
points only. They do not define a complete schema, isolation level, receipt
format, crash/recovery protocol, provider API, retention policy beyond the
ten-tick illustration, or behavior under arbitrary failures and concurrency.

