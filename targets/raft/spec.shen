\\ One Raft server, after Figure 2 of Ongaro and Ousterhout, "In Search of an
\\ Understandable Consensus Algorithm (Extended Version)": leader election, log
\\ replication, commit index, and a key-value state machine with put and get.
\\ No membership change, no snapshots or log compaction.
\\
\\ State  [Id N Role Term Voted Len Log Commit Applied Kv Votes Next Match Issued Acked Hold]
\\   Id N      this server and the number of servers (3 or 5; at most 5)
\\   Role      loading | follower | candidate | leader | failed
\\   Term      currentTerm            (persistent)
\\   Voted     votedFor, -1 for none  (persistent)
\\   Len Log   the log: Len entries [Term Op Key Val Req] in 8 slots (persistent).
\\             Capacity is 8 entries; a leader whose log is full answers `full`.
\\   Commit    commitIndex            (volatile)
\\   Applied   lastApplied            (volatile)
\\   Kv        the state machine: the values of keys 0 and 1
\\   Votes     votes received as a candidate; a candidate's own slot is set once its
\\             vote for itself is durable and it has asked the others
\\   Next      nextIndex per server   (leader)
\\   Match     matchIndex per server  (leader); the leader's own slot is its durable length
\\   Issued    number of disk writes issued since boot; a write's token is its number
\\   Acked     the largest token acknowledged
\\   Hold      [Token Kind Dest Term Flag Index]: one reply waiting for the disk
\\
\\ Durability. The persistent state is on disk as row 0, [Term meta Voted Len 0], and one
\\ row per log index, [Term Op Key Val Req]. Rows are written in an order of which every
\\ prefix is a state this server was allowed to be in: a new entry goes out before the
\\ length that includes it, and a conflicting suffix is cut (the shorter length is written)
\\ before the entry that replaces it. A reply to an RPC leaves only when every write issued
\\ before it is acknowledged; until then it waits in Hold, and a newer reply replaces a
\\ waiting one, which is a lost message. A candidate asks for votes only after its own vote
\\ is durable, and a leader counts its own log towards a majority only up to what is durable.
\\
\\ What this specification relies on from its environment (tool/README.md, environment
\\ rules): a row is written whole or not at all; writes become durable in the order they
\\ were issued, so the acknowledgement of token k covers every token up to k; a crash keeps
\\ a prefix of the pending writes; no acknowledgement, read result or timer is delivered
\\ across a crash. Election timeouts are a fixed function of the server and the term; the
\\ environment supplies no randomness.

(define raft.init
  Id N -> [Id N loading 0 -1 0
           [[0 none 0 0 0] [0 none 0 0 0] [0 none 0 0 0] [0 none 0 0 0]
            [0 none 0 0 0] [0 none 0 0 0] [0 none 0 0 0] [0 none 0 0 0]]
           0 0 [0 0] [false false false false false] [1 1 1 1 1] [0 0 0 0 0]
           0 0 [0 none 0 0 false 0]])

(define raft.step
  [Id N | _] [restart] -> [(raft.init Id N) [[read 0 0]]]
  State [loaded _ Key Found A Tag B C D] -> (raft.loaded State Key Found A Tag B C D)
  [Id N loading | Rest] _ -> [[Id N loading | Rest] []]
  [Id N failed | Rest] _ -> [[Id N failed | Rest] []]
  State Event -> (let Result (raft.apply (raft.handle State Event)) Result))

(define raft.handle
  State [alarm 0] -> (raft.timeout State)
  State [alarm _] -> (raft.heartbeat State)
  State [written Token] -> (raft.written State Token)
  State [put Req Key Val] -> (raft.client State put Key Val Req)
  State [get Req Key] -> (raft.client State get Key 0 Req)
  [Id N | Rest] [on-vote-req Src T LastIndex LastTerm] ->
    (raft.vote-req [Id N | Rest] Src T LastIndex LastTerm) where (raft.member? Src Id N)
  [Id N | Rest] [on-vote-rsp Src T Granted] ->
    (raft.vote-rsp [Id N | Rest] Src T Granted) where (raft.member? Src Id N)
  [Id N | Rest] [on-app-req Src T Prev PrevT Has ETerm Op Key Val Req LCommit] ->
    (raft.app-req [Id N | Rest] Src T Prev PrevT Has [ETerm Op Key Val Req] LCommit)
    where (raft.member? Src Id N)
  [Id N | Rest] [on-app-rsp Src T Ok Index] ->
    (raft.app-rsp [Id N | Rest] Src T Ok Index) where (raft.member? Src Id N)
  State [on-vote-req | _] -> [State []]
  State [on-vote-rsp | _] -> [State []]
  State [on-app-req | _] -> [State []]
  State [on-app-rsp | _] -> [State []])

