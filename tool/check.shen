\\ Helpers for evaluating invariants over simulator traces. Checker code, not node code.
\\ A step is [Kind Node Event Effects World] and a World is [Up States Disks].

(define tool.first-bad
  _ [] _ -> -1
  F [[_ _ _ _ World] | Steps] I -> (if (= (F World) true) (tool.first-bad F Steps (+ I 1)) I))
