(define vm.stack
  [] [Answer] -> Answer
  [[0] | Rest] Stack -> (vm.stack Rest [[u32 [at [var 1]]] | Stack])
  [[1 N] | Rest] Stack -> (vm.stack Rest [[u32 N] | Stack])
  [[2] | Rest] [B A | Stack] -> (vm.stack Rest [[u32 [add A B]] | Stack])
  [[3] | Rest] [B A | Stack] -> (vm.stack Rest [(vm.product A B) | Stack])
  [[4] | Rest] [B A | Stack] -> (vm.stack Rest [[add A [mul [lt A B] [sub B A]]] | Stack]))
(define vm.product
  A B -> [u32 [add [mul [mod A 65536] B]
                   [mul 65536 [mul [div A 65536] [mod B 65536]]]]])
(define vm.loop
  Seed Start Body -> [seq [set 0 [u32 Seed]] [set 1 Start]
                      [while [lt [var 1] [len]]
                        [seq [set 0 Body] [set 1 [add [var 1] 1]]]]
                      [emit [var 0]]])
(define specialize
  [Seed 0 Ops] -> (vm.loop Seed 0 (vm.stack Ops [[var 0]]))
  [_ 1 Ops] -> [if [lt 0 [len]] (vm.loop [at 0] 1 (vm.stack Ops [[var 0]])) [seq]])