\\ A message counts only when it names another server of this cluster as its sender. The
\\ per-server arrays have five slots whatever N is, so a vote or an acknowledgement under
\\ an id from N to 4 would otherwise be counted towards a majority of N.
(define raft.member?
  Src Id N -> (and (>= Src 0) (and (< Src N) (not (= Src Id)))))

\\ ---- recovery: read the persistent state back, row by row, before doing anything else

(define raft.loaded
  [Id N loading _ _ _ Log | Rest] 0 Found Term _ Voted Len _ ->
    (if (and (= Found 1) (> Len 0))
        [[Id N loading Term Voted Len Log | Rest] [[read 1 1]]]
        (raft.boot [Id N loading (if (= Found 1) Term 0) (if (= Found 1) Voted -1) 0 Log | Rest]))
  [Id N loading Term Voted Len Log | Rest] Key 1 ETerm Op K V Req ->
    (if (= Key Len)
        (raft.boot [Id N loading Term Voted Len (raft.store Key [ETerm Op K V Req] Log) | Rest])
        [[Id N loading Term Voted Len (raft.store Key [ETerm Op K V Req] Log) | Rest]
         [[read (+ Key 1) (+ Key 1)]]])
  \\ A row inside the recorded length is not on the disk. The write order below and the
  \\ environment rules exclude it. If it happens all the same, part of what this server may
  \\ have acknowledged is gone: it says so by its role and takes no further part.
  [Id N loading | Rest] _ _ _ _ _ _ _ -> [[Id N failed | Rest] []]
  State _ _ _ _ _ _ _ -> [State []])

(define raft.boot
  [Id N _ Term | Rest] -> [[Id N follower Term | Rest] [[arm 0 (raft.patience Id Term)]]])

\\ The election timeout: 150 to 349, spread by server and by term.
(define raft.patience
  Id Term -> (+ 150 (shen.mod (+ (* Id 41) (* Term 59)) 200)))

\\ ---- bounded collections

\\ Entry I of a log, counting from 1; the empty entry outside it.
(define raft.entry
  _ [] -> [0 none 0 0 0]
  1 [E | _] -> E
  I [_ | Es] -> (raft.entry (- I 1) Es))

(define raft.term-at
  I Log -> (hd (raft.entry I Log)))

(define raft.store
  _ _ [] -> []
  1 E [_ | Es] -> [E | Es]
  I E [X | Es] -> [X | (raft.store (- I 1) E Es)])

\\ The first K entries, with every later slot empty. I is the index of the first slot.
(define raft.keep
  _ [] _ -> []
  K [E | Es] I -> [(if (> I K) [0 none 0 0 0] E) | (raft.keep K Es (+ I 1))])

\\ The index of the first put with this request number, or 0.
(define raft.find
  _ [] _ -> 0
  Req [[_ put _ _ Req] | _] I -> I
  Req [_ | Es] I -> (raft.find Req Es (+ I 1)))

\\ Element P of a per-server array, counting from 0; Default outside it.
(define raft.at
  _ [] Default -> Default
  0 [X | _] _ -> X
  P [_ | Xs] Default -> (raft.at (- P 1) Xs Default))

(define raft.set
  _ _ [] -> []
  0 V [_ | Xs] -> [V | Xs]
  P V [X | Xs] -> [X | (raft.set (- P 1) V Xs)])

(define raft.count
  [] -> 0
  [V | Vs] -> (+ (if V 1 0) (raft.count Vs)))

\\ How many servers are known to store entry I.
(define raft.replicas
  _ [] -> 0
  I [M | Ms] -> (+ (if (>= M I) 1 0) (raft.replicas I Ms)))

(define raft.min
  A B -> (if (< A B) A B))

\\ ---- messages to the other servers: offsets 1 to N-1 round the ring

(define raft.peer
  Id K N -> (shen.mod (+ Id K) N))

(define raft.asks
  K Id N Term Len LastTerm ->
    (if (< K N)
        [[vote-req (raft.peer Id K N) Term Len LastTerm] | (raft.asks (+ K 1) Id N Term Len LastTerm)]
        [])
    where (< K 5)
  _ _ _ _ _ _ -> [])

