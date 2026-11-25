\\ A durable workflow engine: an interpreter of workflow definitions.
\\
\\ Definition   [UndoAttempts UndoTimeout Program]
\\ Program      a list of instructions. A position in it is an index, the Pc.
\\
\\   [act Name Key Attempts Backoff Timeout]    run activity Name under idempotency key Key
\\   [each Name Keys Attempts Backoff Timeout]  the same activity once per key, in order
\\   [sleep Delay]                              wait for a timer
\\   [branch Result Target]                     if the last activity result is Result, go on
\\                                              at Target (targets are forward)
\\   [end]                                      the workflow is complete
\\
\\ An activity is tried up to Attempts times. An attempt ends with a result (0 is a
\\ failure) or with its Timeout; attempt A + 1 starts Backoff * A later. When the attempts
\\ are used up, or a cancel is accepted, every activity that was ever started is undone,
\\ newest first (the saga), each undo tried up to UndoAttempts times. An undo that is not
\\ acknowledged within UndoTimeout that many times ends the workflow `stuck`: compensation
\\ is abandoned, and only then.
\\
\\ State    [Mode Seq Pend Pos]
\\ Mode     loading (reading the history back) or live
\\ Seq      how many history rows this memory has applied
\\ Pend     the row whose disk write is in flight, or [none 0 0]
\\ Pos      [Phase Pc Item Attempt Last Marks], a function of the first Seq rows only
\\ Marks    one number per idempotency key, in program order: 1 once the key was started
\\          and until it is undone. Keys are positive and distinct (definition.shen).
\\
\\ History  row Seq on disk is [Kind A B]. Nothing is decided in memory alone: an event
\\          that changes the position becomes a row, the row is written, and only its
\\          acknowledgement applies it (wf.apply) and performs what the new position asks
\\          for (wf.plan). After a restart the rows are read back one by one, each is applied
\\          with the same wf.apply, and the same wf.plan resumes the work.
\\
\\ Timers  a timer is armed for the position it belongs to and carries Seq, the length of
\\          the history at that position, as its id. An expiry counts only while the node
\\          is still at that Seq; any other is an old timer and is ignored. Nothing is
\\          assumed about a timer source replacing or cancelling timers. A timer does not
\\          survive a crash as a deadline: after a restart the timeout, the backoff or the
\\          sleep of the position starts again from zero.
\\
\\ Relied on from the environment: a row is written whole or not at all, in order. The
\\ number of an acknowledgement and of a read result is checked here, so a completion that
\\ arrives across a crash is not relied on to be absent. Relied on from the activity
\\ workers: a key commits at most once, and a key that was undone never commits afterwards.

(define wf.program [_ _ Program] -> Program)
(define wf.undo-attempts [Attempts _ _] -> Attempts)
(define wf.undo-timeout [_ Timeout _] -> Timeout)

\\ Every idempotency key of a program, in program order.
(define wf.keys
  [] -> []
  [[act _ Key _ _ _] | Is] -> [Key | (wf.keys Is)]
  [[each _ Keys _ _ _] | Is] -> (append Keys (wf.keys Is))
  [_ | Is] -> (wf.keys Is))

(define wf.unmarked
  [] -> []
  [_ | Keys] -> [0 | (wf.unmarked Keys)])

\\ The position before any history.
(define wf.origin
  Def -> [ready (wf.settle (wf.program Def) 0 0) 0 0 0 (wf.unmarked (wf.keys (wf.program Def)))])

(define wf.init
  Def -> [loading 0 [none 0 0] (wf.origin Def)])

\\ ---- one step

(define wf.step
  Def _ [restart] -> [(wf.init Def) [[read 0 0]]]
  Def [loading Seq _ Pos] [loaded _ Seq Found Kind A B] -> (wf.loaded Def Seq Pos Found [Kind A B])
  Def [live Seq [none _ _] Pos] Event -> (wf.append Seq Pos (wf.decide Def Seq Pos Event))
  Def [live Seq Row Pos] [written Seq] -> (wf.enter Def (+ Seq 1) (wf.apply Def Pos Row))
  _ State _ -> [State []])

\\ Replay: a row that is there is applied and the next one is asked for; the first row that
\\ is not there ends the history.
(define wf.loaded
  Def Seq Pos 1 Row -> [[loading (+ Seq 1) [none 0 0] (wf.apply Def Pos Row)]
                        [[read (+ Seq 1) (+ Seq 1)]]]
  Def Seq Pos _ _ -> (wf.enter Def Seq Pos))

