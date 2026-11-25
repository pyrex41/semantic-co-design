\\ What the workflow engine owes, stated over what the simulated world shows.
\\
\\ A situation is [Node Rows Pending Ledger]:
\\   Node     `down`, or the node's memory [Mode Seq Pend Pos]
\\   Rows     the durable history, [[Seq Kind A B] ...] in key order
\\   Pending  rows written but not yet durable, oldest first
\\   Ledger   what the activity workers really did, newest first:
\\              [commit Name Key Result]   the activity ran and its effect is committed
\\              [fault Name Key Attempt]   the activity ran and failed; nothing is committed
\\              [reverse Name Key]         a committed effect was undone
\\              [void Name Key]            an undo arrived for a key that never committed
\\
\\ The ledger belongs to the environment. The engine never sees it.

(define inv.names
  [] -> []
  [[act Name | _] | Is] -> [Name | (inv.names Is)]
  [[each Name | _] | Is] -> [Name | (inv.names Is)]
  [_ | Is] -> (inv.names Is))

\\ ---- 1. at most one committed effect per idempotency key

\\ Every committed effect carries a key the definition declares, and no key commits twice.
(define inv.one-effect-per-key
  Def [_ _ _ Ledger] -> (inv.distinct-declared (inv.committed Ledger) (wf.keys (wf.program Def))))

(define inv.committed
  [] -> []
  [[commit _ Key _] | Ledger] -> [Key | (inv.committed Ledger)]
  [_ | Ledger] -> (inv.committed Ledger))

(define inv.distinct-declared
  [] _ -> true
  [K | Ks] Keys -> (and (element? K Keys) (and (not (element? K Ks)) (inv.distinct-declared Ks Keys))))

\\ The history and the world agree. A worker only ever ran an activity whose intent was
\\ already durable, and only ever undid a key whose undo was already durable; and a row that
\\ says an activity finished, or was undone, is about something a worker really did.
(define inv.history-is-true
  _ [_ Rows _ Ledger] ->
    (and (inv.intended Ledger Rows) (and (inv.happened Rows Ledger) (inv.answered Rows [0 0]))))

\\ A row that ends an attempt is about the attempt in flight, not about an earlier one.
(define inv.answered
  [] _ -> true
  [[_ sched Key A] | Rows] _ -> (inv.answered Rows [Key A])
  [[_ ok Key _] | Rows] [Key A] -> (inv.answered Rows [Key A])
  [[_ fail Key A] | Rows] [Key A] -> (inv.answered Rows [Key A])
  [[_ ok _ _] | _] _ -> false
  [[_ fail _ _] | _] _ -> false
  [_ | Rows] Flight -> (inv.answered Rows Flight))

(define inv.intended
  [] _ -> true
  [[commit _ Key _] | Ledger] Rows -> (and (inv.row? sched Key Rows) (inv.intended Ledger Rows))
  [[fault _ Key _] | Ledger] Rows -> (and (inv.row? sched Key Rows) (inv.intended Ledger Rows))
  [[_ _ Key] | Ledger] Rows -> (and (inv.row? undo Key Rows) (inv.intended Ledger Rows)))

(define inv.row?
  _ _ [] -> false
  Kind Key [[_ Kind Key _] | _] -> true
  Kind Key [_ | Rows] -> (inv.row? Kind Key Rows))

(define inv.happened
  [] _ -> true
  [[_ ok Key Result] | Rows] Ledger ->
    (and (element? Key (inv.keys-with Result Ledger)) (inv.happened Rows Ledger))
  [[_ undone Key _] | Rows] Ledger ->
    (and (element? Key (inv.undone Ledger)) (inv.happened Rows Ledger))
  [_ | Rows] Ledger -> (inv.happened Rows Ledger))

(define inv.keys-with
  _ [] -> []
  Result [[commit _ Key Result] | Ledger] -> [Key | (inv.keys-with Result Ledger)]
  Result [_ | Ledger] -> (inv.keys-with Result Ledger))

(define inv.undone
  [] -> []
  [[reverse _ Key] | Ledger] -> [Key | (inv.undone Ledger)]
  [[void _ Key] | Ledger] -> [Key | (inv.undone Ledger)]
  [_ | Ledger] -> (inv.undone Ledger))

\\ ---- 2. the history is a log, and memory is its replay