\\ AppendEntries for server P: the entry at its nextIndex if there is one, else a heartbeat.
(define raft.feed
  P Term Len Log Commit Next ->
    (raft.feed-entry P Term (raft.at P Next 1) Len Log Commit))

(define raft.feed-entry
  P Term I Len Log Commit ->
    (raft.app-req-of P Term (- I 1) (raft.term-at (- I 1) Log) (<= I Len) (raft.entry I Log) Commit))

(define raft.app-req-of
  P Term Prev PrevT Has [ETerm Op Key Val Req] Commit ->
    [app-req P Term Prev PrevT Has ETerm Op Key Val Req Commit])

(define raft.feeds
  K Id N Term Len Log Commit Next ->
    (if (< K N)
        [(raft.feed (raft.peer Id K N) Term Len Log Commit Next)
         | (raft.feeds (+ K 1) Id N Term Len Log Commit Next)]
        [])
    where (< K 5)
  _ _ _ _ _ _ _ _ -> [])

\\ ---- timers

\\ Election timeout: a server that is not the leader starts an election. The request for
\\ votes waits until the new term and the vote for itself are durable (raft.written).
(define raft.timeout
  [Id N leader Term | Rest] -> [[Id N leader Term | Rest] [[arm 0 (raft.patience Id Term)]]]
  [Id N _ Term _ Len Log Commit Applied Kv _ Next Match Issued Acked Hold] ->
    [[Id N candidate (+ Term 1) Id Len Log Commit Applied Kv [false false false false false]
      Next Match (+ Issued 1) Acked Hold]
     [[write (+ Issued 1) 0 (+ Term 1) meta Id Len 0] [arm 0 (raft.patience Id (+ Term 1))]]])

(define raft.heartbeat
  [Id N leader Term Voted Len Log Commit Applied Kv Votes Next Match Issued Acked Hold] ->
    [[Id N leader Term Voted Len Log Commit Applied Kv Votes Next Match Issued Acked Hold]
     [[arm 1 50] | (raft.feeds 1 Id N Term Len Log Commit Next)]]
  State -> [State []])

\\ ---- the disk acknowledged a write

(define raft.written
  [Id N Role Term Voted Len Log Commit Applied Kv Votes Next Match Issued Acked
   [HToken HKind HDest HTerm HFlag HIndex]] Token ->
    (let Acked1 (if (> Token Acked) Token Acked)
         Free (and (not (= HKind none)) (<= HToken Acked1))
         Durable (= Acked1 Issued)
         Asks (and Durable (and (= Role candidate) (not (raft.at Id Votes false))))
         Leads (and Durable (= Role leader))
         Match1 (if Leads (raft.set Id Len Match) Match)
      [[Id N Role Term Voted Len Log
        (if Leads (raft.advance 8 Term N Len Log Match1 Commit) Commit)
        Applied Kv (if Asks (raft.set Id true Votes) Votes) Next Match1 Issued Acked1
        (if Free [0 none 0 0 false 0] [HToken HKind HDest HTerm HFlag HIndex])]
       (append (if Free [(raft.release HKind HDest HTerm HFlag HIndex)] [])
               (if Asks (raft.asks 1 Id N Term Len (raft.term-at Len Log)) []))]))

(define raft.release
  vote Dest Term Flag _ -> [vote-rsp Dest Term Flag]
  _ Dest Term Flag Index -> [app-rsp Dest Term Flag Index])

\\ The largest index above Commit that a majority stores and whose entry is from the
\\ current term. Earlier entries are committed by it, never by counting replicas (5.4.2).
(define raft.advance
  0 _ _ _ _ _ Commit -> Commit
  I Term N Len Log Match Commit ->
    (if (and (> I Commit)
             (and (<= I Len)
                  (and (= (raft.term-at I Log) Term)
                       (> (* 2 (raft.replicas I Match)) N))))
        I
        (raft.advance (- I 1) Term N Len Log Match Commit)))

\\ ---- a higher term in any message: adopt it and become a follower

(define raft.step-down
  [Id N _ _ _ Len Log Commit Applied Kv Votes Next Match Issued Acked Hold] T ->
    [[Id N follower T -1 Len Log Commit Applied Kv Votes Next Match (+ Issued 1) Acked Hold]
     [[write (+ Issued 1) 0 T meta -1 Len 0]]])

