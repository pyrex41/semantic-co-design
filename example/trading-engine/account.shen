(define tx.initial
  -> (obj [["schema" 1] ["clock" 0] ["fence" 0] ["mode" "paused"] ["blocked" false]
           ["started" false] ["environment" "sandbox"] ["kill-latched" false] ["balances" (obj [["USD" 0] ["BTC" 0] ["BCH" 0]])]
           ["reserved" (obj [["USD" 0] ["BTC" 0] ["BCH" 0]])]
           ["orders" (obj [])] ["venue-orders" (obj [])] ["executions" (obj [])]
           ["metadata" (obj [])] ["books" (obj [])] ["strategies" (obj [])] ["adjustments" (obj [])] ["disabled" (obj [])]
           ["journal" []] ["exceptions" []] ["cursor" 0] ["next-private" 0]
           ["risk-ceiling" 5000000000000000000] ["fee-ppm" 10000] ["credential-epoch" 1]]))

(define tx.result
  State Status Data -> (obj [["state" State] ["status" Status] ["data" Data]]))

(define tx.note
  State Field Note -> (tx.set State Field (prepend Note (get State Field))))

(define tx.journal
  State Kind Identity Amounts ->
    (tx.note State "journal"
      (obj [["kind" Kind] ["identity" Identity] ["time" (get State "clock")]
            ["fence" (get State "fence")] ["postings" Amounts]])))

(define tx.restrict
  State Reason Details ->
    (tx.result (tx.note (merge State (obj [["mode" "reconciliation-required"] ["blocked" true]])) "exceptions"
      (obj [["reason" Reason] ["details" Details] ["time" (get State "clock")]])) Reason Details))

(define tx.balance
  State Asset -> (get (get State "balances") Asset))

(define tx.reserved
  State Asset -> (get (get State "reserved") Asset))

(define tx.available
  State Asset -> (- (tx.balance State Asset) (tx.reserved State Asset)))

(define tx.balance-view
  State Asset ->
    (obj [["total" (tx.balance State Asset)] ["reserved" (tx.reserved State Asset)]
          ["available" (tx.available State Asset)]]))

(define tx.amount-limit
  "USD" -> 5000000000000000000
  "BTC" -> 10000000000
  "BCH" -> 10000000000
  _ -> 0)

(define tx.balance-valid
  Balances ->
    (and (tx.bounded (get Balances "USD") (tx.amount-limit "USD"))
      (and (tx.bounded (get Balances "BTC") (tx.amount-limit "BTC"))
           (tx.bounded (get Balances "BCH") (tx.amount-limit "BCH")))))

(define tx.balance-equal
  Balances Expected ->
    (and (= (get Balances "USD") (get Expected "USD"))
      (and (= (get Balances "BTC") (get Expected "BTC")) (= (get Balances "BCH") (get Expected "BCH")))))

(define tx.order
  State Id -> (get (get State "orders") Id))

(define tx.put-order
  State Order -> (tx.set State "orders" (tx.set (get State "orders") (get Order "client-id") Order)))

(define tx.phase
  Order -> (get Order "phase"))

(define tx.terminal
  "fully-filled" -> true
  "cancelled" -> true
  "rejected" -> true
  _ -> false)

(define tx.open-identity
  Order -> (not (tx.terminal (tx.phase Order))))

(define tx.account-order-ids
  Orders Ids Offset Open ->
    (if (= Offset (len Ids)) Open
      (let Id (at Ids Offset)
        (let Order (get Orders Id)
          (tx.account-order-ids Orders Ids (+ Offset 1)
            (if (tx.open-identity Order) (prepend Id Open) Open))))))

(define tx.open-ids
  State -> (let Orders (get State "orders") (tx.account-order-ids Orders (keys Orders) 0 [])))

(define tx.reserve-asset
  "buy" _ -> "USD"
  "sell" Product -> (tx.base Product))

(define tx.order-reserve
  "buy" _ Quote Fee -> (+ Quote Fee)
  "sell" Size _ _ -> Size)