\\ The position is durable. Do what it asks for.
(define wf.enter
  Def Seq Pos -> (wf.entered Seq Pos (wf.plan Def Seq Pos)))

(define wf.entered
  Seq Pos [Pend Effects] -> [[live Seq Pend Pos] Effects])

\\ A decision is written before anything else happens.
(define wf.append
  Seq Pos [none _ _] -> [[live Seq [none 0 0] Pos] []]
  Seq Pos Row -> [[live Seq Row Pos] [[write Seq Seq | Row]]])

\\ ---- what an event decides: the row it adds to the history, or [none 0 0]

\\ A completion counts for the key and the attempt in flight; a timer for the Seq it was
\\ armed at. A timeout is recorded as `late`, a reported failure as `fail`.
(define wf.decide
  Def _ [waiting Pc Item A _ _] [done Key A Result] -> (wf.outcome Key A Result)
    where (= Key (wf.key Def Pc Item))
  Def Seq [waiting Pc Item A _ _] [timer Seq] -> [late (wf.key Def Pc Item) A]
  Def Seq [backoff Pc Item A _ _] [timer Seq] -> [sched (wf.key Def Pc Item) (+ A 1)]
  _ Seq [ready Pc _ _ _ _] [timer Seq] -> [woke Pc 0]
  Def _ [undoing _ _ _ _ Marks] [reverted Key] -> [undone Key 0]
    where (= Key (wf.newest (wf.keys (wf.program Def)) Marks))
  Def Seq [undoing _ _ U _ Marks] [timer Seq] ->
    (wf.again Def (wf.newest (wf.keys (wf.program Def)) Marks) U)
  _ _ [Phase | _] [cancel] -> [cancel 0 0]
    where (wf.open? Phase)
  _ _ _ _ -> [none 0 0])

(define wf.outcome
  Key A 0 -> [fail Key A]
  Key _ Result -> [ok Key Result])

(define wf.again
  Def Key U -> (if (< U (wf.undo-attempts Def)) [undo Key (+ U 1)] [stuck 0 0]))

(define wf.open?
  ready -> true
  waiting -> true
  backoff -> true
  _ -> false)

\\ ---- what a row does to the position

(define wf.apply
  Def [_ Pc Item _ Last Marks] [sched Key A] ->
    [waiting Pc Item A Last (wf.mark (wf.keys (wf.program Def)) Marks Key 1)]
  Def [_ Pc Item _ _ Marks] [ok _ Result] ->
    (wf.arrive (wf.after (wf.program Def) 0 Pc Item Result (wf.program Def)) Result Marks)
  Def Pos [fail _ A] -> (wf.failed Def Pos A)
  Def Pos [late _ A] -> (wf.failed Def Pos A)
  Def [_ Pc Item _ Last Marks] [woke _ _] ->
    (wf.arrive (wf.after (wf.program Def) 0 Pc Item Last (wf.program Def)) Last Marks)
  _ [_ Pc Item _ Last Marks] [cancel _ _] -> [unwinding Pc Item 0 Last Marks]
  _ [_ Pc Item _ Last Marks] [undo _ U] -> [undoing Pc Item U Last Marks]
  Def [_ Pc Item _ Last Marks] [undone Key _] ->
    [unwinding Pc Item 0 Last (wf.mark (wf.keys (wf.program Def)) Marks Key 0)]
  _ [_ Pc Item _ Last Marks] [completed _ _] -> [completed Pc Item 0 Last Marks]
  _ [_ Pc Item _ Last Marks] [compensated _ _] -> [compensated Pc Item 0 Last Marks]
  _ [_ Pc Item _ Last Marks] [stuck _ _] -> [stuck Pc Item 0 Last Marks])

\\ An attempt that failed or timed out: another one after a backoff, or the saga.
(define wf.failed
  Def [_ Pc Item _ Last Marks] A ->
    (if (< A (wf.attempts (wf.activity (wf.program Def) 0 Pc Item)))
        [backoff Pc Item A Last Marks]
        [unwinding Pc Item 0 Last Marks]))

(define wf.arrive
  [Pc Item] Last Marks -> [ready Pc Item 0 Last Marks])

(define wf.mark
  [] [] _ _ -> []
  [K | Keys] [M | Marks] Key Value -> [(if (= K Key) Value M) | (wf.mark Keys Marks Key Value)])

\\ Where the workflow is once the instruction at Pc has finished with Result: [Pc Item].
(define wf.after
  [I | Is] J Pc Item Result Program ->
    (if (= J Pc) (wf.leave I J Item Result Program) (wf.after Is (+ J 1) Pc Item Result Program)))

