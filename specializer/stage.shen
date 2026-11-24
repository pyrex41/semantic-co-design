(define stage.nth
  0 [X | _] -> X
  N [_ | X] -> (stage.nth (- N 1) X)
  _ [] -> 0)
(define stage.set
  0 V [_ | X] -> [V | X]
  N V [X | Y] -> [X | (stage.set (- N 1) V Y)]
  _ _ [] -> [])
(define stage.zeros
  0 -> []
  N -> [0 | (stage.zeros (- N 1))])
(define stage.expr
  N _ _ _ -> N where (number? N)
  [var N] S _ _ -> (stage.nth N S)
  [len] _ D _ -> (length D)
  [at N] S D K -> (stage.nth (stage.expr N S D K) D)
  [get B N] _ _ K -> (stage.nth N (stage.nth B K))
  [add A B] S D K -> (+ (stage.expr A S D K) (stage.expr B S D K))
  [sub A B] S D K -> (- (stage.expr A S D K) (stage.expr B S D K))
  [mul A B] S D K -> (* (stage.expr A S D K) (stage.expr B S D K))
  [div A B] S D K -> (/ (stage.expr A S D K) (stage.expr B S D K))
  [mod A B] S D K -> (mod (stage.expr A S D K) (stage.expr B S D K))
  [eq A B] S D K -> (= (stage.expr A S D K) (stage.expr B S D K))
  [lt A B] S D K -> (< (stage.expr A S D K) (stage.expr B S D K))
  [le A B] S D K -> (<= (stage.expr A S D K) (stage.expr B S D K))
  [and A B] S D K -> (and (stage.expr A S D K) (stage.expr B S D K))
  [or A B] S D K -> (or (stage.expr A S D K) (stage.expr B S D K))
  [u32 A] S D K -> (mod (stage.expr A S D K) 4294967296)
  _ _ _ _ -> 0)
(define stage.exec
  [] S K D O -> [S K D O]
  [[set N E] | X] S K D O -> (stage.exec X (stage.set N (stage.expr E S D K) S) K D O)
  [[put B N E] | X] S K D O -> (stage.exec X S (stage.set B (stage.set N (stage.expr E S D K) (stage.nth B K)) K) D O)
  [[emit E] | X] S K D O -> (stage.exec X S K D [O | (stage.expr E S D K)])
  [[seq | X] | Y] S K D O -> (stage.exec [X | Y] S K D O)
  [[if C T E] | X] S K D O -> (stage.exec [(if (stage.expr C S D K) T E) | X] S K D O)
  [[while C Body] | X] S K D O -> (if (stage.expr C S D K) (stage.exec [Body [while C Body] | X] S K D O) (stage.exec X S K D O))
  _ S K D O -> [S K D O])
(define stage-eval
  Statement Dynamic -> (stage.reverse (stage.fourth (stage.exec Statement (stage.zeros 64) (stage.zeros 64) Dynamic []))))
(define stage-wrapper
  Statement Dynamic -> (stage-eval Statement Dynamic))
(define stage.fourth
  [_ _ _ O] -> O)
(define stage.reverse
  [] -> []
  [X | Y] -> (append (stage.reverse Y) [X]))