(define tx.reserve-delta
  State Asset Delta ->
    (tx.set State "reserved" (tx.set (get State "reserved") Asset (+ (tx.reserved State Asset) Delta))))

(define tx.replace-order
  State Order New Reservation ->
    (let Delta (- Reservation (get Order "reservation"))
      (tx.put-order (tx.reserve-delta State (get Order "reserve-asset") Delta)
        (tx.set New "reservation" Reservation))))

(define tx.valid-uuid-char
  8 Char -> (= Char "-")
  13 Char -> (= Char "-")
  14 Char -> (= Char "4")
  18 Char -> (= Char "-")
  19 Char -> (contains "89ab" Char)
  23 Char -> (= Char "-")
  _ Char -> (contains "0123456789abcdef" Char))

(define tx.uuid-digits
  Id Position ->
    (if (= Position 36) true
      (and (tx.valid-uuid-char Position (at Id Position)) (tx.uuid-digits Id (+ Position 1)))))

(define tx.uuid
  Id -> (and (text? Id 36 36) (tx.uuid-digits Id 0)))

(define tx.active-book
  State Product MaxAge ->
    (let Book (get (get State "books") Product)
      (if (and (get Book "valid") (<= (- (get State "clock") (get Book "time")) MaxAge))
        Book null)))

\\ Count reservations on pending cancels at full remaining value in this release.
(define tx.pending-cancel-weight
  -> 1)

(define tx.cancel-exposure
  Orders Ids Offset Total ->
    (if (= Offset (len Ids)) Total
      (let Order (get Orders (at Ids Offset))
        (tx.cancel-exposure Orders Ids (+ Offset 1)
          (if (and (= (tx.phase Order) "cancel-pending") (= (get Order "reserve-asset") "USD"))
            (+ Total (get Order "reservation")) Total)))))

(define tx.gross-exposure
  Orders Ids Offset Total ->
    (if (or (= Offset (len Ids)) (>= Total 5000000000000000000)) Total
      (let Order (get Orders (at Ids Offset))
        (let Reserve (get Order "reservation")
          (let Cash (if (= "USD" (get Order "reserve-asset")) Reserve
                        (if (= 0 Reserve) 0 (tx.notional (get Order "price") Reserve)))
            (tx.gross-exposure Orders Ids (+ Offset 1) (+ Total (tx.min Cash (- 5000000000000000000 Total)))))))))

(define tx.risk-available
  State ->
    (let Orders (get State "orders")
      (let Limit (- (get State "risk-ceiling") (tx.gross-exposure Orders (keys Orders) 0 0))
        (let Extra (* (- (tx.pending-cancel-weight) 1) (tx.cancel-exposure Orders (keys Orders) 0 0))
          (if (or (<= Limit 0) (>= Extra Limit)) 0 (- Limit Extra))))))

(define tx.admit-amounts
  State Req Metadata ->
    (let Price (get Req "price")
      (let Size (get Req "size")
        (let Amount (tx.notional Price Size)
          (if (< Amount 0) "invalid-or-overflowing-amount"
            (if (or (not (= 0 (shen.mod Price (get Metadata "price-increment"))))
                    (not (= 0 (shen.mod Size (get Metadata "quantity-increment"))))) "invalid-increment"
              (if (< Amount (get Metadata "minimum")) "below-venue-minimum"
                (let Fee (tx.fee-reserve Amount (get State "fee-ppm"))
                  (let Side (get Req "side")
                    (let Asset (tx.reserve-asset Side (get Req "product"))
                      (let Reserve (tx.order-reserve Side Size Amount Fee)
                        (if (> Reserve (tx.available State Asset)) "insufficient-available"
                          (if (> (if (= Side "buy") Reserve Amount) (tx.risk-available State)) "risk-ceiling"
                            (obj [["fee" Fee] ["reserve" Reserve] ["asset" Asset]]))))))))))))))

