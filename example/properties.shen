(define observe-votes P ->
  [(vote-status "approve" (at P 0) (at P 1))
   (vote-status "approve" (at P 0) (+ (at P 1) 1))
   (vote-status "reject" (at P 0) (at P 1))])

(define vote-law P ->
  (let Seen (at P 1)
    (and (= (at Seen 2) "rejected")
      (and (= (= (at Seen 0) "approved") (>= (at (at P 0) 0) (at (at P 0) 1)))
        (and (or (= (at Seen 0) "approved") (= (at Seen 0) "pending"))
          (or (= (at Seen 1) "pending")
            (and (= (at Seen 1) "approved") (= (at Seen 0) "approved"))))))))

(define observe-rounding P ->
  [(tx.floor-step (at P 0) (at P 1)) (tx.ceil-step (at P 0) (at P 1))])

(define rounding-law P ->
  (let Price (at (at P 0) 0)
    (let Tick (at (at P 0) 1)
      (let Low (at (at P 1) 0)
        (let High (at (at P 1) 1)
          (and (and (<= Low Price) (<= Price High))
            (and (and (< (- Price Low) Tick) (< (- High Price) Tick))
              (and (= (floor-mod Low Tick) 0) (= (floor-mod High Tick) 0)))))))))

(define observe-recovery P ->
  (let Once (rt.restore-reservations P 0 [])
    [Once (rt.restore-reservations Once 0 [])]))

(define recovered-rows-law Old New I ->
  (if (= I (len Old)) true
    (let Before (at Old I)
      (let After (at New I)
        (let Until (get After "until")
          (and (= After (merge Before (obj [["until" Until]])))
            (and (>= Until (get Before "until"))
              (and (>= Until (rt.get-or Before "reserved-until" (get Before "until")))
                (and (or (= Until (get Before "until")) (= Until (get Before "reserved-until")))
                  (recovered-rows-law Old New (+ I 1)))))))))))

(define recovery-law P ->
  (let Before (at P 0)
    (let Once (at (at P 1) 0)
      (and (= Once (at (at P 1) 1))
        (and (= (len Before) (len Once)) (recovered-rows-law Before Once 0))))))
