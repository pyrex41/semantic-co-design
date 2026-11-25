\\ The smallest workflow that still has a retry, a second activity to undo and an order
\\ between the two. It is the one searched exhaustively.

(define small.definition ->
  [1 5
   [[act hold 301 2 1 5]
    [act pay 302 1 1 5]
    [end]]])

(define small.init
  _ _ -> (wf.init (small.definition)))

(define small.step
  State Event -> (wf.step (small.definition) State Event))

\\ Once faults stop, the workflow is finished within this many simulator steps.
(define small.settles-within -> 60)
