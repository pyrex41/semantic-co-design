\\ An ordinary stack machine with locals, a conditional jump and unrestricted jump targets.
\\ Nothing here mentions staging, residual code or the specializer.

(define vm.nth
  0 [X | _] -> X
  N [_ | Xs] -> (vm.nth (- N 1) Xs))

(define vm.put
  0 V [_ | Xs] -> [V | Xs]
  N V [X | Xs] -> [X | (vm.put (- N 1) V Xs)])

(define vm.run
  Code Pc Stack Locals -> (vm.step (vm.nth Pc Code) Code Pc Stack Locals))

(define vm.step
  [halt] _ _ [Answer | _] _ -> Answer
  [push N] Code Pc Stack Locals -> (vm.run Code (+ Pc 1) [N | Stack] Locals)
  [load I] Code Pc Stack Locals -> (vm.run Code (+ Pc 1) [(vm.nth I Locals) | Stack] Locals)
  [store I] Code Pc [V | Stack] Locals -> (vm.run Code (+ Pc 1) Stack (vm.put I V Locals))
  [add] Code Pc [B A | Stack] Locals -> (vm.run Code (+ Pc 1) [(+ A B) | Stack] Locals)
  [sub] Code Pc [B A | Stack] Locals -> (vm.run Code (+ Pc 1) [(- A B) | Stack] Locals)
  [mul] Code Pc [B A | Stack] Locals -> (vm.run Code (+ Pc 1) [(* A B) | Stack] Locals)
  [div] Code Pc [B A | Stack] Locals -> (vm.run Code (+ Pc 1) [(/ (- A (shen.mod A B)) B) | Stack] Locals)
  [mod] Code Pc [B A | Stack] Locals -> (vm.run Code (+ Pc 1) [(shen.mod A B) | Stack] Locals)
  [lt] Code Pc [B A | Stack] Locals -> (vm.run Code (+ Pc 1) [(if (< A B) 1 0) | Stack] Locals)
  [eq] Code Pc [B A | Stack] Locals -> (vm.run Code (+ Pc 1) [(if (= A B) 1 0) | Stack] Locals)
  [jmp T] Code _ Stack Locals -> (vm.run Code T Stack Locals)
  [jz T] Code Pc [V | Stack] Locals -> (if (= V 0)
                                           (vm.run Code T Stack Locals)
                                           (vm.run Code (+ Pc 1) Stack Locals)))
