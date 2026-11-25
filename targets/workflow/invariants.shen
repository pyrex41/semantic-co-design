\\ What the workflow engine owes, stated over what the simulated world shows.
\\
\\ A situation is [Node Rows Pending Ledger Told]:
\\   Node     `down`, or the node's memory [Mode Seq Pend Pos]
\\   Rows     the durable history, [[Seq Kind A B] ...] in key order
\\   Pending  rows written but not yet durable, oldest first
\\   Ledger   what the activity workers really did, newest first:
\\              [commit Name Key Result]   the activity ran and its effect is committed
\\              [fault Name Key Attempt]   the activity ran and failed; nothing is committed
\\              [reverse Name Key]         a committed effect was undone
\\              [void Name Key]            an undo arrived for a key that never committed
\\   Told     what the client was told: the outcome of every `report` delivered to it
\\
\\ The ledger and the reports belong to the environment. The engine never sees them.
\\
\\ Nothing in this file calls the engine. It reads the definition a second time, on its
\\ own (section 0), so that an engine that misreads a definition, or gives a row another
\\ meaning, disagrees with it.

\\ ---- 0. the definition, and what a history means

(define inv.program [_ _ Program] -> Program)
(define inv.undo-attempts [Attempts _ _] -> Attempts)

(define inv.instruction
  [I | _] 0 -> I
  [_ | Is] N -> (inv.instruction Is (- N 1)))

\\ What the definition has at a position: [activity Key Attempts Backoff], [sleep] or [end].
(define inv.stop
  Program Pc Item -> (inv.stop-of (inv.instruction Program Pc) Item))

(define inv.stop-of
  [act _ Key Attempts Backoff _] _ -> [activity Key Attempts Backoff]
  [each _ Keys Attempts Backoff _] Item -> [activity (inv.instruction Keys Item) Attempts Backoff]
  [sleep _] _ -> [sleep]
  [end] _ -> [end])

\\ One entry per activity instance, in program order: its key, and its activity's name.
(define inv.keys
  [] -> []
  [[act _ Key | _] | Is] -> [Key | (inv.keys Is)]
  [[each _ Keys | _] | Is] -> (append Keys (inv.keys Is))
  [_ | Is] -> (inv.keys Is))

(define inv.slots
  [] -> []
  [[act Name | _] | Is] -> [Name | (inv.slots Is)]
  [[each Name Keys | _] | Is] -> (append (inv.repeat Name Keys) (inv.slots Is))
  [_ | Is] -> (inv.slots Is))

(define inv.repeat
  _ [] -> []
  Name [_ | Keys] -> [Name | (inv.repeat Name Keys)])

\\ Where the workflow goes when the stop at [Pc Item] is over with Result: the next key of
\\ an `each`, or else the next instruction, through any branches, which take no time.
(define inv.next
  Program Pc Item Result -> (inv.leave (inv.instruction Program Pc) Program Pc Item Result))

(define inv.leave
  [each _ Keys | _] _ Pc Item _ -> [Pc (+ Item 1)] where (< (+ Item 1) (length Keys))
  _ Program Pc _ Result -> [(inv.through Program (+ Pc 1) Result) 0])

(define inv.through
  Program Pc Result -> (inv.branch (inv.instruction Program Pc) Program Pc Result))

(define inv.branch
  [branch Result Target] Program _ Result -> (inv.through Program Target Result)
  [branch _ _] Program Pc Result -> (inv.through Program (+ Pc 1) Result)
  _ _ Pc _ -> Pc)

\\ A position is [Phase Pc Item Attempt Last Marks], as the engine keeps it. Marks has one
\\ number per key, in program order: 1 from the row that schedules the key until the row
\\ that says it was undone.
(define inv.origin
  Def -> [ready (inv.through (inv.program Def) 0 0) 0 0 0 (inv.zeros (inv.keys (inv.program Def)))])

(define inv.zeros
  [] -> []
  [_ | Keys] -> [0 | (inv.zeros Keys)])

(define inv.mark
  [] [] _ _ -> []
  [Key | Keys] [_ | Marks] Key Value -> [Value | Marks]
  [_ | Keys] [M | Marks] Key Value -> [M | (inv.mark Keys Marks Key Value)])

