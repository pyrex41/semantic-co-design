\\ One of each instruction the two small definitions do not have, for exhaustive search:
\\ an `each` over two keys, a branch that is taken or not on the result of the second scan,
\\ and a sleep. A scan that comes back 1 is clean and skips the fix; 2 asks for it.

(define mix.definition ->
  [1 5
   [[each scan [501 502] 1 1 5]
    [branch 1 3]
    [act fix 503 1 1 5]
    [sleep 3]
    [end]]])

(define mix.init
  _ _ -> (wf.init (mix.definition)))

(define mix.step
  State Event -> (wf.step (mix.definition) State Event))

\\ Once faults stop, the workflow is finished within this many simulator steps.
(define mix.settles-within -> 80)
