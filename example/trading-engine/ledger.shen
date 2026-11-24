(define tx.postings
  Asset Account Contra Amount -> [[Asset Account Amount] [Asset Contra (- 0 Amount)]])

(define tx.postable
  State Asset Delta ->
    (let Balance (tx.balance State Asset)
      (if (< Delta 0) (>= Balance (- 0 Delta)) (<= Delta (- (tx.amount-limit Asset) Balance)))))

(define tx.change-balance
  State Asset Delta ->
    (tx.set State "balances" (tx.set (get State "balances") Asset (+ (tx.balance State Asset) Delta))))

(define tx.fill-side-ok
  "buy" Price Limit -> (<= Price Limit)
  "sell" Price Limit -> (>= Price Limit))

(define tx.fill-key
  Data -> (concat [(get Data "product_id") ":" (get Data "order_id") ":" (str (get Data "trade_id"))]))

(define tx.filled-at
  State Order Fill ->
    (let Product (get Order "product")
      (let Side (get Order "side")
        (let Base (tx.base Product)
          (let Size (get Fill "size")
            (let Fee (get Fill "fee")
              (let Quote (tx.notional (get Fill "price") Size)
                (let Paid (+ (get Order "fee-paid") Fee)
                  (if (or (< Quote 0)
                        (or (> Paid (get Order "fee-budget"))
                          (or (> Size (get Order "remaining"))
                            (not (tx.fill-side-ok Side (get Fill "price") (get Order "price"))))))
                    (tx.restrict State "fill-outside-order" Fill)
                    (tx.commit-fill State Order Fill Base Side Size Fee Quote Paid))))))))))

(define tx.commit-fill
  State Order Fill Base Side Size Fee Quote Paid ->
    (let QuoteDelta (if (= Side "buy") (- 0 Quote) Quote)
      (let BaseDelta (if (= Side "buy") Size (- 0 Size))
        (let Net (- QuoteDelta Fee)
          (if (not (and (tx.postable State "USD" Net) (tx.postable State Base BaseDelta)))
            (tx.restrict State "fill-balance-mismatch" Fill)
            (let Remaining (- (get Order "remaining") Size)
              (let Reservation (tx.remaining-reserve Order Remaining Paid)
                (let New (merge Order (obj [["remaining" Remaining] ["fee-paid" Paid]
                                             ["phase" (tx.order-phase-with-fill Order Remaining)]]))
                  (let Updated (tx.replace-order
                                (tx.change-balance (tx.change-balance State "USD" Net) Base BaseDelta)
                                Order New Reservation)
                    (if (or (< (tx.available Updated Base) 0) (< (tx.available Updated "USD") 0))
                      (tx.restrict State "post-fill-reservations-exceed-balance" Fill)
                      (tx.record-fill Updated New Fill Base BaseDelta QuoteDelta Fee Reservation)))))))))))

(define tx.record-fill
  State Order Fill Base BaseDelta QuoteDelta Fee Reservation ->
    (let Identity (get Fill "identity")
      (let Stored (tx.set State "executions" (tx.set (get State "executions") Identity Fill))
        (let Journal (tx.journal (tx.journal Stored "fill-base" Identity
                        (tx.postings Base "asset" "venue-clearing" BaseDelta))
                        "fill-quote" Identity (tx.postings "USD" "asset" "venue-clearing" QuoteDelta))
          (tx.result (if (= Fee 0) Journal
                        (tx.journal Journal "fee" Identity (tx.postings "USD" "asset" "venue-fees" (- 0 Fee))))
            (tx.phase Order) (tx.set Order "reservation" Reservation))))))

(define tx.fill-record
  Data ->
    (let VenueId (get Data "order_id")
      (let Product (get Data "product_id")
        (let TradeId (get Data "trade_id")
          (let Side (get Data "side")
            (let Price (tx.decimal (get Data "price") 2)
              (let Size (tx.decimal (get Data "size") 8)
                (let Fee (tx.decimal (get Data "fee") 16)
                  (if (and (not (= (tx.base Product) null))
                        (and (text? VenueId 1 72) (and (tx.positive TradeId 9000000000000000000)
                          (and (or (= Side "buy") (= Side "sell"))
                            (and (tx.positive Price 1000000000)
                              (and (tx.positive Size 10000000000) (tx.bounded Fee 500000000000000000)))))))
                    (obj [["identity" (tx.fill-key Data)] ["venue-id" VenueId] ["product" Product]
                          ["trade-id" TradeId] ["side" Side] ["price" Price] ["size" Size] ["fee" Fee]]) null)))))))))

(define tx.fill
  State Data ->
    (if (not (= true (get Data "settled")))
      (if (= false (get Data "settled")) (tx.result State "unsettled-fill" null)
        (tx.restrict State "invalid-fill-settlement" null))
      (let Fill (tx.fill-record Data)
        (if (= Fill null) (tx.restrict State "invalid-fill" null)
          (tx.accept-fill State Fill)))))

(define tx.accept-fill
  State Fill ->
    (let Identity (get Fill "identity")
      (let Existing (get (get State "executions") Identity)
        (if (not (= Existing null))
          (if (= Existing Fill) (tx.result State "duplicate-fill" Existing)
            (tx.restrict State "contradictory-fill" Fill))
          (let ClientId (get (get State "venue-orders") (get Fill "venue-id"))
            (let Order (if (= ClientId null) null (tx.order State ClientId))
              (if (or (= Order null) (tx.terminal (tx.phase Order)))
                (tx.restrict State "unmatched-fill" Fill)
                (if (or (not (= (get Order "side") (get Fill "side")))
                        (not (= (get Order "product") (get Fill "product"))))
                  (tx.restrict State "mismatched-fill" Fill)
                  (if (>= (len (get State "executions")) 4096)
                    (tx.restrict State "execution-history-full" Fill)
                    (tx.filled-at State Order Fill))))))))))

(define tx.statement-balances
  Rows Offset Balances ->
    (if (= Offset (len Rows)) Balances
      (let Row (at Rows Offset)
        (let Currency (get Row "currency")
          (if (= (tx.amount-limit Currency) 0) (tx.statement-balances Rows (+ Offset 1) Balances)
            (if (not (= null (get Balances Currency))) null
              (let Amount (tx.decimal (get Row "balance") (if (= Currency "USD") 16 8))
                (if (not (tx.bounded Amount (tx.amount-limit Currency))) null
                  (tx.statement-balances Rows (+ Offset 1) (tx.set Balances Currency Amount))))))))))

(define tx.statement
  Rows ->
    (if (and (list? Rows) (<= (len Rows) 128))
      (let Balances (tx.statement-balances Rows 0 (obj []))
        (if (tx.balance-valid Balances) Balances null)) null))

(define tx.balance-opening
  State Balances ->
    (let USD (tx.journal State "opening" "statement" (tx.postings "USD" "asset" "opening" (get Balances "USD")))
      (let BTC (tx.journal USD "opening" "statement" (tx.postings "BTC" "asset" "opening" (get Balances "BTC")))
        (tx.journal BTC "opening" "statement" (tx.postings "BCH" "asset" "opening" (get Balances "BCH"))))))