\\ The newest key that is still marked, or 0: the one compensation has to undo next.
(define inv.newest
  [] [] Found -> Found
  [Key | Keys] [1 | Marks] _ -> (inv.newest Keys Marks Key)
  [_ | Keys] [_ | Marks] Found -> (inv.newest Keys Marks Found))

(define inv.open?
  ready -> true
  waiting -> true
  backoff -> true
  _ -> false)

\\ What a history means: the position its rows lead to, or `illegal` when some row could
\\ not have been written at the position the rows before it lead to.
(define inv.meaning
  Def Rows -> (inv.read Def (inv.origin Def) Rows))

(define inv.read
  _ illegal _ -> illegal
  _ Pos [] -> Pos
  Def [Phase Pc Item A Last Marks] [Row | Rows] ->
    (inv.read Def (inv.row Def (inv.stop (inv.program Def) Pc Item)
                           (inv.newest (inv.keys (inv.program Def)) Marks 0)
                           [Phase Pc Item A Last Marks] Row)
              Rows))

\\ One row [Kind A B] at one position. Stop is what the definition has there and Newest
\\ the key compensation would undo next. Every clause is one thing a workflow may do:
(define inv.row
  \\ an activity starts with attempt 1; attempt A + 1 follows the backoff after attempt A
  Def [activity Key _ _] _ [ready Pc Item _ Last Marks] [sched Key 1] ->
    [waiting Pc Item 1 Last (inv.mark (inv.keys (inv.program Def)) Marks Key 1)]
  _ [activity Key _ _] _ [backoff Pc Item A Last Marks] [sched Key B] ->
    [waiting Pc Item B Last Marks] where (= B (+ A 1))
  \\ the attempt in flight ends: with a result that is not 0, and the workflow goes on;
  \\ or failed or timed out, and it is tried again unless the attempts are used up
  Def [activity Key _ _] _ [waiting Pc Item _ _ Marks] [ok Key Result] ->
    (inv.arrive (inv.next (inv.program Def) Pc Item Result) Result Marks) where (not (= Result 0))
  _ [activity Key Attempts _] _ [waiting Pc Item A Last Marks] [fail Key A] ->
    (inv.failed A Attempts Pc Item Last Marks)
  _ [activity Key Attempts _] _ [waiting Pc Item A Last Marks] [late Key A] ->
    (inv.failed A Attempts Pc Item Last Marks)
  \\ a sleep is over; the last result stays what it was
  Def [sleep] _ [ready Pc Item _ Last Marks] [woke Pc 0] ->
    (inv.arrive (inv.next (inv.program Def) Pc Item Last) Last Marks)
  \\ the workflow completes at `end`, and nowhere else
  _ [end] _ [ready Pc Item _ Last Marks] [completed 0 0] -> [completed Pc Item 0 Last Marks]
  \\ a cancel is accepted while the workflow is still going forward
  _ _ _ [Phase Pc Item _ Last Marks] [cancel 0 0] -> [unwinding Pc Item 0 Last Marks]
    where (inv.open? Phase)
  \\ compensation undoes the newest marked key: a first try, then up to UndoAttempts in all
  _ _ Newest [unwinding Pc Item _ Last Marks] [undo Newest 1] -> [undoing Pc Item 1 Last Marks]
    where (not (= Newest 0))
  Def _ Newest [undoing Pc Item U Last Marks] [undo Newest V] -> [undoing Pc Item V Last Marks]
    where (and (= V (+ U 1)) (<= V (inv.undo-attempts Def)))
  Def _ Newest [undoing Pc Item _ Last Marks] [undone Newest 0] ->
    [unwinding Pc Item 0 Last (inv.mark (inv.keys (inv.program Def)) Marks Newest 0)]
  \\ it ends compensated when nothing is marked any more, and may only be abandoned after
  \\ the last allowed try of an undo
  _ _ 0 [unwinding Pc Item _ Last Marks] [compensated 0 0] -> [compensated Pc Item 0 Last Marks]
  Def _ _ [undoing Pc Item U Last Marks] [stuck 0 0] -> [stuck Pc Item 0 Last Marks]
    where (= U (inv.undo-attempts Def))
  \\ and nothing else
  _ _ _ _ _ -> illegal)

