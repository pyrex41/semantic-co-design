------------------------------- MODULE MCraft -------------------------------
\* A bounded model of the published raft.tla (Diego Ongaro, 2014; the file next to
\* this one is byte for byte the one at github.com/ongardie/raft.tla) and the
\* safety properties of Figure 3 of the Raft paper stated over its variables.
\* raft.tla itself states no invariants; these are ours. They are the TLA+
\* counterparts of targets/raft/invariants.shen, for comparison of the two sets.

EXTENDS raft

CONSTANTS MaxTerm, MaxLog, MaxMessages

\* Terms, logs and the message bag of raft.tla grow without bound.
Bounded ==
    /\ \A i \in Server : currentTerm[i] <= MaxTerm
    /\ \A i \in Server : Len(log[i]) <= MaxLog
    /\ \A m \in DOMAIN messages : messages[m] <= 1
    /\ Cardinality(DOMAIN messages) <= MaxMessages

\* Election Safety: at most one leader can be elected in a given term.
ElectionSafety ==
    \A e1, e2 \in elections : e1.eterm = e2.eterm => e1.eleader = e2.eleader

\* Log Matching: if two logs contain an entry with the same index and term, then
\* the logs are identical in all entries up through the given index.
LogMatching ==
    \A i, j \in Server : \A n \in 1..Len(log[i]) :
        (n <= Len(log[j]) /\ log[i][n].term = log[j][n].term)
            => SubSeq(log[i], 1, n) = SubSeq(log[j], 1, n)

\* Leader Completeness: an entry some server knows to be committed is in the log
\* of every leader whose term is at least that server's term.
LeaderCompleteness ==
    \A i, j \in Server :
        (state[i] = Leader /\ currentTerm[j] <= currentTerm[i]) =>
            \A n \in 1..commitIndex[j] :
                n <= Len(log[i]) /\ log[i][n] = log[j][n]

\* State Machine Safety: no two servers commit different entries at one index.
StateMachineSafety ==
    \A i, j \in Server : \A n \in 1..commitIndex[i] :
        n <= commitIndex[j] => log[i][n] = log[j][n]

\* Leader Append-Only: a leader never overwrites or deletes entries in its log.
LeaderAppendOnly ==
    [][\A i \in Server :
          (state[i] = Leader /\ state'[i] = Leader /\ currentTerm'[i] = currentTerm[i])
              => /\ Len(log'[i]) >= Len(log[i])
                 /\ SubSeq(log'[i], 1, Len(log[i])) = log[i]]_vars

\* What a bound with MaxTerm = 2 cannot show. Terms start at 1 and the first candidate is
\* in term 2, so at most one election ever happens. Where that holds, LeaderCompleteness
\* cannot be false: the only leader there is, is the one that committed. It is checked so
\* that the report can say so.
AtMostOneElection == Cardinality(elections) <= 1

=============================================================================
