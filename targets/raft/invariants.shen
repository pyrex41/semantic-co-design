\\ The safety properties of Raft (Figure 3 of the extended paper), over simulator traces.
\\ Checker code: nothing here is shared with the node.
\\
\\ A step is [Kind Node Event Effects World]; a World is [Up States Disks]; a server state is
\\   [Id N Role Term Voted Len Log Commit Applied Kv Votes Next Match Issued Acked Hold]
\\ or the symbol down. A disk is its rows [Key | Value]: row 0 is [0 Term meta Voted Len 0].
\\ Each invariant returns true or a list that starts with `violated`.

\\ A server that is still reading its disk back has no log in memory yet: while it loads,
\\ its Len is the length it is going to have and its Log is partly filled. The invariants
\\ about logs look at running servers, so such a server counts as down.
(define inv.running
  [] -> []
  [[_ _ loading | _] | States] -> [down | (inv.running States)]
  [State | States] -> [State | (inv.running States)])

(define inv.take
  0 _ -> []
  _ [] -> []
  N [X | Xs] -> [X | (inv.take (- N 1) Xs)])

(define inv.prefix?
  [] _ -> true
  [X | Xs] [X | Ys] -> (inv.prefix? Xs Ys)
  _ _ -> false)

\\ ---- Election Safety: at most one leader can be elected in a given term.
\\ Leaders is every [Term Id] that has been a leader in any world so far.

(define inv.election-safety
  Steps -> (inv.es Steps []))

(define inv.es
  [] _ -> true
  [[_ _ _ _ [_ States _]] | Steps] Leaders ->
    (inv.es-next (inv.es-states (inv.running States) Leaders) Steps))

(define inv.es-next
  [violated | Why] _ -> [violated | Why]
  Leaders Steps -> (inv.es Steps Leaders))

(define inv.es-states
  [] Leaders -> Leaders
  [[Id _ leader Term | _] | States] Leaders -> (inv.es-add Term Id Leaders Leaders States)
  [_ | States] Leaders -> (inv.es-states States Leaders))

(define inv.es-add
  Term Id [] All States -> (inv.es-states States [[Term Id] | All])
  Term Id [[Term Id] | _] All States -> (inv.es-states States All)
  Term Id [[Term Other] | _] _ _ -> [violated two-leaders-in-term Term Id Other]
  Term Id [_ | Leaders] All States -> (inv.es-add Term Id Leaders All States))

\\ ---- Leader Append-Only: a leader never overwrites or deletes entries in its log.
\\ Between two consecutive worlds in which a server leads the same term, its old log is a
\\ prefix of its new one.

(define inv.leader-append-only
  [] -> true
  [[_ _ _ _ [_ States _]] | Steps] -> (inv.lao (inv.running States) Steps))

(define inv.lao
  _ [] -> true
  Before [[_ _ _ _ [_ States _]] | Steps] ->
    (if (inv.lao-all Before (inv.running States))
        (inv.lao (inv.running States) Steps)
        [violated leader-append-only Before States]))

(define inv.lao-all
  [] [] -> true
  [[Id N leader Term _ Len Log | _] | Before] [[Id N leader Term _ Len2 Log2 | _] | After] ->
    (and (inv.prefix? (inv.take Len Log) (inv.take Len2 Log2)) (inv.lao-all Before After))
  [_ | Before] [_ | After] -> (inv.lao-all Before After))

\\ ---- Log Matching: if two logs contain an entry with the same index and term, then the
\\ logs are identical in all entries up through that index. Checked for every pair of
\\ running servers in every world.

(define inv.log-matching
  [] -> true
  [[_ _ _ _ [_ States _]] | Steps] -> (inv.lm-next (inv.lm-all (inv.running States)) Steps))

(define inv.lm-next
  true Steps -> (inv.log-matching Steps)
  Bad _ -> Bad)

(define inv.lm-all
  [] -> true
  [down | States] -> (inv.lm-all States)
  [State | States] -> (inv.lm-and (inv.lm-one State States) States))

