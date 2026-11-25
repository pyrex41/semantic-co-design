\\ Every node keeps the largest number it has heard and passes it round a ring.
\\ State [Id Nodes Value Rounds]. Memory is volatile: a restart forgets the value.

(define ring.init
  Id Nodes -> [Id Nodes 0 0])

(define ring.step
  [Id Nodes _ _] [restart] -> [[Id Nodes 0 2] [[arm 0 10]]]
  State [set New] -> (ring.learn State New)
  State [heard _ New] -> (ring.learn State New)
  State [alarm 0] -> (ring.repeat State)
  State _ -> [State []])

(define ring.learn
  [Id Nodes Value Rounds] New ->
    [[Id Nodes New Rounds] [[tell (ring.next Id Nodes) New]]] where (> New Value)
  State _ -> [State []])

\\ Anti-entropy: say the current value again, a bounded number of times.
(define ring.repeat
  [Id Nodes Value 0] -> [[Id Nodes Value 0] []]
  [Id Nodes Value Rounds] ->
    [[Id Nodes Value (- Rounds 1)] [[tell (ring.next Id Nodes) Value] [arm 0 10]]])

(define ring.next
  Id Nodes -> (if (= (+ Id 1) Nodes) 0 (+ Id 1)))