\\ ---- RequestVote

(define raft.vote-req
  [Id N Role Term Voted Len Log Commit Applied Kv Votes Next Match Issued Acked Hold]
  Src T LastIndex LastTerm ->
    (let Newer (> T Term)
         Term1 (if Newer T Term)
         Voted1 (if Newer -1 Voted)
         Grant (and (= T Term1)
                    (and (or (= Voted1 -1) (= Voted1 Src))
                         (raft.up-to-date? LastTerm LastIndex (raft.term-at Len Log) Len)))
         Voted2 (if Grant Src Voted1)
         Dirty (or Newer (not (= Voted2 Voted)))
         Issued1 (if Dirty (+ Issued 1) Issued)
         Ready (= Acked Issued1)
      [[Id N (if Newer follower Role) Term1 Voted2 Len Log Commit Applied Kv Votes Next Match
        Issued1 Acked (if Ready Hold [Issued1 vote Src Term1 Grant 0])]
       (append (if Dirty [[write Issued1 0 Term1 meta Voted2 Len 0]] [])
               (append (if Grant [[arm 0 (raft.patience Id Term1)]] [])
                       (if Ready [[vote-rsp Src Term1 Grant]] [])))]))

\\ The candidate's log is at least as up-to-date as this one (5.4.1).
(define raft.up-to-date?
  LastTerm LastIndex MyTerm MyIndex ->
    (or (> LastTerm MyTerm) (and (= LastTerm MyTerm) (>= LastIndex MyIndex))))

(define raft.vote-rsp
  [Id N Role Term Voted Len Log Commit Applied Kv Votes Next Match Issued Acked Hold] Src T Granted ->
    (if (> T Term)
        (raft.step-down
          [Id N Role Term Voted Len Log Commit Applied Kv Votes Next Match Issued Acked Hold] T)
        (let Counts (and (= Role candidate) (and (= T Term) Granted))
             Votes1 (if Counts (raft.set Src true Votes) Votes)
             Wins (and Counts (> (* 2 (raft.count Votes1)) N))
             Next1 (if Wins (raft.all (+ Len 1)) Next)
             Match1 (if Wins (raft.set Id (if (= Acked Issued) Len 0) [0 0 0 0 0]) Match)
          [[Id N (if Wins leader Role) Term Voted Len Log Commit Applied Kv Votes1 Next1 Match1
            Issued Acked Hold]
           (if Wins [[arm 1 50] | (raft.feeds 1 Id N Term Len Log Commit Next1)] [])])))

(define raft.all
  X -> [X X X X X])

\\ ---- AppendEntries

(define raft.app-req
  [Id N Role Term Voted Len Log Commit Applied Kv Votes Next Match Issued Acked Hold]
  Src T Prev PrevT Has [ETerm Op Key Val Req] LCommit ->
    (let Newer (> T Term)
         Current (>= T Term)
         Term1 (if Newer T Term)
         Voted1 (if Newer -1 Voted)
         Index (+ Prev 1)
         Fits (and Current
                   (and (<= Prev Len)
                        (and (= (raft.term-at Prev Log) PrevT) (or (not Has) (< Prev 8)))))
         New (and Fits (and Has (not (and (<= Index Len) (= (raft.term-at Index Log) ETerm)))))
         Cut (and New (<= Index Len))
         Len1 (if New Index Len)
         Log1 (if New (raft.store Index [ETerm Op Key Val Req] (raft.keep Prev Log 1)) Log)
         Top (if Fits (if Has Index Prev) 0)
         Commit1 (if (> (raft.min LCommit Top) Commit) (raft.min LCommit Top) Commit)
         Issued1 (+ Issued (if New (if Cut 3 2) (if Newer 1 0)))
         Ready (= Acked Issued1)
      [[Id N (if Current follower Role) Term1 Voted1 Len1 Log1 Commit1 Applied Kv Votes Next Match
        Issued1 Acked (if Ready Hold [Issued1 app Src Term1 Fits Top])]
       (append (if New
                   (if Cut
                       [[write (+ Issued 1) 0 Term1 meta Voted1 Prev 0]
                        [write (+ Issued 2) Index ETerm Op Key Val Req]
                        [write (+ Issued 3) 0 Term1 meta Voted1 Index 0]]
                       [[write (+ Issued 1) Index ETerm Op Key Val Req]
                        [write (+ Issued 2) 0 Term1 meta Voted1 Index 0]])
                   (if Newer [[write (+ Issued 1) 0 Term1 meta Voted1 Len 0]] []))
               (append (if Current [[arm 0 (raft.patience Id Term1)]] [])
                       (if Ready [[app-rsp Src Term1 Fits Top]] [])))]))