\\ Rows are numbered from 0 without a gap, and a pending write only ever continues them.
(define inv.history-is-a-log
  _ [_ Rows Pending _] -> (inv.numbered 0 (append Rows Pending)))

(define inv.numbered
  _ [] -> true
  N [[N | _] | Rows] -> (inv.numbered (+ N 1) Rows)
  _ _ -> false)

\\ A node's position is what applying the first Seq durable rows to the origin gives,
\\ whether it got there row by row as they were acknowledged or by reading them back after
\\ a restart. A live node has seen every durable row except, at most, the one in flight.
(define inv.memory-is-replay
  _ [down | _] -> true
  Def [[Mode Seq Pend Pos] Rows | _] ->
    (and (<= Seq (length Rows))
         (and (= Pos (inv.replay Def (wf.origin Def) (inv.take Seq Rows)))
              (inv.caught-up Mode Pend Seq Rows))))

(define inv.replay
  _ Pos [] -> Pos
  Def Pos [[_ | Row] | Rows] -> (inv.replay Def (wf.apply Def Pos Row) Rows))

(define inv.take
  0 _ -> []
  N [Row | Rows] -> [Row | (inv.take (- N 1) Rows)])

(define inv.caught-up
  loading _ _ _ -> true
  live [none _ _] Seq Rows -> (= Seq (length Rows))
  live Pend Seq Rows -> (or (= Seq (length Rows)) (= [[Seq | Pend]] (inv.drop Seq Rows))))

(define inv.drop
  0 Rows -> Rows
  N [_ | Rows] -> (inv.drop (- N 1) Rows))

\\ ---- 3. a finished workflow stays finished

\\ Nothing is ever written after the row that completes, compensates or gives up.
(define inv.terminal-is-final
  _ [_ Rows Pending _] -> (inv.closed (append Rows Pending)))

(define inv.closed
  [] -> true
  [[_ Kind _ _] | Rows] -> (if (inv.final? Kind) (= Rows []) (inv.closed Rows)))

(define inv.final?
  completed -> true
  compensated -> true
  stuck -> true
  _ -> false)

\\ ---- 4. compensation runs exactly for what completed

\\ Effects are undone newest first. A compensated workflow has undone every effect that was
\\ committed and nothing else is left; a completed workflow never undid anything.
(define inv.compensation-matches
  _ [_ Rows _ Ledger] ->
    (inv.compensated (reverse (inv.committed Ledger)) (inv.reversed Ledger) (inv.undone Ledger)
                     (inv.kinds Rows)))

\\ Committed is oldest first, Reversed and Undone are newest first.
(define inv.compensated
  Committed Reversed Undone Kinds ->
    (and (inv.suffix? Reversed Committed)
         (and (or (not (element? compensated Kinds)) (= Reversed Committed))
              (or (not (element? completed Kinds)) (= Undone [])))))

(define inv.reversed
  [] -> []
  [[reverse _ Key] | Ledger] -> [Key | (inv.reversed Ledger)]
  [_ | Ledger] -> (inv.reversed Ledger))

\\ Reversed, newest first, is the tail of the commits, oldest first: the newest commit was
\\ reversed first, then the one before it, with none skipped.
(define inv.suffix?
  Xs Ys -> (or (= Xs Ys) (inv.suffix-of-tail? Xs Ys)))

(define inv.suffix-of-tail?
  _ [] -> false
  Xs [_ | Ys] -> (inv.suffix? Xs Ys))

(define inv.kinds
  [] -> []
  [[_ Kind _ _] | Rows] -> [Kind | (inv.kinds Rows)])

\\ ---- every state invariant at once: the names of the ones a situation violates

(define inv.verdict
  Def Sit -> (inv.failing Def Sit [one-effect-per-key history-is-true history-is-a-log
                                   memory-is-replay terminal-is-final compensation-matches]))

(define inv.failing
  _ _ [] -> []
  Def Sit [Name | Names] ->
    (if (= (inv.holds Name Def Sit) true)
        (inv.failing Def Sit Names)
        [Name | (inv.failing Def Sit Names)]))

