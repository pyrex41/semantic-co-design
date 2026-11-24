(define stage.bank
  0 [X | _] -> (if (cons? X) X [])
  N [_ | Rest] -> (stage.bank (- N 1) Rest)
  _ [] -> [])
(define stage.posrem
  A B -> A where (< A B)
  A B -> (let R (stage.posrem A (+ B B)) (if (< R B) R (- R B))))
(define stage.rem
  _ 0 -> (simple-error "division by zero")
  A B -> (stage.rem A (- 0 B)) where (< B 0)
  A B -> (- 0 (stage.posrem (- 0 A) B)) where (< A 0)
  A B -> (stage.posrem A B))
(define stage.quot
  A B -> (/ (- A (stage.rem A B)) B))
\\ Keep a small unboxed fast path. The wide form is H * 65536 + L, 0 <= L < 65536.
(define stage.parts
  [wide H L] -> [H L]
  N -> (let R (stage.rem N 65536)
            L (if (< R 0) (+ R 65536) R)
            [(/ (- N L) 65536) L]))
(define stage.pack
  H L -> (stage.pack (+ H 1) (- L 65536)) where (>= L 65536)
  H L -> (stage.pack (- H 1) (+ L 65536)) where (< L 0)
  H L -> (+ (* H 65536) L) where (and (>= H -1073741824) (< H 1073741824))
  H L -> [wide H L])
(define stage.addparts
  [H L] [I J] -> (stage.pack (+ H I) (+ L J)))
(define stage.add
  A B -> (+ A B) where (and (number? A) (number? B)
                            (< (+ A B) 70368744177664) (>= (+ A B) -70368744177664))
  A B -> (stage.addparts (stage.parts A) (stage.parts B)))
(define stage.neg
  [wide H L] -> (stage.pack (- 0 H) (- 0 L))
  A -> (- 0 A))
(define stage.lessparts
  [H L] [I J] -> (or (< H I) (and (= H I) (< L J))))
(define stage.less
  A B -> (< A B) where (and (number? A) (number? B))
  A B -> (stage.lessparts (stage.parts A) (stage.parts B)))
(define stage.abs
  A -> (if (stage.less A 0) (stage.neg A) A))
(define stage.divide-positive
  A B -> [0 A] where (stage.less A B)
  A B -> (let QR (stage.divide-positive A (stage.add B B))
              Q (stage.add (hd QR) (hd QR)) R (hd (tl QR))
              (if (stage.less R B) [Q R] [(stage.add Q 1) (stage.add R (stage.neg B))])))
(define stage.divide
  _ 0 -> (simple-error "division by zero")
  A B -> (let QR (stage.divide-positive (stage.abs A) (stage.abs B))
              Q (hd QR) R (hd (tl QR))
              [(if (= (stage.less A 0) (stage.less B 0)) Q (stage.neg Q))
               (if (stage.less A 0) (stage.neg R) R)]))
(define stage.half
  [wide H L] -> (let Bit (stage.rem H 2)
                     (stage.pack (/ (- H Bit) 2) (+ (* Bit 32768) (stage.quot L 2))))
  A -> (stage.quot A 2))
(define stage.odd
  [wide H L] -> (= 1 (stage.rem L 2))
  A -> (= 1 (stage.rem A 2)))
(define stage.times
  _ 0 -> 0
  A B -> (* A B) where (and (number? A) (number? B)
                            (< (* A B) 70368744177664) (>= (* A B) -70368744177664))
  A B -> (stage.neg (stage.times A (stage.neg B))) where (stage.less B 0)
  A B -> (let Half (stage.times A (stage.half B)) Both (stage.add Half Half)
              (if (stage.odd B) (stage.add Both A) Both)))
(define stage.div
  A B -> (stage.quot A B) where (and (number? A) (number? B))
  A B -> (hd (stage.divide A B)))
(define stage.mod
  A B -> (stage.rem A B) where (and (number? A) (number? B))
  A B -> (hd (tl (stage.divide A B))))
(define stage.word
  A -> (let R (stage.mod A 4294967296) (if (< R 0) (+ R 4294967296) R)))
(define stage.nth
  0 [X | _] -> X
  N [_ | X] -> (stage.nth (- N 1) X)
  _ [] -> 0)
(define stage.set
  0 V [] -> [V]
  0 V [_ | X] -> [V | X]
  N V [X | Y] -> [X | (stage.set (- N 1) V Y)]
  N V [] -> [0 | (stage.set (- N 1) V [])])
(define stage.expr
  N _ _ _ -> N where (number? N)
  [wide H L] _ _ _ -> [wide H L]
  [var N] S _ _ -> (stage.nth N S)
  [len] _ D _ -> (length D)
  [at N] S D K -> (stage.nth (stage.expr N S D K) D)
  [get B N] S D K -> (stage.nth (stage.expr N S D K) (stage.bank B K))
  [add A B] S D K -> (stage.add (stage.expr A S D K) (stage.expr B S D K))
  [sub A B] S D K -> (stage.add (stage.expr A S D K) (stage.neg (stage.expr B S D K)))
  [mul A B] S D K -> (stage.times (stage.expr A S D K) (stage.expr B S D K))
  [div A B] S D K -> (stage.div (stage.expr A S D K) (stage.expr B S D K))
  [mod A B] S D K -> (stage.mod (stage.expr A S D K) (stage.expr B S D K))
  [eq A B] S D K -> (if (= (stage.expr A S D K) (stage.expr B S D K)) 1 0)
  [lt A B] S D K -> (if (stage.less (stage.expr A S D K) (stage.expr B S D K)) 1 0)
  [le A B] S D K -> (if (not (stage.less (stage.expr B S D K) (stage.expr A S D K))) 1 0)
  [and A B] S D K -> (if (and (not (= 0 (stage.expr A S D K))) (not (= 0 (stage.expr B S D K)))) 1 0)
  [or A B] S D K -> (if (or (not (= 0 (stage.expr A S D K))) (not (= 0 (stage.expr B S D K)))) 1 0)
  [u32 A] S D K -> (stage.word (stage.expr A S D K))
  X _ _ _ -> (simple-error "unknown residual expression"))
(define stage.exec
  [] S K D O -> [S K D O]
  [[set N E] | X] S K D O -> (stage.exec X (stage.set N (stage.expr E S D K) S) K D O)
  [[put B N E] | X] S K D O -> (stage.exec X S (stage.set B (stage.set (stage.expr N S D K) (stage.expr E S D K) (stage.bank B K)) K) D O)
  [[emit E] | X] S K D O -> (stage.exec X S K D [(stage.expr E S D K) | O])
  [[seq | Items] | Rest] S K D O -> (stage.exec (append Items Rest) S K D O)
  [[if C T E] | X] S K D O -> (stage.exec [(if (not (= (stage.expr C S D K) 0)) T E) | X] S K D O)
  [[while C Body] | X] S K D O -> (if (not (= (stage.expr C S D K) 0)) (stage.exec [Body [while C Body] | X] S K D O) (stage.exec X S K D O))
  X _ _ _ _ -> (simple-error "unknown residual statement"))
(define stage-eval
  Statement Dynamic -> (reverse (stage.fourth (stage.exec [Statement] [] [] Dynamic []))))
(define stage.fourth
  [_ _ _ O] -> O)
