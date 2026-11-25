\\ One activity with one retry. Small enough to search with two crashes, so that a node
\\ also dies at every effect boundary of its own recovery.

(define single.definition ->
  [1 5
   [[act hold 401 2 1 5]
    [end]]])

(define single.init
  _ _ -> (wf.init (single.definition)))

(define single.step
  State Event -> (wf.step (single.definition) State Event))

\\ Once faults stop, the workflow is finished within this many simulator steps.
(define single.settles-within -> 60)
