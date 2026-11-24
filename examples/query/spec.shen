; Relational query specialization contract.  The static query id is consumed
; by the shared staged backend.  Input rows remain dynamic.
(define specialize
  Query -> [seq [set 0 [len]] [set 1 0]
                 [while [lt [get 1 0] [len]]
                   [seq [emit [at [get 1 0]]] [set 1 [add [get 1 0] 1]]]]])