(define inv.arrive
  [Pc Item] Last Marks -> [ready Pc Item 0 Last Marks])

(define inv.failed
  A Attempts Pc Item Last Marks ->
    (if (< A Attempts) [backoff Pc Item A Last Marks] [unwinding Pc Item 0 Last Marks]))

\\ ---- 1. at most one committed effect per activity

\\ Counted over what the workers did, whatever key a request carried: no key commits
\\ twice, and no activity has more commits than the definition has instances of it.
(define inv.one-effect-per-activity
  Def [_ _ _ Ledger | _] ->
    (and (inv.distinct (inv.committed Ledger))
         (inv.fit (inv.commit-names Ledger) (inv.slots (inv.program Def)))))

(define inv.committed
  [] -> []
  [[commit _ Key _] | Ledger] -> [Key | (inv.committed Ledger)]
  [_ | Ledger] -> (inv.committed Ledger))

(define inv.commit-names
  [] -> []
  [[commit Name _ _] | Ledger] -> [Name | (inv.commit-names Ledger)]
  [_ | Ledger] -> (inv.commit-names Ledger))

(define inv.distinct
  [] -> true
  [K | Ks] -> (and (not (element? K Ks)) (inv.distinct Ks)))

\\ Every name takes a slot of its own.
(define inv.fit
  [] _ -> true
  [Name | Names] Slots -> (and (element? Name Slots) (inv.fit Names (inv.without Name Slots))))

(define inv.without
  X [X | Ys] -> Ys
  X [Y | Ys] -> [Y | (inv.without X Ys)])

\\ The history and the world agree. A worker only ever ran an activity whose intent was
\\ already durable, and only ever undid a key whose undo was already durable. A row that
\\ says an activity finished is about an effect really committed with that result; one
\\ that says an attempt failed is about that attempt really failing; one that says a key
\\ was undone is about an undo a worker really did.
(define inv.history-is-true
  _ [_ Rows _ Ledger | _] -> (and (inv.intended Ledger Rows) (inv.happened Rows Ledger)))

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
    (and (element? [commit Key Result] (inv.facts Ledger)) (inv.happened Rows Ledger))
  [[_ fail Key Attempt] | Rows] Ledger ->
    (and (element? [fault Key Attempt] (inv.facts Ledger)) (inv.happened Rows Ledger))
  [[_ undone Key _] | Rows] Ledger ->
    (and (element? Key (inv.undone Ledger)) (inv.happened Rows Ledger))
  [_ | Rows] Ledger -> (inv.happened Rows Ledger))

\\ The ledger without the names: [commit Key Result], [fault Key Attempt], [reverse Key], ...
(define inv.facts
  [] -> []
  [[Kind _ | Rest] | Ledger] -> [[Kind | Rest] | (inv.facts Ledger)])

(define inv.undone
  [] -> []
  [[reverse _ Key] | Ledger] -> [Key | (inv.undone Ledger)]
  [[void _ Key] | Ledger] -> [Key | (inv.undone Ledger)]
  [_ | Ledger] -> (inv.undone Ledger))

\\ ---- 2. the history is a log of what the definition allows, and memory is its replay

\\ Rows are numbered from 0 without a gap, and a pending write only ever continues them.
(define inv.history-is-a-log
  _ [_ Rows Pending | _] -> (inv.numbered 0 (append Rows Pending)))

(define inv.numbered
  _ [] -> true
  N [[N | _] | Rows] -> (inv.numbered (+ N 1) Rows)
  _ _ -> false)

\\ Every row, durable or still being written, is one the definition allows where it
\\ stands (inv.row): the activities of the path taken, in order, each at most Attempts
\\ times; after a cancel or a last failed attempt nothing but compensation, newest first;
\\ `completed` only at `end`, `compensated` only when everything started was undone, and
\\ `stuck` only after UndoAttempts tries of one undo.
(define inv.history-follows-definition
  Def [_ Rows Pending | _] -> (not (= illegal (inv.meaning Def (inv.tails (append Rows Pending))))))

