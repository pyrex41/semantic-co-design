\\ Test-only accounting laws over the persisted record contract. No transition is imported.
(define law.assets
  -> ["BCH" "BTC" "USD"])

(define law.zero
  -> (obj [["BCH" 0] ["BTC" 0] ["USD" 0]]))

(define law.limit
  "USD" -> 5000000000000000000
  "BTC" -> 10000000000
  "BCH" -> 10000000000
  _ -> 0)

(define law.base
  "BTC-USD" -> "BTC"
  "BCH-USD" -> "BCH"
  _ -> null)

(define law.bound
  X High -> (and (int? X) (and (>= X 0) (<= X High))))

(define law.quote
  Price Size ->
    (if (and (law.bound Price 1000000000) (law.bound Size 10000000000))
      (if (= Size 0) 0
        (if (<= Price (floor-div 5000000000000 Size)) (* (* Price Size) 1000000) null))
      null))

(define law.balance-fields
  Balances Reserved Assets Offset ->
    (if (= Offset (len Assets)) true
      (let Asset (at Assets Offset)
        (let Balance (get Balances Asset)
          (and (law.bound Balance (law.limit Asset))
            (and (law.bound (get Reserved Asset) Balance)
              (law.balance-fields Balances Reserved Assets (+ Offset 1))))))))

(define law.asset-envelope
  Balances Reserved ->
    (and (obj? Balances) (and (obj? Reserved)
      (and (= (keys Balances) (law.assets)) (and (= (keys Reserved) (law.assets))
        (law.balance-fields Balances Reserved (law.assets) 0))))))

(define law.terminal
  Phase -> (contains ["fully-filled" "cancelled" "rejected"] Phase))

(define law.order-reserve
  Order Quote ->
    (if (law.terminal (get Order "phase")) 0
      (if (= "sell" (get Order "side")) (get Order "remaining")
        (+ Quote (- (get Order "fee-budget") (get Order "fee-paid"))))))

(define law.order-valid
  Order Id ->
    (let Phase (get Order "phase")
      (let Side (get Order "side")
        (let Amount (get Order "size")
          (let Remaining (get Order "remaining")
            (let Fee (get Order "fee-budget")
              (let Full (law.quote (get Order "price") Amount)
                (and (= Id (get Order "client-id"))
                  (and (not (= null (law.base (get Order "product"))))
                    (and (contains ["buy" "sell"] Side)
                      (and (contains ["pending-send" "sent-unknown" "acknowledged" "partially-filled"
                                     "cancel-pending" "reconciliation-required" "fully-filled" "cancelled" "rejected"] Phase)
                        (and (not (= Full null))
                          (and (> Full 0) (and (law.bound Remaining Amount)
                            (and (= (= Remaining 0) (= Phase "fully-filled"))
                              (and (law.bound Fee 1250000000000000000)
                                (law.bound (get Order "fee-paid") Fee)))))))))))))))))

(define law.fold-orders
  Orders Ids Position Balances Reserved Index ->
    (if (= Position (len Ids)) [Reserved Index]
      (let Id (at Ids Position)
        (let Order (get Orders Id)
          (if (not (law.order-valid Order Id)) null
            (let Asset (if (= "buy" (get Order "side")) "USD" (law.base (get Order "product")))
              (let Venue (get Order "venue-id")
                (let Reserve (law.order-reserve Order (law.quote (get Order "price") (get Order "remaining")))
                  (let Held (get Reserved Asset)
                    (if (or (not (= Asset (get Order "reserve-asset")))
                          (or (not (= Reserve (get Order "reservation")))
                            (or (> Reserve (- (get Balances Asset) Held))
                              (and (not (= Venue null))
                                (or (not (text? Venue 1 72)) (not (= null (get Index Venue)))))))) null
                      (law.fold-orders Orders Ids (+ Position 1) Balances (put Reserved Asset (+ Held Reserve))
                        (if (= Venue null) Index (put Index Venue Id)))))))))))))

(define law.component
  Kind Identity -> (concat [Kind ":" Identity]))

