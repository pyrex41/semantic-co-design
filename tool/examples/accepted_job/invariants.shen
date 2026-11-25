\\ What AcceptedJob owes, stated over simulator traces.
\\ A step is [Kind Node Event Effects World]; a World is [Up States Disks];
\\ a durable row is [Receipt Key Digest].

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
  Status [_ | Effects] Requests Rows -> (inv.effects Status Effects Requests Rows))

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

\\ A reply `ok Receipt` is lost, the client retries the same digest, the retry is answered
\\ with the same receipt, and one job is durable at the end.
(define wit.retry-after-lost-reply
  Steps -> (and (wit.lost Steps []) (wit.one-row (wit.last Steps))))

(define wit.lost
  [] _ -> false
  [[client _ [submit Req Key Digest] _ _] | Steps] Requests ->
    (wit.lost Steps [[Req Key Digest] | Requests])
  [[drop _ [reply Req ok Receipt] _ _] | Steps] Requests ->
    (or (wit.retried Steps (inv.request Req Requests) Receipt []) (wit.lost Steps Requests))
  [_ | Steps] Requests -> (wit.lost Steps Requests))

\\ Retries submitted after the loss, then one of them answered with the same receipt.
(define wit.retried
  [] _ _ _ -> false
  [[client _ [submit Req Key Digest] _ _] | Steps] [Key Digest] Receipt Retries ->
    (wit.retried Steps [Key Digest] Receipt [Req | Retries])
  [[output _ [reply Req ok Receipt] _ _] | Steps] Request Receipt Retries ->
    (or (element? Req Retries) (wit.retried Steps Request Receipt Retries))
  [_ | Steps] Request Receipt Retries -> (wit.retried Steps Request Receipt Retries))

\\ A submission reaches the node, the node is then down with nothing durable (a crash
\\ before the commit), and a later retry is answered `ok` with one job durable at the end.
(define wit.retry-after-precommit-crash
  Steps -> (and (wit.phase 0 Steps) (wit.one-row (wit.last Steps))))

(define wit.phase
  _ [] -> false
  0 [[deliver _ [submit | _] _ _] | Steps] -> (wit.phase 1 Steps)
  1 [[_ _ _ _ [[false] _ [[]]]] | Steps] -> (wit.phase 2 Steps)
  2 [[output _ [reply _ ok _] _ _] | _] -> true
  Phase [_ | Steps] -> (wit.phase Phase Steps))

\\ A submission with a different digest is answered `conflict`.
(define wit.conflict-rejected
  [] -> false
  [[output _ [reply _ conflict _] _ _] | _] -> true
  [_ | Steps] -> (wit.conflict-rejected Steps))

(define wit.last
  [Step] -> Step
  [_ | Steps] -> (wit.last Steps))

(define wit.one-row
  [_ _ _ _ [_ _ [[_]]]] -> true
  _ -> false)
