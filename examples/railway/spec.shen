; Event codes are route*4 + kind, where kind is request=0, cancel=1,
; entry=2, clear=3. Slots 0/1 hold route and occupancy, slot 2 is the
; dynamic cursor. The residual loop emits a verdict for every event.
(define specialize
  Layout -> [seq
    [set 0 0] [set 1 0] [set 2 0]
    [while [lt [var 2] [len]]
      [seq
        [set 3 [mod [at [var 2]] 4]]
        [set 4 [add [div [at [var 2]] 4] 1]]
        [set 5 0]
        [if [eq [var 3] 0]
            [if [eq [var 0] 0] [seq [set 0 [var 4]] [set 5 1]] [seq]]
            [if [eq [var 3] 1]
                [if [and [eq [var 0] [var 4]] [eq [var 1] 0]]
                    [seq [set 0 0] [set 1 0] [set 5 2]] [seq]]
                [if [eq [var 3] 2]
                    [if [and [eq [var 0] [var 4]] [eq [var 1] 0]]
                        [seq [set 1 1] [set 5 3]] [seq]]
                    [if [and [eq [var 0] [var 4]] [eq [var 1] 1]]
                        [seq [set 0 0] [set 1 0] [set 5 4]] [seq]]]]]
        [emit [var 5]]
        [set 2 [add [var 2] 1]]]])
