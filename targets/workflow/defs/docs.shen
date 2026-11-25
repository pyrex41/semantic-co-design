\\ A document package. Render three documents, each under its own key and with its own
\\ retries, let the store settle for a while, then bundle them.

(define docs.definition ->
  [3 30
   [[each render [201 202 203] 3 2 30]
    [sleep 25]
    [act bundle 204 2 3 30]
    [end]]])

(define docs.init
  _ _ -> (wf.init (docs.definition)))

(define docs.step
  State Event -> (wf.step (docs.definition) State Event))

\\ Once faults stop, the workflow is finished within this many simulator steps.
(define docs.settles-within -> 120)
