\\ AcceptedJob: at most one durable job per idempotency key, with the commit
\\ boundary inside the specification.
\\
\\ State    [Jobs Pending]
\\ Jobs     one slot per receipt number: [unknown _ _] until its disk read returns,
\\          then [free _ _] or [stored Key Digest]
\\ Pending  [idle ...] or [writing Req Slot Key Digest]: the one write in flight
\\
\\ A job is accepted when the disk acknowledges its row. Only then is the client
\\ answered. Memory is lost at a crash; `restart` says what is read back.
\\
\\ What this specification relies on from its environment: a disk row is written whole
\\ or not at all, and no acknowledgement or read result is delivered across a crash. At
\\ most one write is in flight, so an acknowledgement can only be for that write; the
\\ token the disk hands back is not consulted.

(define job.init
  _ _ -> [[[unknown 0 0] [unknown 0 0]] [idle 0 0 0 0]])

(define job.step
  _ [restart] -> [(job.init 0 0) (job.reads 0 (hd (job.init 0 0)))]
  State [loaded _ Slot Found Key Digest] -> [(job.load State Slot Found Key Digest) []]
  State [submit Req Key Digest] -> (job.submit State Req Key Digest)
  State [written _] -> (job.written State))

\\ After a restart every slot is read back before any request is served.
(define job.reads
  _ [] -> []
  Slot [_ | Jobs] -> [[read Slot Slot] | (job.reads (+ Slot 1) Jobs)])

(define job.load
  [Jobs Pending] Slot 1 Key Digest -> [(job.put Slot [stored Key Digest] Jobs) Pending]
  [Jobs Pending] Slot _ _ _ -> [(job.put Slot [free 0 0] Jobs) Pending])

(define job.ready?
  [] -> true
  [[unknown _ _] | _] -> false
  [_ | Jobs] -> (job.ready? Jobs))

\\ A request that arrives while the table is loading is not answered; the client retries.
(define job.submit
  [Jobs Pending] Req Key Digest ->
    (if (job.ready? Jobs)
        (job.decide (job.find Key Jobs 0) [Jobs Pending] Req Key Digest)
        [[Jobs Pending] []]))

\\ The stored row for a key as [Slot Digest], or [-1 0].
(define job.find
  _ [] _ -> [-1 0]
  Key [[stored Key Digest] | _] Slot -> [Slot Digest]
  Key [_ | Jobs] Slot -> (job.find Key Jobs (+ Slot 1)))

(define job.decide
  [-1 _] [Jobs Pending] Req Key Digest ->
    (job.insert (job.free Jobs 0) [Jobs Pending] Req Key Digest)
  [Slot Digest] State Req _ Digest -> [State [[reply Req ok Slot]]]
  [Slot _] State Req _ _ -> [State [[reply Req conflict Slot]]])

(define job.free
  [] _ -> -1
  [[free _ _] | _] Slot -> Slot
  [_ | Jobs] Slot -> (job.free Jobs (+ Slot 1)))

\\ The write set is one row. Nothing is promised to the client here.
(define job.insert
  _ [Jobs [writing | Rest]] _ _ _ -> [[Jobs [writing | Rest]] []]
  -1 State Req _ _ -> [State [[reply Req full 0]]]
  Slot [Jobs _] Req Key Digest ->
    [[Jobs [writing Req Slot Key Digest]] [[write 0 Slot Key Digest]]])

\\ The disk acknowledged the row in flight: it is durable, so record it and answer.
(define job.written
  [Jobs [writing Req Slot Key Digest]] ->
    [[(job.put Slot [stored Key Digest] Jobs) [idle 0 0 0 0]] [[reply Req ok Slot]]]
  State -> [State []])

(define job.put
  0 Row [_ | Jobs] -> [Row | Jobs]
  N Row [Job | Jobs] -> [Job | (job.put (- N 1) Row Jobs)])
