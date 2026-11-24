(define emitRow
  0 _ -> [seq]
  N P -> [seq (emitRow (- N 1) P) [emit [at [add P (- N 1)]]]])
(define fieldAt
  Base Col -> [at [add Base Col]])
(define eachRow
  Slot Count Width Start Body -> [seq [set Slot Start]
    [while [lt [var Slot] [add Start [mul Count Width]]]
      [seq Body [set Slot [add [var Slot] Width]]]]])
(define relationWidth
  [0 0] W _ -> W
  [0 1] _ V -> V
  [1 _ Child] W V -> (relationWidth Child W V))
(define scanRows
  [0 0] W V Slot Body -> (eachRow Slot [at 1] W 3 Body)
  [0 1] W V Slot Body -> (eachRow Slot [at 2] V [add 3 [mul [at 1] W]] Body)
  [1 Col Child] W V Slot Body ->
    (scanRows Child W V Slot [if [le [at 0] (fieldAt [var Slot] Col)] Body [seq]]))
(define joinRows
  W V K J L R -> (scanRows L W V 0 (scanRows R W V 1 [if [eq (fieldAt [var 0] K) (fieldAt [var 1] J)]
        [seq (emitRow (relationWidth L W V) [var 0]) (emitRow (relationWidth R W V) [var 1])]
        [seq]])))
(define groupRows
  W V K Col Child -> (scanRows Child W V 0 [seq [set 3 0]
      [set 4 0]
      (scanRows Child W V 1 [if [eq (fieldAt [var 0] K) (fieldAt [var 1] K)]
          [seq [set 3 [add [var 3] (fieldAt [var 1] Col)]]
            [if [lt [var 1] [var 0]] [set 4 1] [seq]]]
          [seq]])
      [if [eq [var 4] 0] [seq [emit (fieldAt [var 0] K)] [emit [var 3]]] [seq]]]))
(define specialize
  [W V [2 K J L R]] -> (joinRows W V K J L R)
  [W V [3 K Col Child]] -> (groupRows W V K Col Child)
  [W V Relation] -> (scanRows Relation W V 0 (emitRow (relationWidth Relation W V) [var 0])))
