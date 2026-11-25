\\ Not part of the engine. A fixture for the check that a residual program contains no
\\ dispatch over definition syntax: a three-instruction interpreter that looks its
\\ instruction up as a value and only afterwards asks what kind it is.

(define leak.program -> [[inc 1] [dec 1] [inc 2]])

(define leak.init
  _ _ -> [0 0 idle])

(define leak.step
  [Pc Acc Seen] [tick] ->
    [[(+ Pc 1) (leak.exec (leak.fetch (leak.program) 0 Pc) Acc) Seen] []])

(define leak.fetch
  [I | Is] J Pc -> (if (= J Pc) I (leak.fetch Is (+ J 1) Pc)))

(define leak.exec
  [inc N] Acc -> (+ Acc N)
  [dec N] Acc -> (- Acc N))

(define leak.opcode
  [Op _] -> Op)