\\ A node's position is what its first Seq durable rows mean, whether it got there row by
\\ row as they were acknowledged or by reading them back after a restart. A live node has
\\ seen every durable row except, at most, the one in flight.
(define inv.memory-is-replay
  _ [down | _] -> true
  Def [[Mode Seq Pend Pos] Rows | _] ->
    (and (<= Seq (length Rows))
         (and (= Pos (inv.meaning Def (inv.tails (inv.take Seq Rows))))
              (inv.caught-up Mode Pend Seq Rows))))

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

\\ Rows without their numbers. Maude keeps the token of a pending write in front of its
\\ row, and the same function takes it off.
(define inv.tails
  [] -> []
  [[_ | Row] | Rows] -> [Row | (inv.tails Rows)])

\\ ---- 3. a finished workflow stays finished

\\ Nothing is ever written after the row that completes, compensates or gives up.
(define inv.terminal-is-final
  _ [_ Rows Pending | _] -> (inv.closed (append Rows Pending)))

(define inv.closed
  [] -> true
  [[_ Kind _ _] | Rows] -> (if (inv.final? Kind) (= Rows []) (inv.closed Rows)))

(define inv.final?
  completed -> true
  compensated -> true
  stuck -> true
  _ -> false)

\\ ---- 4. the outcome is what the world shows

