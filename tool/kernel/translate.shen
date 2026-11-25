\\ Cases where a careless translation of Shen to Maude gives a different answer.
\\ tool/kernel_check.py evaluates each call in shen-jit and in the translated module.

\\ A wildcard next to a variable that happens to be called W1.
(define t.wild
  W1 _ -> W1)

(define t.boom
  0 -> 0)

(define t.pos
  0 -> true)

\\ A guard that is an error is an error, not a reason to try the next clause.
(define t.guard
  X -> 1 where (= (t.boom X) 0)
  _ -> 2)

(define t.guard2
  X -> 1 where (t.pos X)
  _ -> 2)

\\ A guard that is false goes on to the next clause, and off the end of the function.
(define t.guard3
  X -> 1 where (> X 2)
  X -> 2 where (> X 0)
  0 -> 3)

\\ = is strict in both operands.
(define t.eq
  X -> (= (t.boom X) 1))

(define t.mixed
  X -> [(= X true) (= X 1) (= [X] [1]) (= [X true] [1 true])])

\\ A binding nobody uses is still evaluated; the arm not taken is not.
(define t.unused
  X -> (let Y (t.boom X) 5))

(define t.lazy
  X -> (if (= X 0) 1 (t.boom X)))

(define t.and
  X -> (and (> X 0) (= (t.boom X) 0)))

(define t.or
  X -> (or (= X 0) (= (t.boom X) 0)))

(define t.repeat
  X X -> same
  _ _ -> different)

(define t.compare
  A B -> [(< A B) (> A B) (<= A B) (>= A B)])

(define t.calc
  0 A B -> (+ A B)
  1 A B -> (- A B)
  2 A B -> (* A B)
  3 A B -> (shen.mod A B)
  4 A B -> (/ (- A (shen.mod A B)) B))