(define tx.admit-new
  State Req Metadata ->
    (let Amounts (tx.admit-amounts State Req Metadata)
      (if (not (obj? Amounts)) (tx.result State Amounts null)
        (let Id (get Req "client-id")
          (let Reserve (get Amounts "reserve")
            (let Asset (get Amounts "asset")
              (let Order (obj [["client-id" Id] ["venue-id" null] ["product" (get Req "product")]
                               ["side" (get Req "side")] ["price" (get Req "price")]
                               ["size" (get Req "size")] ["remaining" (get Req "size")]
                               ["reservation" Reserve] ["reserve-asset" Asset]
                               ["fee-budget" (get Amounts "fee")] ["fee-paid" 0]
                               ["post-only" (get Metadata "post-only")]
                               ["phase" "pending-send"] ["submitted" false] ["cancel-outcome" null]
                               ["origin-release" (get Req "release")]
                               ["expiry" (+ (get State "clock") (get Req "lifetime"))]])
                (tx.result (tx.put-order (tx.reserve-delta State Asset Reserve) Order)
                  "admitted" Order))))))))

(define tx.admit
  State Req ->
    (let Product (get Req "product")
      (let Metadata (get (get State "metadata") Product)
        (let ClientId (get Req "client-id")
          (let Side (get Req "side")
            (if (not (= "trading" (get State "mode"))) (tx.result State "admission-fenced" null)
              (if (or (get (get State "disabled") Product) (or (= Metadata null) (not (get Metadata "online")))) (tx.result State "instrument-disabled" null)
                (if (= null (tx.active-book State Product 60000)) (tx.result State "stale-market" null)
                  (if (or (not (tx.uuid ClientId)) (not (tx.positive (get Req "lifetime") 86400000)))
                    (tx.result State "invalid-intent" null)
                    (if (not (or (= Side "buy") (= Side "sell"))) (tx.result State "invalid-side" null)
                      (if (not (= null (tx.order State ClientId))) (tx.result State "duplicate-client-id" null)
                        (if (>= (len (get State "orders")) 1024) (tx.result State "order-history-full" null)
                          (tx.admit-new State Req Metadata)))))))))))))

(define tx.total-filled
  Order -> (- (get Order "size") (get Order "remaining")))

(define tx.order-phase-with-fill
  Order Remaining ->
    (if (= Remaining 0) "fully-filled"
      (if (= "cancel-pending" (tx.phase Order)) "cancel-pending" "partially-filled")))

(define tx.remaining-reserve
  Order Remaining Paid ->
    (if (= 0 Remaining) 0
      (tx.order-reserve (get Order "side") Remaining (tx.notional (get Order "price") Remaining)
        (- (get Order "fee-budget") Paid))))

(define tx.mark-terminal
  State Order Phase ->
    (let New (merge Order (obj [["phase" Phase] ["terminal-at" (get State "clock")]]))
      (tx.result (tx.replace-order State Order New 0) Phase (tx.set New "reservation" 0))))

(define tx.track-id
  State Order VenueId ->
    (tx.set State "venue-orders"
      (tx.set (get State "venue-orders") VenueId (get Order "client-id"))))

(define tx.acknowledge
  State Order VenueId ->
    (let Client (get Order "client-id")
      (let Known (get Order "venue-id")
        (let Existing (get (get State "venue-orders") VenueId)
          (if (or (not (text? VenueId 1 72))
                (or (and (not (= Known null)) (not (= Known VenueId)))
                  (and (not (= Existing null)) (not (= Client Existing)))))
            (tx.restrict State "contradictory-order-identity" Client)
            (let Phase (tx.phase Order)
              (let Next (if (or (= Phase "sent-unknown") (= Phase "reconciliation-required")) "acknowledged" Phase)
                (let New (merge Order (obj [["phase" Next] ["venue-id" VenueId]]))
                  (tx.result (tx.put-order (tx.track-id State Order VenueId) New) Next New)))))))))