(define raft.app-rsp
  [Id N Role Term Voted Len Log Commit Applied Kv Votes Next Match Issued Acked Hold] Src T Ok Index ->
    (if (> T Term)
        (raft.step-down
          [Id N Role Term Voted Len Log Commit Applied Kv Votes Next Match Issued Acked Hold] T)
        (let Mine (and (= Role leader) (= T Term))
             Got (and Mine Ok)
             Missed (and Mine (not Ok))
             Match0 (raft.at Src Match 0)
             Next0 (raft.at Src Next 1)
             Match1 (if (and Got (> Index Match0)) (raft.set Src Index Match) Match)
             Back (and Missed (> Next0 (+ Match0 1)))
             Next1 (if (and Got (> (+ Index 1) Next0))
                       (raft.set Src (+ Index 1) Next)
                       (if Back (raft.set Src (- Next0 1) Next) Next))
             Commit1 (if Got (raft.advance 8 Term N Len Log Match1 Commit) Commit)
             More (or Back (and Got (<= (raft.at Src Next1 1) Len)))
          [[Id N Role Term Voted Len Log Commit1 Applied Kv Votes Next1 Match1 Issued Acked Hold]
           (if More [(raft.feed Src Term Len Log Commit1 Next1)] [])])))

\\ ---- clients. Only a leader takes a request, and every request it takes becomes a log
\\ entry of its term: a get goes through the log like a put, so its answer is confirmed by
\\ a majority, and a retried request is appended again, which is also what lets entries of
\\ earlier terms commit (there is no no-op entry at the start of a term).

(define raft.client
  [Id N leader Term Voted Len Log Commit Applied Kv Votes Next Match Issued Acked Hold]
  Op Key Val Req ->
    (if (= Len 8)
        [[Id N leader Term Voted Len Log Commit Applied Kv Votes Next Match Issued Acked Hold]
         [[reply Req full 0]]]
        (raft.append-entry
          [Id N leader Term Voted Len Log Commit Applied Kv Votes Next Match Issued Acked Hold]
          (+ Len 1) [Term Op Key Val Req]))
  State _ _ _ _ -> [State []])

(define raft.append-entry
  [Id N Role Term Voted _ Log Commit Applied Kv Votes Next Match Issued Acked Hold]
  Index [ETerm Op Key Val Req] ->
    (raft.spread
      [Id N Role Term Voted Index (raft.store Index [ETerm Op Key Val Req] Log) Commit Applied Kv
       Votes Next Match (+ Issued 2) Acked Hold]
      [[write (+ Issued 1) Index ETerm Op Key Val Req]
       [write (+ Issued 2) 0 Term meta Voted Index 0]]))

(define raft.spread
  [Id N Role Term Voted Len Log Commit Applied Kv Votes Next | Rest] Writes ->
    [[Id N Role Term Voted Len Log Commit Applied Kv Votes Next | Rest]
     (append Writes (raft.feeds 1 Id N Term Len Log Commit Next))])

\\ ---- the state machine: one committed entry is applied per step, and a leader answers
\\ the client whose request it was. A put takes effect once: the first copy of a request in
\\ the log is executed and the later copies that retries produce are not (section 8).

(define raft.apply
  [[Id N Role Term Voted Len Log Commit Applied | Rest] Effects] ->
    (if (< Applied Commit)
        (raft.apply-entry [Id N Role Term Voted Len Log Commit Applied | Rest] Effects
                          (raft.entry (+ Applied 1) Log))
        [[Id N Role Term Voted Len Log Commit Applied | Rest] Effects]))

(define raft.apply-entry
  [Id N Role Term Voted Len Log Commit Applied Kv | Rest] Effects [_ Op Key Val Req] ->
    [[Id N Role Term Voted Len Log Commit (+ Applied 1)
      (if (and (= Op put) (= (raft.find Req Log 1) (+ Applied 1))) (raft.set Key Val Kv) Kv)
      | Rest]
     (if (= Role leader)
         [[reply Req ok (if (= Op get) (raft.at Key Kv 0) 0)] | Effects]
         Effects)])