(define inv.lm-and
  true States -> (inv.lm-all States)
  Bad _ -> Bad)

(define inv.lm-one
  _ [] -> true
  State [down | States] -> (inv.lm-one State States)
  [Id N Role Term Voted Len Log | Rest] [[Id2 N2 Role2 Term2 Voted2 Len2 Log2 | Rest2] | States] ->
    (inv.lm-one-next (inv.lm-pair (inv.take Len Log) (inv.take Len2 Log2) true)
                     [Id N Role Term Voted Len Log | Rest] States))

(define inv.lm-one-next
  true State States -> (inv.lm-one State States)
  Bad _ _ -> Bad)

\\ Two logs entry by entry. Same: every earlier pair of entries was equal.
(define inv.lm-pair
  [] _ _ -> true
  _ [] _ -> true
  [[Term | A] | As] [[Term | B] | Bs] Same ->
    (if (and Same (= A B))
        (inv.lm-pair As Bs true)
        [violated log-matching [Term | A] [Term | B]])
  [_ | As] [_ | Bs] _ -> (inv.lm-pair As Bs false))

\\ ---- Leader Completeness: if a log entry is committed in a given term, then that entry
\\ is present in the logs of the leaders of all higher-numbered terms.
\\ Known holds, by index, every entry some server has had at or below its commit index,
\\ with the term that server was in when the entry was first seen committed. That term is
\\ at least the term in which the entry was committed, so a leader of that term or a later
\\ one must have the entry (the leader of the same term is the one that committed it). Two
\\ servers that commit different entries at one index are reported too.

(define inv.leader-completeness
  Steps -> (inv.lc Steps []))

(define inv.lc
  [] _ -> true
  [[_ _ _ _ [_ States _]] | Steps] Known ->
    (inv.lc-next (inv.lc-learn (inv.running States) Known) (inv.running States) Steps))

(define inv.lc-next
  [violated | Why] _ _ -> [violated | Why]
  Known States Steps -> (inv.lc-leaders States Steps Known))

(define inv.lc-learn
  [] Known -> Known
  [down | States] Known -> (inv.lc-learn States Known)
  [[_ _ _ Term _ _ Log Commit | _] | States] Known ->
    (inv.lc-learn-next (inv.merge (inv.take Commit Log) Known Term) States))

(define inv.lc-learn-next
  [violated | Why] _ -> [violated | Why]
  Known States -> (inv.lc-learn States Known))

\\ Extend the known entries with a server's prefix, which must agree with them.
(define inv.merge
  [] Known _ -> Known
  [E | Es] [] Term -> (inv.cons [E Term] (inv.merge Es [] Term))
  [E | Es] [[E Seen] | Known] Term -> (inv.cons [E Seen] (inv.merge Es Known Term))
  [E | _] [[F _] | _] _ -> [violated entries-differ-at-one-index E F])

(define inv.cons
  _ [violated | Why] -> [violated | Why]
  X Xs -> [X | Xs])

(define inv.lc-leaders
  [] Steps Known -> (inv.lc Steps Known)
  [[Id _ leader Term _ Len Log | _] | States] Steps Known ->
    (if (inv.lc-has (inv.take Len Log) Known Term)
        (inv.lc-leaders States Steps Known)
        [violated leader-completeness Id Term (inv.take Len Log) Known])
  [_ | States] Steps Known -> (inv.lc-leaders States Steps Known))

(define inv.lc-has
  _ [] _ -> true
  [E | Es] [[E _] | Known] Term -> (inv.lc-has Es Known Term)
  [_ | Es] [[_ Seen] | Known] Term -> (if (<= Seen Term) false (inv.lc-has Es Known Term))
  [] [[_ Seen] | Known] Term -> (if (<= Seen Term) false (inv.lc-has [] Known Term)))

\\ ---- State Machine Safety: if a server has applied a log entry at a given index, no
\\ other server will ever apply a different log entry for the same index. Also: the
\\ key-value state of every server is what its applied entries produce, each request once.

(define inv.state-machine-safety
  Steps -> (inv.sms Steps []))

