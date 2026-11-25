(define pat.handlers
  [] Default -> [emit Default]
  [[Threshold Answer] | Rest] Default ->
    [if [le Threshold [at 0]] [emit Answer] (pat.handlers Rest Default)])

(define pat.stages
  [] -> [set 0 [u32 [add [var 0] [var 2]]]]
  [[0 N] | Rest] -> [if [le N [var 2]] (pat.stages Rest) [seq]]
  [[1 N] | Rest] -> [seq [set 2 [u32 [add [var 2] N]]] (pat.stages Rest)])

(define pat.fold
  Stages -> [seq [set 0 0] [set 1 0]
             [while [lt [var 1] [len]]
               [seq [set 2 [at [var 1]]] (pat.stages Stages) [set 1 [add [var 1] 1]]]]
             [emit [var 0]]])

(define pat.binding
  _ [] -> -1
  Id [[Id Pos] | _] -> Pos
  Id [_ | Rest] -> (pat.binding Id Rest))

(define pat.match
  [] _ _ -1 -> [emit 1]
  [] _ Env Capture -> [emit [at (pat.binding Capture Env)]]
  [[0 Value] | Rest] Pos Env Capture ->
    [if [eq [at Pos] Value] (pat.match Rest (+ Pos 1) Env Capture) [emit -1]]
  [[1 Id] | Rest] Pos Env Capture ->
    (let Prior (pat.binding Id Env)
      (if (= Prior -1)
          (pat.match Rest (+ Pos 1) [[Id Pos] | Env] Capture)
          [if [eq [at Pos] [at Prior]] (pat.match Rest (+ Pos 1) Env Capture) [emit -1]]))
  [[2] | Rest] Pos Env Capture -> (pat.match Rest (+ Pos 1) Env Capture))

(define specialize
  [0 Rows Default] -> [if [eq [len] 1] (pat.handlers Rows Default) [emit -1]]
  [1 Stages] -> (pat.fold Stages)
  [2 Pattern Capture] -> [if [eq [len] (length Pattern)] (pat.match Pattern 0 [] Capture) [emit -1]])
