\\ What AcceptedJob owes, stated over simulator traces.
\\ A step is [Kind Node Event Effects World]; a World is [Up States Disks];
\\ a durable row is [Receipt Key Digest]. Effects are the ones that took effect, and
\\ World is the world they left: when a node dies during a step, the crash is a step of
\\ its own (kind `crash`) after it.

\\ ---- state invariant

(define inv.one-row-per-key
  [_ _ Disks] -> (inv.each-disk Disks))

(define inv.each-disk
  [] -> true
  [Rows | Disks] -> (and (inv.distinct (inv.keys Rows)) (inv.each-disk Disks)))

(define inv.keys
  [] -> []
  [[_ Key _] | Rows] -> [Key | (inv.keys Rows)])

(define inv.distinct
  [] -> true
  [K | Ks] -> (and (not (element? K Ks)) (inv.distinct Ks)))

\\ ---- trace invariants

\\ When the node answers `ok Receipt`, that row is already durable with the key and
\\ digest of the request being answered.
(define inv.ok-is-durable
  Steps -> (inv.replies ok Steps []))

\\ When the node answers `conflict`, a durable row for the key holds another digest.
(define inv.conflict-is-real
  Steps -> (inv.replies conflict Steps []))

\\ When the node answers `full`, every receipt is taken by a durable row of another key.
(define inv.full-is-real
  Steps -> (inv.replies full Steps []))

(define inv.replies
  _ [] _ -> true
  Status [[client _ [submit Req Key Digest] _ _] | Steps] Requests ->
    (inv.replies Status Steps [[Req Key Digest] | Requests])
  Status [[_ _ _ [] _] | Steps] Requests -> (inv.replies Status Steps Requests)
  Status [[_ Node _ Effects [_ _ Disks]] | Steps] Requests ->
    (if (inv.effects Status Effects Requests (nth (+ Node 1) Disks))
        (inv.replies Status Steps Requests)
        [violated Status Effects Disks]))

(define inv.effects
  _ [] _ _ -> true
  ok [[reply Req ok Receipt] | Effects] Requests Rows ->
    (and (element? [Receipt | (inv.request Req Requests)] Rows)
         (inv.effects ok Effects Requests Rows))
  conflict [[reply Req conflict Receipt] | Effects] Requests Rows ->
    (and (inv.other-digest? Receipt (inv.request Req Requests) Rows)
         (inv.effects conflict Effects Requests Rows))
  full [[reply Req full _] | Effects] Requests Rows ->
    (and (inv.no-room? (inv.request Req Requests) Rows)
         (inv.effects full Effects Requests Rows))
  Status [_ | Effects] Requests Rows -> (inv.effects Status Effects Requests Rows))

\\ The table has two receipts.
(define inv.no-room?
  [Key _] Rows -> (and (= (length Rows) 2) (not (element? Key (inv.keys Rows)))))

(define inv.request
  Req [[Req Key Digest] | _] -> [Key Digest]
  Req [_ | Requests] -> (inv.request Req Requests))

(define inv.other-digest?
  _ _ [] -> false
  Receipt [Key Digest] [[Receipt Key Other] | _] -> (not (= Other Digest))
  Receipt Request [_ | Rows] -> (inv.other-digest? Receipt Request Rows))

\\ A durable row never changes and never disappears.
(define inv.rows-are-immutable
  [[_ _ _ _ [_ _ Before]] [K N E F [U S After]] | Steps] ->
    (if (inv.kept Before After)
        (inv.rows-are-immutable [[K N E F [U S After]] | Steps])
        [violated Before After])
  _ -> true)

(define inv.kept
  [] [] -> true
  [Rows | Before] [Later | After] -> (and (inv.subset Rows Later) (inv.kept Before After)))

(define inv.subset
  [] _ -> true
  [Row | Rows] Later -> (and (element? Row Later) (inv.subset Rows Later)))

\\ ---- witnesses: histories that must be possible

\\ A reply `ok Receipt` is lost, the client retries the same key and digest, the retry is
\\ answered with the same receipt, and one job is durable for that key at the end.
(define wit.retry-after-lost-reply
  Steps -> (wit.lost Steps [] (wit.final-rows Steps)))

(define wit.lost
  [] _ _ -> false
  [[client _ [submit Req Key Digest] _ _] | Steps] Requests Final ->
    (wit.lost Steps [[Req Key Digest] | Requests] Final)
  [[drop _ [reply Req ok Receipt] _ _] | Steps] Requests Final ->
    (or (and (wit.retried Steps (inv.request Req Requests) Receipt [])
             (= (wit.count (hd (inv.request Req Requests)) Final) 1))
        (wit.lost Steps Requests Final))
  [_ | Steps] Requests Final -> (wit.lost Steps Requests Final))

\\ Retries submitted after the loss, then one of them answered with the same receipt.
(define wit.retried
  [] _ _ _ -> false
  [[client _ [submit Req Key Digest] _ _] | Steps] [Key Digest] Receipt Retries ->
    (wit.retried Steps [Key Digest] Receipt [Req | Retries])
  [[output _ [reply Req ok Receipt] _ _] | Steps] Request Receipt Retries ->
    (or (element? Req Retries) (wit.retried Steps Request Receipt Retries))
  [_ | Steps] Request Receipt Retries -> (wit.retried Steps Request Receipt Retries))

\\ A submission reaches the node, the node is then down with nothing durable (a crash
\\ before the commit), a later request for the same key is answered `ok`, and one job is
\\ durable for that key at the end.
(define wit.retry-after-precommit-crash
  Steps -> (wit.reaches Steps [] (wit.final-rows Steps)))

(define wit.reaches
  [] _ _ -> false
  [[client _ [submit Req Key Digest] _ _] | Steps] Requests Final ->
    (wit.reaches Steps [[Req Key Digest] | Requests] Final)
  [[deliver _ [submit _ Key _] _ _] | Steps] Requests Final ->
    (or (and (wit.phase 1 Key Steps Requests) (= (wit.count Key Final) 1))
        (wit.reaches Steps Requests Final))
  [_ | Steps] Requests Final -> (wit.reaches Steps Requests Final))

(define wit.phase
  _ _ [] _ -> false
  1 Key [[_ _ _ _ [[false] _ [[]]]] | Steps] Requests -> (wit.phase 2 Key Steps Requests)
  Phase Key [[client _ [submit Req K D] _ _] | Steps] Requests ->
    (wit.phase Phase Key Steps [[Req K D] | Requests])
  2 Key [[output _ [reply Req ok _] _ _] | Steps] Requests ->
    true where (= (hd (inv.request Req Requests)) Key)
  Phase Key [_ | Steps] Requests -> (wit.phase Phase Key Steps Requests))

\\ A submission with a different digest is answered `conflict`.
(define wit.conflict-rejected
  [] -> false
  [[output _ [reply _ conflict _] _ _] | _] -> true
  [_ | Steps] -> (wit.conflict-rejected Steps))

\\ With both receipts taken, a submission for a third key is answered `full`.
(define wit.table-full
  [] -> false
  [[output _ [reply _ full _] _ _] | _] -> true
  [_ | Steps] -> (wit.table-full Steps))

(define wit.final-rows
  [[_ _ _ _ [_ _ [Rows]]]] -> Rows
  [_ | Steps] -> (wit.final-rows Steps))

(define wit.count
  _ [] -> 0
  Key [[_ Key _] | Rows] -> (+ 1 (wit.count Key Rows))
  Key [_ | Rows] -> (wit.count Key Rows))