(define law.fill-valid
  Report Order Key Quote ->
    (let Price (get Report "price")
      (let Side (get Report "side")
        (let Product (get Report "product")
          (let Trade (get Report "trade-id")
            (and (= Key (get Report "identity"))
              (and (= Product (get Order "product"))
                (and (= Side (get Order "side"))
                  (and (and (law.bound Trade 9000000000000000000) (> Trade 0))
                    (and (= Key (concat [Product ":" (get Report "venue-id") ":" (str Trade)]))
                      (and (and (int? Quote) (> Quote 0))
                        (and (law.bound (get Report "fee") 500000000000000000)
                          (if (= "buy" Side) (<= Price (get Order "price")) (>= Price (get Order "price")))))))))))))))

(define law.fill-postings
  Expected Report Key Quote ->
    (let Buy (= "buy" (get Report "side"))
      (let Base (law.base (get Report "product"))
        (let Quantity (get Report "size")
          (let BaseKey (law.component "fill-base" Key)
            (let QuoteKey (law.component "fill-quote" Key)
              (let BaseRows (put Expected BaseKey [Base "venue-clearing" (if Buy Quantity (- 0 Quantity))])
                (let Pairs (put BaseRows QuoteKey ["USD" "venue-clearing" (if Buy (- 0 Quote) Quote)])
                  (let Fee (get Report "fee")
                    (if (= Fee 0) Pairs (put Pairs (law.component "fee" Key) ["USD" "venue-fees" (- 0 Fee)])))))))))))

(define law.fold-fills
  State Reports Ids Position Totals Expected ->
    (if (= Position (len Ids)) [Totals Expected]
      (let Key (at Ids Position)
        (let Report (get Reports Key)
          (let Venue (get Report "venue-id")
            (let Client (if (str? Venue) (get (get State "venue-orders") Venue) null)
              (let Order (get (get State "orders") Client)
                (let Quote (law.quote (get Report "price") (get Report "size"))
                  (if (not (and (obj? Order) (law.fill-valid Report Order Key Quote))) null
                    (let Previous (get Totals Client)
                      (let Quantity (+ (get Report "size") (if (= Previous null) 0 (at Previous 0)))
                        (let Fee (+ (get Report "fee") (if (= Previous null) 0 (at Previous 1)))
                          (if (or (> Quantity (get Order "size")) (> Fee (get Order "fee-budget"))) null
                            (law.fold-fills State Reports Ids (+ Position 1) (put Totals Client [Quantity Fee])
                              (law.fill-postings Expected Report Key Quote)))))))))))))))

(define law.order-fill-totals
  Orders Ids Position Totals ->
    (if (= Position (len Ids)) true
      (let Id (at Ids Position)
        (let Order (get Orders Id)
          (let TotalsForOrder (get Totals Id)
            (let Filled (- (get Order "size") (get Order "remaining"))
              (and (= [Filled (get Order "fee-paid")] (if (= TotalsForOrder null) [0 0] TotalsForOrder))
                (law.order-fill-totals Orders Ids (+ Position 1) Totals))))))))

\\ A paired transaction is a formal debit/credit in one asset. Its internal
\\ asset posting is the sole input for recomputing the materialized balance.
(define law.pair-value
  AssetRow ContraRow ->
    (if (and (and (list? AssetRow) (= 3 (len AssetRow)))
          (and (list? ContraRow) (= 3 (len ContraRow))))
      (let Asset (at AssetRow 0)
        (let Value (at AssetRow 2)
          (let Contra (at ContraRow 1)
            (if (and (> (law.limit Asset) 0)
                  (and (= (at AssetRow 1) "asset") (and (not (= Contra "asset"))
                    (and (= Asset (at ContraRow 0))
                      (and (int? Value) (and (>= Value -5000000000000000000)
                        (and (<= Value 5000000000000000000) (= (at ContraRow 2) (- 0 Value)))))))))
              [Asset Contra Value] null)))) null))

(define law.pair
  Rows ->
    (if (and (list? Rows) (= (len Rows) 2))
      (if (= "asset" (at (at Rows 0) 1)) (law.pair-value (at Rows 0) (at Rows 1))
        (law.pair-value (at Rows 1) (at Rows 0))) null))

