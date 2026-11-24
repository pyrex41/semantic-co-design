; Relational query specialization contract.  The static query id is consumed
; by the shared staged backend.  Input rows remain dynamic.
(define specialize
  Query -> [seq [set 1 0]
                 [while [lt [var 1] [len]]
                   [seq [emit [at [var 1]]] [set 1 [add [var 1] 1]]]]])
