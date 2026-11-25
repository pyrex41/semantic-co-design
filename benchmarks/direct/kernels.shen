(define bench.checksum
  [] Acc -> [Acc]
  [X | Rest] Acc -> (bench.checksum Rest (shen.mod (+ (* 31 Acc) X) 4294967296)))

(define bench.rail
  [] _ _ _ -> []
  [E | Rest] R T S ->
    (let Route (if (< E 4) 1 2)
         Kind (shen.mod E 4)
         Next (cond ((and (= Kind 0) (= R 0)) [1 Route T Route])
                    ((and (= Kind 1) (and (= R Route) (= T 0))) [2 0 T S])
                    ((and (= Kind 2) (and (= R Route) (= T 0))) [3 R Route S])
                    ((and (= Kind 3) (= T Route)) [4 0 0 S])
                    (true [0 R T S]))
         (append Next (bench.rail Rest (hd (tl Next)) (hd (tl (tl Next))) (hd (tl (tl (tl Next))))))))

(define bench.drop
  0 X -> X
  N [_ | X] -> (bench.drop (- N 1) X))

(define bench.match
  0 _ _ _ -> []
  N [K V | T] A B -> (let Tail (bench.match (- N 1) T A B)
                            (if (= A K) [A B K V | Tail] Tail)))

(define bench.join
  0 _ _ _ -> []
  N [A B | L] M R -> (append (bench.match M R A B) (bench.join (- N 1) L M R)))

(define bench.decode
  [] _ _ _ _ _ _ _ _ _ -> []
  [N | R] 0 Pos Err Phase Kind Len Left Sum Buf ->
    (if (= N 0)
      (append [-2 Pos Err Phase (if (= Phase 4) Left 0)] (bench.decode R 0 Pos Err Phase Kind Len Left Sum Buf))
      (bench.decode R N Pos Err Phase Kind Len Left Sum Buf))
  [B | R] N Pos Err P K L Left Sum Buf ->
    (let Fail (or (and (= P 0) (not (= B 165)))
                 (or (and (= P 1) (not (element? B [1 2])))
                   (or (and (= P 2) (> B 0)) (or (and (= P 3) (> B 32))
                     (and (= P 5) (not (= B (shen.mod Sum 256))))))))
         NewErr (if (and (= Err -1) Fail) Pos Err)
         Good (and (= Err -1) (not Fail))
         NP (if Good (cond ((= P 3) (if (= B 0) 5 4)) ((= P 4) (if (= Left 1) 5 4))
                           ((= P 5) 0) (true (+ P 1))) P)
         NL (if (and Good (= P 3)) B L)
         Rem (if Good (cond ((= P 3) B) ((= P 4) (- Left 1)) (true Left)) Left)
         NK (if (and Good (= P 1)) B K)
         NS (if (= P 1) (+ 7 B) (+ Sum B))
         NB (cond ((= P 3) []) ((= P 4) [B | Buf]) (true Buf))
         End (if (= Err -1) (+ Pos 1) Pos)
         Frame (if (and Good (= P 5)) [-1 K L | (reverse Buf)] [])
         Mark (if (= N 1) [-2 End NewErr NP (if (= NP 4) Rem 0)] [])
         (append Frame (append Mark (bench.decode R (- N 1) End NewErr NP NK NL Rem NS NB)))))

(define bench.run
  vm Row -> (bench.checksum Row 17)
  railway Row -> (bench.rail Row 0 0 0)
  query [_ N M | Data] -> (bench.join N Data M (bench.drop (* 2 N) Data))
  decoder Row -> (bench.decode Row 0 0 -1 0 0 0 0 0 []))

(define bench.rows
  _ [] -> []
  Name [R | Tail] -> [(bench.run Name R) | (bench.rows Name Tail)])

(define bench.digits
  B Acc Stream -> (if (and (>= B 48) (<= B 57))
    (bench.digits (read-byte Stream) (+ (* Acc 10) (- B 48)) Stream) Acc))

(define bench.number
  Stream -> (let B (read-byte Stream)
    (cond ((< B 0) (simple-error "unexpected end of corpus"))
          ((<= B 32) (bench.number Stream))
          (true (bench.digits B 0 Stream)))))

(define bench.read-row
  0 _ -> []
  N Stream -> [(bench.number Stream) | (bench.read-row (- N 1) Stream)])

(define bench.read-rows
  0 _ -> []
  N Stream -> (let Len (bench.number Stream)
                   Capacity (bench.number Stream)
                   Row (bench.read-row Len Stream)
                   [Row | (bench.read-rows (- N 1) Stream)]))

(define bench.domain
  0 -> vm
  1 -> decoder
  2 -> railway
  3 -> query)

(define bench.print-rows
  [] -> true
  [Row | Tail] -> (do (output "~A~%" Row) (bench.print-rows Tail)))

(define bench.main
  -> (do (set *maximum-print-sequence-size* 100000)
     (let Stream (stinput)
          Name (bench.domain (bench.number Stream))
          Rows (bench.read-rows (bench.number Stream) Stream)
          Warm (bench.run Name (hd Rows))
          Start (get-time run)
          Answers (bench.rows Name Rows)
          Elapsed (- (get-time run) Start)
          (do (output "~A~%" Elapsed) (bench.print-rows Answers)))))
