(define proj-nth
  N _ -> 0 where (< N 0)
  0 [X | _] -> X
  N [_ | Rest] -> (proj-nth (- N 1) Rest)
  _ [] -> 0)

\\ Quotient and remainder truncate toward zero; a zero divisor gives zero.
(define proj-positive-remainder
  A B -> A where (< A B)
  A B -> (let R (proj-positive-remainder A (+ B B))
              (if (< R B) R (- R B))))

(define proj-remainder
  _ 0 -> 0
  A B -> (proj-remainder A (- 0 B)) where (< B 0)
  A B -> (- 0 (proj-positive-remainder (- 0 A) B)) where (< A 0)
  A B -> (proj-positive-remainder A B))

(define proj-quotient
  _ 0 -> 0
  A B -> (/ (- A (proj-remainder A B)) B))

(define proj-bin
  add A B -> (+ A B)
  sub A B -> (- A B)
  mul A B -> (* A B)
  divv A B -> (proj-quotient A B)
  modv A B -> (proj-remainder A B)
  eqv A B -> (if (= A B) 1 0)
  ltv A B -> (if (< A B) 1 0)
  minv A B -> (if (< A B) A B)
  maxv A B -> (if (< A B) B A)
  andv A B -> (if (and (not (= A 0)) (not (= B 0))) 1 0)
  orv A B -> (if (or (not (= A 0)) (not (= B 0))) 1 0))

(define proj-eval
  [lit N] _ _ _ -> N
  [arg N] _ Args _ -> (proj-nth N Args)
  [cfg N] Config _ _ -> (proj-nth N Config)
  [var N] _ _ Env -> (proj-nth N Env)
  [getv Index] Config Args Env -> (proj-nth (proj-eval Index Config Args Env) Args)
  [lenv] _ Args _ -> (length Args)
  [letv Value Body] Config Args Env ->
    (let V (proj-eval Value Config Args Env)
         (proj-eval Body Config Args [V | Env]))
  [ifv Test Yes No] Config Args Env ->
    (if (= (proj-eval Test Config Args Env) 0)
        (proj-eval No Config Args Env)
        (proj-eval Yes Config Args Env))
  [foldv Count Initial Body] Config Args Env ->
    (let N (proj-eval Count Config Args Env)
         V (proj-eval Initial Config Args Env)
         (proj-fold 0 N V Body Config Args Env))
  [Op A B] Config Args Env ->
    (proj-bin Op (proj-eval A Config Args Env) (proj-eval B Config Args Env)))

(define proj-fold
  Index Count Value _ _ _ _ -> Value where (>= Index Count)
  Index Count Value Body Config Args Env ->
    (proj-fold (+ Index 1) Count
               (proj-eval Body Config Args [Value Index | Env])
               Body Config Args Env))

(define proj-run
  Expr Config Args -> (proj-eval Expr Config Args []))

(define proj-pe-bin
  Op [lit A] [lit B] -> [lit (proj-bin Op A B)]
  add X [lit 0] -> X
  add [lit 0] X -> X
  sub X [lit 0] -> X
  mul X [lit 1] -> X
  mul [lit 1] X -> X
  divv X [lit 1] -> X
  Op A B -> [Op A B])

(define proj-pe-get
  [lit N] -> [lit 0] where (< N 0)
  [lit N] -> [arg N]
  Index -> [getv Index])

\\ The environment stores literals and the absolute levels of residual binders.
\\ Only binders emitted into the residual program advance the residual depth.
(define proj-pe-var
  [level Level] Depth -> [var (- (- Depth Level) 1)]
  [lit N] _ -> [lit N]
  0 _ -> [lit 0])

(define proj-specialize
  [lit N] _ _ _ -> [lit N]
  [arg N] _ _ _ -> [arg N]
  [cfg N] Config _ _ -> [lit (proj-nth N Config)]
  [var N] _ Env Depth -> (proj-pe-var (proj-nth N Env) Depth)
  [lenv] _ _ _ -> [lenv]
  [getv Index] Config Env Depth ->
    (proj-pe-get (proj-specialize Index Config Env Depth))
  [letv Value Body] Config Env Depth ->
    (proj-pe-let (proj-specialize Value Config Env Depth) Body Config Env Depth)
  [ifv Test Yes No] Config Env Depth ->
    (proj-pe-if (proj-specialize Test Config Env Depth) Yes No Config Env Depth)
  [foldv Count Initial Body] Config Env Depth ->
    (proj-pe-fold (proj-specialize Count Config Env Depth)
                  (proj-specialize Initial Config Env Depth) Body Config Env Depth)
  [Op A B] Config Env Depth ->
    (proj-pe-bin Op (proj-specialize A Config Env Depth)
                    (proj-specialize B Config Env Depth)))

(define proj-pe-let
  [lit N] Body Config Env Depth ->
    (proj-specialize Body Config [[lit N] | Env] Depth)
  Value Body Config Env Depth ->
    [letv Value (proj-specialize Body Config [[level Depth] | Env] (+ Depth 1))])

(define proj-pe-if
  [lit 0] _ No Config Env Depth -> (proj-specialize No Config Env Depth)
  [lit _] Yes _ Config Env Depth -> (proj-specialize Yes Config Env Depth)
  Test Yes No Config Env Depth ->
    [ifv Test (proj-specialize Yes Config Env Depth)
              (proj-specialize No Config Env Depth)])

\\ A static loop may read only its accumulator and index. An unsupported node
\\ costs more than the entire work budget, even in an unselected branch.
(define proj-static-cost
  [lit _] -> 1
  [var N] -> 1 where (and (>= N 0) (< N 2))
  [ifv Test Yes No] -> (+ 1 (+ (proj-static-cost Test)
                              (+ (proj-static-cost Yes) (proj-static-cost No))))
  [Op A B] -> (+ 1 (+ (proj-static-cost A) (proj-static-cost B)))
    where (element? Op [add sub mul divv modv eqv ltv minv maxv andv orv])
  Other -> 257)

\\ Retain the residual loop if an iteration hits the runtime integer boundary.
(define proj-static-fold
  Index Count Value _ _ -> [lit Value] where (>= Index Count)
  Index Count Value Body Residual ->
    (let Step (trap-error (proj-eval Body [] [] [Value Index]) (/. E overflow))
         (if (= Step overflow)
             Residual
             (proj-static-fold (+ Index 1) Count Step Body Residual))))

(define proj-finish-fold
  [lit N] [lit V] Body ->
    (proj-static-fold 0 N V Body [foldv [lit N] [lit V] Body])
      where (and (<= N 256) (<= (* N (proj-static-cost Body)) 256))
  Count Initial Body -> [foldv Count Initial Body])

(define proj-pe-fold
  [lit N] Initial _ _ _ _ -> Initial where (<= N 0)
  Count Initial Body Config Env Depth ->
    (proj-finish-fold Count Initial
     (proj-specialize Body Config [[level (+ Depth 1)] [level Depth] | Env] (+ Depth 2))))

(define proj-pe
  Expr Config -> (proj-specialize Expr Config [] 0))
