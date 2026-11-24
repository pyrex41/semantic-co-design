(define railEvent
  Route Position Base -> [if [eq [var 4] Base]
    [if [eq [var 0] 0] [seq [set 0 Route] [set 2 Position] [set 3 1]] [seq]]
    [if [eq [var 4] [add Base 1]]
      [if [and [eq [var 0] Route] [eq [var 1] 0]]
        [seq [set 0 0] [set 3 2]]
        [seq]]
      [if [eq [var 4] [add Base 2]]
        [if [and [eq [var 0] Route] [eq [var 1] 0]]
          [seq [set 1 Route] [set 3 3]]
          [seq]]
        [if [eq [var 4] [add Base 3]]
          [if [eq [var 1] Route] [seq [set 1 0] [set 0 0] [set 3 4]] [seq]]
          [seq]]]]])
(define specialize
  [First Second] -> [seq [while [lt [var 5] [len]]
      [seq [set 4 [at [var 5]]]
        [set 3 0]
        (railEvent 1 First 0)
        (railEvent 2 Second 4)
        [emit [var 3]]
        [emit [var 0]]
        [emit [var 1]]
        [emit [var 2]]
        [set 5 [add [var 5] 1]]]]])