(define inv.sms
  [] _ -> true
  [[_ _ _ _ [_ States _]] | Steps] Known ->
    (inv.sms-next (inv.sms-states (inv.running States) Known) Steps))

(define inv.sms-next
  [violated | Why] _ -> [violated | Why]
  Known Steps -> (inv.sms Steps Known))

(define inv.sms-states
  [] Known -> Known
  [down | States] Known -> (inv.sms-states States Known)
  [[Id _ _ _ _ _ Log _ Applied Kv | _] | States] Known ->
    (if (= Kv (inv.replay (inv.take Applied Log) [0 0] []))
        (inv.sms-states-next (inv.merge (inv.take Applied Log) Known 0) States)
        [violated state-is-not-the-applied-entries Id Kv (inv.take Applied Log)]))

(define inv.sms-states-next
  [violated | Why] _ -> [violated | Why]
  Known States -> (inv.sms-states States Known))

\\ What a list of entries does to the store. Done is the requests already executed: a
\\ put is executed the first time its request number appears.
(define inv.replay
  [] Kv _ -> Kv
  [[_ put Key Val Req] | Es] Kv Done ->
    (if (element? Req Done)
        (inv.replay Es Kv Done)
        (inv.replay Es (inv.write Key Val Kv) [Req | Done]))
  [_ | Es] Kv Done -> (inv.replay Es Kv Done))

(define inv.write
  0 Val [_ B] -> [Val B]
  1 Val [A _] -> [A Val])

\\ ---- The commit rule of 5.4.2, stated directly: a leader advances its commit index only
\\ to an entry of its own term. This is not one of the five properties; it is the rule that
\\ Figure 8 motivates, checked where it is applied, between two consecutive worlds in which
\\ a server leads the same term.

(define inv.commit-rule
  [] -> true
  [[_ _ _ _ [_ States _]] | Steps] -> (inv.cr (inv.running States) Steps))

(define inv.cr
  _ [] -> true
  Before [[_ _ _ _ [_ States _]] | Steps] ->
    (if (inv.cr-all Before (inv.running States))
        (inv.cr (inv.running States) Steps)
        [violated commit-of-an-earlier-term-by-counting Before States]))

(define inv.cr-all
  [] [] -> true
  [[Id N leader Term _ _ _ Commit | _] | Before] [[Id N leader Term _ Len Log Commit2 | _] | After] ->
    (and (or (= Commit2 Commit) (inv.cr-term (inv.take Commit2 Log) Term))
         (inv.cr-all Before After))
  [_ | Before] [_ | After] -> (inv.cr-all Before After))

\\ The last entry of a prefix has this term.
(define inv.cr-term
  [[Term | _]] Term -> true
  [_ E | Es] Term -> (inv.cr-term [E | Es] Term)
  _ _ -> false)

\\ ---- Persistent state is on stable storage before a reply that depends on it.
\\ Seen is every [Node Term Voted] that has been in row 0 of a disk. A granted vote must be
\\ one of them, and a successful AppendEntries reply for index Top needs a durable log of
\\ at least Top entries; no reply names a term the disk does not have.

(define inv.replies-are-durable
  Steps -> (inv.rd Steps []))

(define inv.rd
  [] _ -> true
  [[_ Node _ Effects [_ _ Disks]] | Steps] Seen ->
    (inv.rd-check Node Effects Disks (inv.rd-disks Disks 0 Seen) Steps))

(define inv.rd-check
  Node Effects Disks Seen Steps ->
    (if (inv.rd-effects Node Effects Seen (inv.meta (inv.disk Node Disks)))
        (inv.rd Steps Seen)
        [violated reply-before-durable Node Effects (inv.disk Node Disks)]))

(define inv.disk
  _ [] -> []
  0 [Rows | _] -> Rows
  Node [_ | Disks] -> (inv.disk (- Node 1) Disks))

\\ Row 0 of a disk as [Term Voted Len]; a disk never written is term 0, no vote, no entries.
(define inv.meta
  [[0 Term meta Voted Len _] | _] -> [Term Voted Len]
  _ -> [0 -1 0])

