\\ An order as a saga. Reserve the stock and charge the card. A charge that comes back
\\ as 2 ("needs review") goes through a manual review; a clean charge (1) skips it. Then
\\ ship. A failure anywhere, or a cancel, undoes what was started, newest first.
\\
\\ An undo waits 45 for its acknowledgement: longer than a round trip to a worker in the
\\ seeded runs (up to 40), so that compensation is only ever abandoned after a fault.
\\ The activity timeouts of 30 are deliberately shorter than that: a slow completion is a
\\ timed-out attempt.

(define order.definition ->
  [2 45
   [[act reserve 101 2 4 60]
    [act charge 102 3 4 30]
    [branch 1 4]
    [act review 103 1 4 30]
    [act ship 104 2 4 30]
    [end]]])

(define order.init
  _ _ -> (wf.init (order.definition)))

(define order.step
  State Event -> (wf.step (order.definition) State Event))

\\ Once faults stop, the workflow is finished within this many simulator steps.
(define order.settles-within -> 100)