\\ Effects are undone newest first. A compensated workflow has undone every effect that was
\\ committed and nothing else is left; a completed workflow never undid anything.
(define inv.compensation-matches
  _ [_ Rows _ Ledger | _] ->
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

\\ A completed workflow did its whole path and nothing else. Walk the definition from the
\\ start, taking at every branch the result the workers really committed: the keys met on
\\ the way to `end` are exactly the committed keys, oldest first. A key on the way that
\\ never committed, or a commit that is not on the way, is not a completed workflow.
(define inv.completed-is-whole
  Def [_ Rows _ Ledger | _] ->
    (or (not (element? completed (inv.kinds Rows)))
        (= (inv.walk (inv.program Def) (inv.through (inv.program Def) 0 0) 0 0 Ledger)
           (reverse (inv.committed Ledger)))))

(define inv.walk
  Program Pc Item Last Ledger -> (inv.walk-at (inv.stop Program Pc Item) Program Pc Item Last Ledger))

(define inv.walk-at
  [end] _ _ _ _ _ -> []
  [sleep] Program Pc Item Last Ledger -> (inv.walk-on (inv.next Program Pc Item Last) Program Last Ledger)
  [activity Key _ _] Program Pc Item _ Ledger ->
    (inv.walk-past Key (inv.result Key Ledger) Program Pc Item Ledger))

(define inv.walk-past
  Key 0 _ _ _ _ -> [Key never-committed]
  Key Result Program Pc Item Ledger ->
    [Key | (inv.walk-on (inv.next Program Pc Item Result) Program Result Ledger)])

(define inv.walk-on
  [Pc Item] Program Last Ledger -> (inv.walk Program Pc Item Last Ledger))

\\ The result a key committed with, or 0.
(define inv.result
  _ [] -> 0
  Key [[commit _ Key Result] | _] -> Result
  Key [_ | Ledger] -> (inv.result Key Ledger))

\\ What the client was told is a durable fact: every report it received names the row
\\ that finished the workflow (and by 3 there is only one such row).
(define inv.reports-are-true
  _ [_ Rows _ _ Told] -> (inv.told Told (inv.kinds Rows)))

(define inv.told
  [] _ -> true
  [Outcome | Told] Kinds ->
    (and (inv.final? Outcome) (and (element? Outcome Kinds) (inv.told Told Kinds))))

\\ ---- every state invariant at once: the names of the ones a situation violates

(define inv.verdict
  Def Sit -> (inv.failing Def Sit [one-effect-per-activity history-is-true history-is-a-log
                                   history-follows-definition memory-is-replay
                                   terminal-is-final compensation-matches completed-is-whole
                                   reports-are-true]))

(define inv.failing
  _ _ [] -> []
  Def Sit [Name | Names] ->
    (if (= (inv.holds Name Def Sit) true)
        (inv.failing Def Sit Names)
        [Name | (inv.failing Def Sit Names)]))

(define inv.holds
  one-effect-per-activity Def Sit -> (inv.one-effect-per-activity Def Sit)
  history-is-true Def Sit -> (inv.history-is-true Def Sit)
  history-is-a-log Def Sit -> (inv.history-is-a-log Def Sit)
  history-follows-definition Def Sit -> (inv.history-follows-definition Def Sit)
  memory-is-replay Def Sit -> (inv.memory-is-replay Def Sit)
  terminal-is-final Def Sit -> (inv.terminal-is-final Def Sit)
  compensation-matches Def Sit -> (inv.compensation-matches Def Sit)
  completed-is-whole Def Sit -> (inv.completed-is-whole Def Sit)
  reports-are-true Def Sit -> (inv.reports-are-true Def Sit))

\\ ---- over a whole run: steps are [Kind Time Sit], oldest first
\\ Kind is the simulator's step kind and Time its clock after the step; a step in which the
\\ node died is followed by `crash`.

\\ The first step at which a state invariant does not hold, or -1.
(define inv.first
  _ _ [] _ -> -1
  Name Def [[_ _ Sit] | Steps] I ->
    (if (= (inv.holds Name Def Sit) true) (inv.first Name Def Steps (+ I 1)) I))

\\ 2, literally: a durable row never changes and never disappears.
(define inv.append-only
  Def [[_ _ [_ Before | _]] [K T [N After | R]] | Steps] ->
    (and (inv.prefix? Before After) (inv.append-only Def [[K T [N After | R]] | Steps]))
  _ _ -> true)

(define inv.prefix?
  [] _ -> true
  [X | Xs] [X | Ys] -> (inv.prefix? Xs Ys)
  _ _ -> false)

\\ 2, literally: when a node comes back live after a restart, its position is the one a
\\ live node last had at the same point of the history, if it ever was live there.
(define inv.recovery-reproduces
  _ Steps -> (inv.recoveries Steps [] false))

\\ Seen: [Seq Pos] of every live state so far, newest first. Away: was the node not live in
\\ the step before?
(define inv.recoveries
  [] _ _ -> true
  [[_ _ [[live Seq _ Pos] | _]] | Steps] Seen true ->
    (and (inv.same-as-before Seq Pos Seen) (inv.recoveries Steps [[Seq Pos] | Seen] false))
  [[_ _ [[live Seq _ Pos] | _]] | Steps] Seen false ->
    (inv.recoveries Steps [[Seq Pos] | Seen] false)
  [_ | Steps] Seen _ -> (inv.recoveries Steps Seen true))

(define inv.same-as-before
  _ _ [] -> true
  Seq Pos [[Seq Before] | _] -> (= Pos Before)
  Seq Pos [_ | Seen] -> (inv.same-as-before Seq Pos Seen))

\\ 3, literally: once a live node shows a finished workflow, every later live state shows
\\ the same one, however many crashes and restarts lie in between.
(define inv.never-regresses
  _ [] -> true
  Def [[_ _ [Node | _]] | Steps] ->
    (if (= none (inv.finished Node))
        (inv.never-regresses Def Steps)
        (inv.stays (inv.finished Node) Steps)))

\\ The finished workflow a node shows: completed, compensated or stuck, or none when the
\\ node is down, still reading its history, or the workflow is not finished.
(define inv.finished
  [live _ _ [Phase | _]] -> Phase where (inv.final? Phase)
  _ -> none)

(define inv.stays
  _ [] -> true
  Phase [[_ _ [[live _ _ [Other | _]] | _]] | Steps] -> (and (= Phase Other) (inv.stays Phase Steps))
  Phase [_ | Steps] -> (inv.stays Phase Steps))

\\ A retry waits for its backoff. Attempt A + 1 of a key is not written down earlier than
\\ Backoff * A after the row that ended attempt A became durable.
(define inv.backs-off
  Def Steps -> (inv.retries (inv.program Def) Steps []))

\\ Ended: [Key Attempt Time] for every attempt known to have ended, with the time its row
\\ was first seen durable.
(define inv.retries
  _ [] _ -> true
  Program [[_ Time [_ Rows Pending | _]] | Steps] Ended ->
    (inv.retries-after Program Steps Time (append Rows Pending) (inv.ended Rows Time Ended)))

(define inv.retries-after
  Program Steps Time Written Ended ->
    (and (inv.waited Program Written Time Ended) (inv.retries Program Steps Ended)))

(define inv.ended
  [] _ Ended -> Ended
  [[_ Kind Key A] | Rows] Time Ended ->
    (inv.ended Rows Time (if (and (or (= Kind fail) (= Kind late)) (= none (inv.end-of Key A Ended)))
                             [[Key A Time] | Ended]
                             Ended)))

(define inv.end-of
  _ _ [] -> none
  Key A [[Key A Time] | _] -> Time
  Key A [_ | Ended] -> (inv.end-of Key A Ended))

(define inv.waited
  _ [] _ _ -> true
  Program [[_ sched Key B] | Written] Time Ended ->
    (and (inv.waited-for (inv.end-of Key (- B 1) Ended) (* (inv.backoff Program Key) (- B 1)) Time)
         (inv.waited Program Written Time Ended)) where (> B 1)
  Program [_ | Written] Time Ended -> (inv.waited Program Written Time Ended))

(define inv.waited-for
  none _ _ -> false
  Since Backoff Time -> (>= (- Time Since) Backoff))

(define inv.backoff
  [[act _ Key _ Backoff _] | _] Key -> Backoff
  [[each _ Keys _ Backoff _] | _] Key -> Backoff where (element? Key Keys)
  [_ | Is] Key -> (inv.backoff Is Key))

\\ Compensation is abandoned only after a fault. In a run with no crash, no lost and no
\\ duplicated message, a workflow never ends stuck.
(define inv.stuck-needs-a-fault
  _ Steps -> (or (inv.any-fault? Steps) (not (inv.ever-stuck? Steps))))

(define inv.any-fault?
  [] -> false
  [[Kind | _] | Steps] -> (or (inv.fault? Kind) (inv.any-fault? Steps)))

(define inv.ever-stuck?
  [] -> false
  [[_ _ [_ Rows Pending | _]] | Steps] ->
    (or (element? stuck (inv.kinds (append Rows Pending))) (inv.ever-stuck? Steps)))

\\ ---- 5. once faults stop, the workflow finishes within Bound steps

\\ A fault is a crash, a lost or a duplicated message. Bound steps after the last one the
\\ node is live with a finished workflow (and by 3 it stays so). A run shorter than that
\\ must end finished. Finished includes `stuck`: abandoning compensation settles a run, and
\\ inv.history-follows-definition and inv.stuck-needs-a-fault say when that is allowed.
(define inv.settles
  Bound Steps -> (inv.finished-by Bound (inv.after-last-fault Steps Steps)))

(define inv.after-last-fault
  [] Tail -> Tail
  [[Kind | Rest] | Steps] Tail ->
    (if (inv.fault? Kind)
        (inv.after-last-fault Steps [[Kind | Rest] | Steps])
        (inv.after-last-fault Steps Tail)))

(define inv.fault?
  crash -> true
  drop -> true
  dup -> true
  lost -> true
  _ -> false)

(define inv.finished-by
  _ [] -> false
  _ [[_ _ [Node | _]] | _] -> true where (inv.resting? Node)
  0 _ -> false
  N [_ | Steps] -> (inv.finished-by (- N 1) Steps))

\\ A node at rest: live, nothing in flight to the disk, and the workflow finished. The
\\ exhaustive search asks that a world in which nothing is left to happen is at rest.
(define inv.resting?
  [live _ [none _ _] [Phase | _]] -> (inv.final? Phase)
  _ -> false)

\\ The client is told. A run that has ended without a fault left the node at rest and the
\\ client told the outcome the node shows, and nothing else. (A lost report is a fault:
\\ the engine reports once per arrival at the outcome and does not ask for a receipt.)
(define inv.client-is-told
  _ Steps -> (or (inv.any-fault? Steps) (inv.told-at-rest? (inv.last Steps))))

(define inv.last
  [[_ _ Sit]] -> Sit
  [_ | Steps] -> (inv.last Steps))

(define inv.told-at-rest?
  [[live _ [none _ _] [Phase | _]] _ _ _ [Phase]] -> (inv.final? Phase)
  _ -> false)