(define inv.rd-disks
  [] _ Seen -> Seen
  [Rows | Disks] Node Seen -> (inv.rd-disks Disks (+ Node 1) (inv.rd-add Node (inv.meta Rows) Seen)))

(define inv.rd-add
  Node [Term Voted _] Seen -> (if (element? [Node Term Voted] Seen) Seen [[Node Term Voted] | Seen]))

(define inv.rd-effects
  _ [] _ _ -> true
  Node [[vote-rsp Dest Term Granted] | Effects] Seen [DTerm DVoted DLen] ->
    (and (>= DTerm Term)
         (and (or (not Granted) (element? [Node Term Dest] Seen))
              (inv.rd-effects Node Effects Seen [DTerm DVoted DLen])))
  Node [[app-rsp _ Term Ok Top] | Effects] Seen [DTerm DVoted DLen] ->
    (and (>= DTerm Term)
         (and (or (not Ok) (>= DLen Top))
              (inv.rd-effects Node Effects Seen [DTerm DVoted DLen])))
  Node [_ | Effects] Seen Meta -> (inv.rd-effects Node Effects Seen Meta))

\\ ---- witnesses: histories the simulation must be able to show

(define wit.leader-change
  Steps -> (wit.two-terms Steps -1))

\\ Two different terms had a leader.
(define wit.two-terms
  [] _ -> false
  [[_ _ _ _ [_ States _]] | Steps] Term -> (wit.two-terms-in States Steps Term))

(define wit.two-terms-in
  [] Steps Term -> (wit.two-terms Steps Term)
  [[_ _ leader Term | _] | States] Steps Term -> (wit.two-terms-in States Steps Term)
  [[_ _ leader Other | _] | States] Steps -1 -> (wit.two-terms-in States Steps Other)
  [[_ _ leader _ | _] | _] _ _ -> true
  [_ | States] Steps Term -> (wit.two-terms-in States Steps Term))

\\ A follower cut a conflicting suffix from its log: three writes in one step.
(define wit.conflict-repaired
  [] -> false
  [[_ _ [on-app-req | _] [[write _ 0 | _] [write | _] [write | _] | _] _] | _] -> true
  [_ | Steps] -> (wit.conflict-repaired Steps))

\\ A server recovered a non-empty log from its disk.
(define wit.log-recovered
  [] -> false
  [[_ _ [loaded _ Key 1 | _] _ _] | _] -> true where (> Key 0)
  [_ | Steps] -> (wit.log-recovered Steps))

\\ Some entry was committed by three or more servers.
(define wit.committed
  [] -> false
  [[_ _ _ _ [_ States _]] | Steps] -> (if (>= (wit.count-committed States) 3) true (wit.committed Steps)))

(define wit.count-committed
  [] -> 0
  [down | States] -> (wit.count-committed States)
  [[_ _ _ _ _ _ _ Commit | _] | States] ->
    (+ (if (> Commit 0) 1 0) (wit.count-committed States)))

\\ A leader whose log was full said so.
(define wit.full
  [] -> false
  [[_ _ _ Effects _] | Steps] -> (if (wit.full-in Effects) true (wit.full Steps)))

(define wit.full-in
  [] -> false
  [[reply _ full _] | _] -> true
  [_ | Effects] -> (wit.full-in Effects))

\\ A put that was already executed came round again and was skipped: some server has
\\ applied two puts with one request number.
(define wit.retry-skipped
  [] -> false
  [[_ _ _ _ [_ States _]] | Steps] -> (if (wit.twice-in States) true (wit.retry-skipped Steps)))

(define wit.twice-in
  [] -> false
  [down | States] -> (wit.twice-in States)
  [[_ _ _ _ _ _ Log _ Applied | _] | States] ->
    (if (wit.twice (inv.take Applied Log) []) true (wit.twice-in States)))

(define wit.twice
  [] _ -> false
  [[_ put _ _ Req] | Es] Done -> (if (element? Req Done) true (wit.twice Es [Req | Done]))
  [_ | Es] Done -> (wit.twice Es Done))
