(define vm.word
  N -> (shen.mod N 4294967296))

(define vm.execute
  [] [Answer] _ -> Answer
  [[0] | Code] Stack Item -> (vm.execute Code [Item | Stack] Item)
  [[1 N] | Code] Stack Item -> (vm.execute Code [N | Stack] Item)
  [[2] | Code] [B A | Stack] Item -> (vm.execute Code [(vm.word (+ A B)) | Stack] Item)
  [[3] | Code] [B A | Stack] Item -> (vm.execute Code [(vm.word (* A B)) | Stack] Item))

(define vm.fold
  Code Acc [] -> Acc
  Code Acc [X | Rest] -> (vm.fold Code (vm.execute Code [Acc] X) Rest))
