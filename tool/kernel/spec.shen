\\ A node written only to exercise the compiler: runtime clause selection, repeated
\\ pattern variables, guards, joins of different shapes, system functions and traps.

(define k.init _ _ -> [idle 0 false [[empty 0 0] [empty 0 0]]])

(define k.step
  [Mode Owner Flag Slots] [put K V] -> (k.put (k.slot K Slots 1) [Mode Owner Flag Slots] K V)
  [Mode Owner Flag Slots] [get K] -> [[Mode Owner Flag Slots] [(k.answer K (k.find K Slots))]]
  [idle _ Flag Slots] [lock Who] -> [[held Who Flag Slots] [[granted Who]]]
  [held Who Flag Slots] [lock Who] -> [[held Who (not Flag) Slots] []]
  [held Owner Flag Slots] [lock Who] -> [[held Owner Flag Slots] [[denied Who Owner]]]
  [held Who Flag Slots] [unlock Who] ->
    [[idle 0 Flag Slots] (append (k.notes Who Slots) [[released Who]])]
  State [math A B] -> [State [[total (k.math A B)]]] where (and (> B 0) (and (> A B) (not (= B 3))))
  State [math A B] ->
    [State (if (= A B) [] [[total (nth A [10 20 30])] [total (length (hd (tl (tl (tl State)))))]])]
  State [flag B] -> (k.flag State B)
  State _ -> [State []])

(define k.slot
  _ [] _ -> 0
  K [[full K _] | _] I -> I
  _ [[empty _ _] | _] I -> I
  K [_ | Slots] I -> (k.slot K Slots (+ I 1)))

(define k.put
  0 State K _ -> [State [[rejected K]]]
  I [Mode Owner Flag Slots] K V -> [[Mode Owner Flag (k.set I [full K V] Slots)] [[stored K I]]])

(define k.set
  1 Row [_ | Slots] -> [Row | Slots]
  I Row [Slot | Slots] -> [Slot | (k.set (- I 1) Row Slots)])

(define k.find
  _ [] -> [false 0]
  K [[full K V] | _] -> [true V]
  K [_ | Slots] -> (k.find K Slots))

(define k.answer
  K [true V] -> [found K V]
  K [false _] -> [missing K])

(define k.notes
  _ [] -> []
  Who [[full K V] | Slots] -> [[note Who K] | (k.notes Who Slots)] where (> V 1)
  Who [_ | Slots] -> (k.notes Who Slots))

(define k.math
  A B -> (let Q (/ (- A (shen.mod A B)) B)
              R (shen.mod A B)
           (cases (= R 0) (+ Q Q)
                  (or (< Q 2) (= R 1)) (* Q R)
                  true (- Q R))))

(define k.flag
  [Mode Owner Flag Slots] Flag -> [[Mode Owner Flag Slots] [[same]]]
  [Mode Owner _ Slots] B -> [[Mode Owner B Slots] []])
