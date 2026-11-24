\\ A step is [Kind Node Event Effects World]; a World is [Up States Disks];
\\ a node state is [Id Nodes Value Rounds] or the symbol down.

\\ No node ever holds a value that was not requested.
(define inv.only-requested-values
  Steps -> (inv.scan Steps [0]))

(define inv.scan
  [] _ -> true
  [[client _ [set V] _ _] | Steps] Known -> (inv.scan Steps [V | Known])
  [[_ _ _ _ [_ States _]] | Steps] Known ->
    (if (inv.all-known States Known) (inv.scan Steps Known) [violated States Known]))

(define inv.all-known
  [] _ -> true
  [down | States] Known -> (inv.all-known States Known)
  [[_ _ V _] | States] Known -> (and (element? V Known) (inv.all-known States Known)))

\\ Witness: the network was partitioned and still every node ends with the largest value.
(define wit.converged-after-partition
  Steps -> (and (wit.partitioned Steps)
                (and (> (wit.max Steps 0) 0) (wit.all (wit.final Steps) (wit.max Steps 0)))))

(define wit.partitioned
  [] -> false
  [[partition | _] | _] -> true
  [_ | Steps] -> (wit.partitioned Steps))

(define wit.max
  [] M -> M
  [[client _ [set V] _ _] | Steps] M -> (wit.max Steps (if (> V M) V M))
  [_ | Steps] M -> (wit.max Steps M))

(define wit.final
  [[_ _ _ _ [_ States _]]] -> States
  [_ | Steps] -> (wit.final Steps))

(define wit.all
  [] _ -> true
  [[_ _ V _] | States] V -> (wit.all States V)
  _ _ -> false)
