\\ AcceptedJob: at most one durable job per idempotency key, with the commit
\\ boundary inside the specification.
\\
\\ State    [Jobs Pending Next]
\\ Jobs     one slot per receipt number: [unknown _ _] until its disk read returns,
\\          then [free _ _] or [stored Key Digest]
\\ Pending  [idle ...] or [writing Req Slot Key Digest Token]: the one write in flight
\\ Next     the next disk token
\\
\\ A job is accepted when the disk acknowledges its row. Only then is the client
\\ answered. Memory is lost at a crash; `restart` says what is read back.

(define job.init
  _ _ -> [[[unknown 0 0] [unknown 0 0]] [idle 0 0 0 0 0] 0])

(define job.step
  _ [restart] -> [(job.init 0 0) (job.reads 0 (hd (job.init 0 0)))]
  State [loaded _ Slot Found Key Digest] -> [(job.load State Slot Found Key Digest) []]
  State [submit Req Key Digest] -> (job.submit State Req Key Digest)
  State [written Token] -> (job.written State Token))

\\ After a restart every slot is read back before any request is served.
(define job.reads
  _ [] -> []
  Slot [_ | Jobs] -> [[read Slot Slot] | (job.reads (+ Slot 1) Jobs)])

(define job.load
  [Jobs Pending Next] Slot 1 Key Digest -> [(job.put Slot [stored Key Digest] Jobs) Pending Next]
  [Jobs Pending Next] Slot _ _ _ -> [(job.put Slot [free 0 0] Jobs) Pending Next])

(define job.ready?
  [] -> true
  [[unknown _ _] | _] -> false
  [_ | Jobs] -> (job.ready? Jobs))

\\ A request that arrives while the table is loading is not answered; the client retries.
(define job.submit
  [Jobs Pending Next] Req Key Digest ->
    (if (job.ready? Jobs)
        (job.decide (job.find Key Jobs 0) [Jobs Pending Next] Req Key Digest)
        [[Jobs Pending Next] []]))

\\ The stored row for a key as [Slot Digest], or [-1 0].
(define job.find
  _ [] _ -> [-1 0]
  Key [[stored Key Digest] | _] Slot -> [Slot Digest]
  Key [_ | Jobs] Slot -> (job.find Key Jobs (+ Slot 1)))

(define job.decide
  [-1 _] [Jobs Pending Next] Req Key Digest ->
    (job.insert (job.free Jobs 0) [Jobs Pending Next] Req Key Digest)
  [Slot Digest] State Req _ Digest -> [State [[reply Req ok Slot]]]
  [Slot _] State Req _ _ -> [State [[reply Req conflict Slot]]])

(define job.free
  [] _ -> -1
  [[free _ _] | _] Slot -> Slot
  [_ | Jobs] Slot -> (job.free Jobs (+ Slot 1)))

\\ The write set is one row. Nothing is promised to the client here.
(define job.insert
  _ [Jobs [writing | Rest] Next] _ _ _ -> [[Jobs [writing | Rest] Next] []]
  -1 State Req _ _ -> [State [[reply Req full 0]]]
  Slot [Jobs _ Next] Req Key Digest ->
    [[Jobs [writing Req Slot Key Digest Next] (+ Next 1)] [[write Next Slot Key Digest]]])

\\ The disk acknowledged the row: it is durable, so record it and answer.
(define job.written
  [Jobs [writing Req Slot Key Digest Token] Next] Token ->
    [[(job.put Slot [stored Key Digest] Jobs) [idle 0 0 0 0 0] Next] [[reply Req ok Slot]]]
  State _ -> [State []])

(define job.put
  0 Row [_ | Jobs] -> [Row | Jobs]
  N Row [Job | Jobs] -> [Job | (job.put (- N 1) Row Jobs)])
