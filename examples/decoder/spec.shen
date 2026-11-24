(define accepted
  [] -> 0
  [Kind | More] -> [or [eq [var 9] Kind] (accepted More)])
(define takeKind
  Kinds Seed -> [if (accepted Kinds)
    [seq [set 5 [var 9]] [set 7 [add Seed [var 9]]] [set 2 2]]
    [set 4 [var 3]]])
(define takeLength
  Weight Max Next -> [seq [set 6 [add [var 6] [mul [var 9] Weight]]]
    [if [lt Max [var 6]]
      [set 4 [var 3]]
      [seq [set 7 [add [var 7] [var 9]]] [set 2 Next]]]])
(define finishFrame
  Byte -> [if [eq [mod [var 7] 256] Byte]
    [seq [emit -1]
      [emit [var 5]]
      [emit [var 6]]
      [set 10 0]
      [while [lt [var 10] [var 6]]
        [seq [emit [get 0 [var 10]]] [set 10 [add [var 10] 1]]]]
      [set 2 0]
      [set 8 0]]
    [set 4 [var 3]]])
(define takeByte
  Magic Kinds First Second Max Seed -> [if [eq [var 2] 0]
    [if [eq [var 9] Magic] [seq [set 6 0] [set 2 1]] [set 4 [var 3]]]
    [if [eq [var 2] 1]
      (takeKind Kinds Seed)
      [if [eq [var 2] 2]
        (takeLength First Max 3)
        [if [eq [var 2] 3]
          [seq (takeLength Second Max 4)
            [if [and [eq [var 4] -1] [eq [var 6] 0]] [set 2 5] [seq]]]
          [if [eq [var 2] 4]
            [seq [put 0 [var 8] [var 9]]
              [set 8 [add [var 8] 1]]
              [set 7 [add [var 7] [var 9]]]
              [if [eq [var 8] [var 6]] [set 2 5] [seq]]]
            (finishFrame [var 9])]]]]])
(define decodeChunks
  ByteRule -> [seq [set 4 -1]
    [while [lt [var 0] [len]]
      [seq [set 1 [add [add [var 0] 1] [at [var 0]]]]
        [set 0 [add [var 0] 1]]
        [while [lt [var 0] [var 1]]
          [seq [if [eq [var 4] -1]
              [seq [set 9 [at [var 0]]] ByteRule [set 3 [add [var 3] 1]]]
              [seq]]
            [set 0 [add [var 0] 1]]]]
        [emit -2]
        [emit [var 3]]
        [emit [var 4]]
        [emit [var 2]]
        [if [eq [var 2] 4] [emit [sub [var 6] [var 8]]] [emit 0]]]]])
(define specialize
  [Magic Kinds 0 Max Seed] -> (decodeChunks (takeByte Magic Kinds 256 1 Max Seed))
  [Magic Kinds 1 Max Seed] -> (decodeChunks (takeByte Magic Kinds 1 256 Max Seed)))