(define inv.holds
  one-effect-per-key Def Sit -> (inv.one-effect-per-key Def Sit)
  history-is-true Def Sit -> (inv.history-is-true Def Sit)
  history-is-a-log Def Sit -> (inv.history-is-a-log Def Sit)
  memory-is-replay Def Sit -> (inv.memory-is-replay Def Sit)
  terminal-is-final Def Sit -> (inv.terminal-is-final Def Sit)
  compensation-matches Def Sit -> (inv.compensation-matches Def Sit))

\\ Maude keeps the token of a pending write in front of its row.
(define inv.tails
  [] -> []
  [[_ | Row] | Rows] -> [Row | (inv.tails Rows)])

\\ ---- over a whole run: steps are [Kind Sit], oldest first
\\ Kind is the simulator's step kind; a step in which the node died is followed by `crash`.

\\ The first step at which a state invariant does not hold, or -1.
(define inv.first
  _ _ [] _ -> -1
  Name Def [[_ Sit] | Steps] I ->
    (if (= (inv.holds Name Def Sit) true) (inv.first Name Def Steps (+ I 1)) I))

\\ 2, literally: a durable row never changes and never disappears.
(define inv.append-only
  [[_ [_ Before | _]] [K [N After | R]] | Steps] ->
    (and (inv.prefix? Before After) (inv.append-only [[K [N After | R]] | Steps]))
  _ -> true)

(define inv.prefix?
  [] _ -> true
  [X | Xs] [X | Ys] -> (inv.prefix? Xs Ys)
  _ _ -> false)

\\ 2, literally: when a node comes back live after a restart, its position is the one a
\\ live node last had at the same point of the history, if it ever was live there.
(define inv.recovery-reproduces
  Steps -> (inv.recoveries Steps [] false))

\\ Seen: [Seq Pos] of every live state so far, newest first. Away: was the node not live in
\\ the step before?
(define inv.recoveries
  [] _ _ -> true
  [[_ [[live Seq _ Pos] | _]] | Steps] Seen true ->
    (and (inv.same-as-before Seq Pos Seen) (inv.recoveries Steps [[Seq Pos] | Seen] false))
  [[_ [[live Seq _ Pos] | _]] | Steps] Seen false ->
    (inv.recoveries Steps [[Seq Pos] | Seen] false)
  [_ | Steps] Seen _ -> (inv.recoveries Steps Seen true))

(define inv.same-as-before
  _ _ [] -> true
  Seq Pos [[Seq Before] | _] -> (= Pos Before)
  Seq Pos [_ | Seen] -> (inv.same-as-before Seq Pos Seen))

\\ 3, literally: once a live node shows a finished workflow, every later live state shows
\\ the same one.
(define inv.never-regresses
  [] -> true
  [[_ [[live _ _ [Phase | _]] | _]] | Steps] ->
    (if (inv.final? Phase) (inv.stays Phase Steps) (inv.never-regresses Steps))
  [_ | Steps] -> (inv.never-regresses Steps))

(define inv.stays
  _ [] -> true
  Phase [[_ [[live _ _ [Other | _]] | _]] | Steps] -> (and (= Phase Other) (inv.stays Phase Steps))
  Phase [_ | Steps] -> (inv.stays Phase Steps))

\\ ---- 5. once faults stop, the workflow finishes within Bound steps

\\ A fault is a crash, a lost or a duplicated message. Bound steps after the last one the
\\ node is live with a finished workflow (and by 3 it stays so). A run shorter than that
\\ must end finished.
(define inv.settles
  Bound Steps -> (inv.finished-by Bound (inv.after-last-fault Steps Steps)))

(define inv.after-last-fault
  [] Tail -> Tail
  [[Kind Sit] | Steps] Tail ->
    (if (inv.fault? Kind)
        (inv.after-last-fault Steps [[Kind Sit] | Steps])
        (inv.after-last-fault Steps Tail)))

(define inv.fault?
  crash -> true
  drop -> true
  dup -> true
  lost -> true
  _ -> false)

(define inv.finished-by
  _ [] -> false
  _ [[_ [Node | _]] | _] -> true where (inv.resting? Node)
  0 _ -> false
  N [_ | Steps] -> (inv.finished-by (- N 1) Steps))

\\ A node at rest: live, nothing in flight to the disk, and the workflow finished. The
\\ exhaustive search asks that a world in which nothing is left to happen is at rest.
(define inv.resting?
  [live _ [none _ _] [Phase | _]] -> (inv.final? Phase)
  _ -> false)