(define wf.leave
  [each _ Keys _ _ _] J Item _ _ -> [J (+ Item 1)] where (< (+ Item 1) (length Keys))
  _ J _ Result Program -> [(wf.settle Program (+ J 1) Result) 0])

\\ Branches take no time: go on to the next instruction that does something.
(define wf.settle
  Program Pc Result -> (wf.settle-at (wf.instruction Program Pc) Program Pc Result))

(define wf.settle-at
  [branch Value Target] Program Pc Result ->
    (if (= Result Value) (wf.settle Program Target Result) (wf.settle Program (+ Pc 1) Result))
  _ _ Pc _ -> Pc)

(define wf.instruction
  [I | _] 0 -> I
  [_ | Is] N -> (wf.instruction Is (- N 1)))

\\ The activity at a position: [Name Key Attempts Backoff Timeout].
(define wf.activity
  [I | Is] J Pc Item ->
    (if (= J Pc) (wf.activity-of I Item) (wf.activity Is (+ J 1) Pc Item)))

(define wf.activity-of
  [act Name Key Attempts Backoff Timeout] _ -> [Name Key Attempts Backoff Timeout]
  [each Name Keys Attempts Backoff Timeout] Item ->
    [Name (wf.pick Keys 0 Item) Attempts Backoff Timeout])

(define wf.pick
  [K | Keys] J Item -> (if (= J Item) K (wf.pick Keys (+ J 1) Item)))

(define wf.key
  Def Pc Item -> (hd (tl (wf.activity (wf.program Def) 0 Pc Item))))

(define wf.attempts
  [_ _ Attempts _ _] -> Attempts)

\\ The newest key that is still marked, or 0.
(define wf.newest
  [] [] -> 0
  [K | Keys] [M | Marks] -> (wf.newer (wf.newest Keys Marks) K M))

(define wf.newer
  0 K 1 -> K
  Later _ _ -> Later)

\\ The name of the activity a key belongs to.
(define wf.name
  [[act Name K _ _ _] | Is] Key -> (if (= K Key) Name (wf.name Is Key))
  [[each Name Keys _ _ _] | Is] Key -> (if (element? Key Keys) Name (wf.name Is Key))
  [_ | Is] Key -> (wf.name Is Key))

\\ ---- what a position asks for: [Pend Effects]

(define wf.plan
  Def Seq [ready Pc Item _ _ _] -> (wf.start (wf.program Def) 0 Pc Item Seq)
  Def Seq [waiting Pc Item A _ _] -> (wf.request (wf.activity (wf.program Def) 0 Pc Item) A Seq)
  Def Seq [backoff Pc Item A _ _] -> (wf.pause (wf.activity (wf.program Def) 0 Pc Item) A Seq)
  Def Seq [unwinding _ _ _ _ Marks] ->
    (wf.unwind (wf.newest (wf.keys (wf.program Def)) Marks) Seq)
  Def Seq [undoing _ _ U _ Marks] ->
    (wf.revert Def (wf.newest (wf.keys (wf.program Def)) Marks) U Seq)
  _ _ [Final | _] -> [[none 0 0] [[report Final]]])

(define wf.start
  [I | Is] J Pc Item Seq ->
    (if (= J Pc) (wf.begin I J Item Seq) (wf.start Is (+ J 1) Pc Item Seq)))

(define wf.begin
  [act _ Key _ _ _] _ _ Seq -> (wf.intend Seq [sched Key 1])
  [each _ Keys _ _ _] _ Item Seq -> (wf.intend Seq [sched (wf.pick Keys 0 Item) 1])
  [sleep Delay] _ _ Seq -> [[none 0 0] [[arm Seq Delay]]]
  [end] _ _ Seq -> (wf.intend Seq [completed 0 0]))

(define wf.intend
  Seq Row -> [Row [[write Seq Seq | Row]]])

(define wf.request
  [Name Key _ _ Timeout] A Seq -> [[none 0 0] [[run Name Key A] [arm Seq Timeout]]])

(define wf.pause
  [_ _ _ Backoff _] A Seq -> [[none 0 0] [[arm Seq (* Backoff A)]]])

(define wf.unwind
  0 Seq -> (wf.intend Seq [compensated 0 0])
  Key Seq -> (wf.intend Seq [undo Key 1]))

(define wf.revert
  Def Key U Seq -> [[none 0 0] [[revert (wf.name (wf.program Def) Key) Key U]
                                [arm Seq (wf.undo-timeout Def)]]])
