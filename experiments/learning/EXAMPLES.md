# Training examples shared by all three arms

## Job submission after a lost reply

A client submits one request. The worker commits the job and loses the reply. After restart the client retries with the same tenant, key and payload. One implementation returns receipt 1 and storage contains job 1. A mutant returns the same receipt and storage contains jobs 1 and 2. Both look identical to the client. Reading both durable rows reveals the defect. If the original transaction rolled back, a successful retry creates the first job.

Two overlapping clients complicate the lookup. When both read absence before either insert commits, each can create a job. A transaction that reserves the writer slot before the scoped lookup serializes the decisions. With two tenants and the same key, omitting tenant scope can return the other tenant's receipt even when the database correctly serializes the writes.

## Outbox send after a lost reply

One pending job is sent twice after the first send's outcome is unknown. The legacy Capcov model admits one publication on each send when both commit. The intended fixture commits the publication and attempted mark together. If the first send commits, the second sees the mark and suppresses publication. If it rolls back, the second publishes.

In the split-provider implementation the provider commits an external effect before the worker records an attempted mark. The worker restarts and repeats the attempt. With a stable identity and a ten-tick provider receipt, replay at tick nine produced one effect in total. Replay at tick ten produced two. When the provider had no receipt, the retry also produced two. Attempt counts alone could not distinguish the outcomes.