(define law.adjustment-proof
  Identity ->
    (and (text? (get Identity "actor") 1 100) (and (text? (get Identity "approver") 1 100)
      (and (not (= (get Identity "actor") (get Identity "approver"))) (text? (get Identity "reason") 8 400)))))

(define law.journal-row
  Kind Identity Value Opened Expected Seen ->
    (let Asset (at Value 0)
      (if (= Kind "opening")
        (if (and (= Identity "statement") (and (= (at Value 1) "opening")
              (and (= (get Opened Asset) null) (law.bound (at Value 2) (law.limit Asset)))))
          [(put Opened Asset true) Seen] null)
        (if (not (get Opened Asset)) null
          (if (= Kind "adjustment")
            (if (and (= (at Value 1) "adjustment") (law.adjustment-proof Identity)) [Opened Seen] null)
            (let Key (law.component Kind Identity)
              (if (and (= (get Seen Key) null) (= (get Expected Key) Value))
                [Opened (put Seen Key true)] null)))))))

(define law.fold-journal
  Journal Position Balances Opened Expected Seen ->
    (if (< Position 0)
      (if (= (keys Seen) (keys Expected)) [Balances (keys Opened)] null)
      (let Row (at Journal Position)
        (let Kind (get Row "kind")
          (let Rows (get Row "postings")
            (if (= Kind "release-activation")
              (if (= Rows []) (law.fold-journal Journal (- Position 1) Balances Opened Expected Seen) null)
              (let Value (law.pair Rows)
                (if (= Value null) null
                  (let Valid (law.journal-row Kind (get Row "identity") Value Opened Expected Seen)
                    (if (= Valid null) null
                      (let Asset (at Value 0)
                        (let Total (+ (get Balances Asset) (at Value 2))
                          (if (< Total 0) null
                            (law.fold-journal Journal (- Position 1) (put Balances Asset Total)
                              (at Valid 0) Expected (at Valid 1)))))))))))))))

(define law.reconstruct
  State Expected ->
    (let Journal (get State "journal")
      (let Result (law.fold-journal Journal (- (len Journal) 1) (law.zero) (obj []) Expected (obj []))
        (and (not (= Result null))
          (and (= (get State "balances") (at Result 0))
            (= (if (get State "started") (law.assets) []) (at Result 1)))))))

(define law.ledger
  State ->
    (let Orders (get State "orders")
      (let Balances (get State "balances")
        (let Held (get State "reserved")
          (if (not (law.asset-envelope Balances Held)) "asset-envelope"
            (let Ids (keys Orders)
              (let Removed (law.fold-orders Orders Ids 0 Balances (law.zero) (obj []))
                (if (= Removed null) "order-reservation"
                  (if (not (= (at Removed 0) Held)) "reserved-sum"
                    (if (not (= (at Removed 1) (get State "venue-orders"))) "venue-index"
                      (let Fills (get State "executions")
                        (let Projection (law.fold-fills State Fills (keys Fills) 0 (obj []) (obj []))
                          (if (or (= Projection null) (not (law.order-fill-totals Orders Ids 0 (at Projection 0))))
                            "execution-lineage"
                            (if (law.reconstruct State (at Projection 1)) "holds" "journal-reconstruction"))))))))))))))

(define law.same-finance
  Before After ->
    (= [(get Before "balances") (get Before "reserved") (get Before "executions") (get Before "journal")]
       [(get After "balances") (get After "reserved") (get After "executions") (get After "journal")]))

(define law.transition
  Entry Status Before After ->
    (if (or (= Entry "reconcile")
          (and (= Entry "fill")
            (not (contains ["partially-filled" "fully-filled" "cancel-pending"] Status))))
      (law.same-finance Before After) true))

(define law.check
  Observation ->
    (let After (get Observation "after")
      (let Ledger (law.ledger After)
        (if (not (= Ledger "holds")) Ledger
          (if (law.transition (get Observation "entry") (get Observation "status") (get Observation "before") After)
            "holds" "nonposting-transition")))))
