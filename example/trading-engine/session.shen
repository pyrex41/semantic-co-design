(define tx.mode
  State Mode -> (tx.result (tx.set State "mode" Mode) Mode null))

(define tx.writer
  State Req ->
    (let Fence (get Req "fence")
      (if (and (tx.positive Fence 9000000000000000000) (> Fence (get State "fence")))
        (tx.mode (tx.disconnect-books (merge (tx.recover-unstarted State (tx.open-ids State) 0) (obj [["fence" Fence] ["blocked" true]])) (tx.products) 0)
          "reconciliation-required")
        (tx.result State "stale-fence" null))))

(define tx.start
  State Req ->
    (if (get State "started") (tx.result State "already-initialized" null)
      (let Balances (tx.statement (get Req "accounts"))
        (if (or (= Balances null) (not (contains ["sandbox" "production" null] (get Req "environment"))))
          (tx.restrict State "invalid-opening-statement" null)
          (let New (tx.balance-opening (merge State (obj [["started" true] ["balances" Balances]
                    ["environment" (if (= null (get Req "environment")) "sandbox" (get Req "environment"))]])) Balances)
            (tx.result New "initialized" Balances))))))

(define tx.metadata-at
  State Product -> (get (get State "metadata") Product))

(define tx.metadata
  State Req ->
    (let Product (get Req "product")
      (if (= (tx.base Product) null) (tx.result State "unknown-product" Product)
        (let Current (tx.product-metadata Product (get Req "body"))
          (let Metadata (get State "metadata")
            (tx.result (tx.set State "metadata" (if (= Current null) (remove Metadata Product)
                                                      (tx.set Metadata Product Current)))
              (if (= Current null) "invalid-metadata" "metadata") Current))))))

(define tx.set-book
  State Product Book -> (tx.set State "books" (tx.set (get State "books") Product Book)))

(define tx.book
  State Product ->
    (let Book (get (get State "books") Product)
      (if (= Book null) (tx.empty-book) Book)))

(define tx.book-result
  State Product Book ->
    (tx.result (tx.set-book State Product Book) (if (get Book "valid") "book" "resnapshot-required")
      (tx.book-view Book)))

(define tx.market-time
  State Req ->
    (let Now (get State "clock")
      (let Timestamp (get Req "wire-time")
        (if (and (not (get Req "live")) (= Timestamp null)) Now
          (if (and (tx.bounded Timestamp 9000000000000000000) (<= Timestamp (+ Now 1000)))
            (tx.min Now Timestamp) -1)))))

(define tx.market-snapshot
  State Req ->
    (let Product (get Req "product")
      (if (= (tx.base Product) null) (tx.result State "unknown-product" Product)
        (let Current (tx.book State Product)
          (let Time (tx.market-time State Req)
            (let Book (if (< Time 0) null (tx.snapshot (get Req "body") Time))
              (if (or (= Book null) (< (get Book "sequence") (get Current "sequence")))
                (tx.book-result State Product (tx.set Current "valid" false))
                (tx.book-result State Product Book))))))))

(define tx.market-delta
  State Req ->
    (let Data (get Req "body")
      (let Product (get Data "product_id")
        (if (= (tx.base Product) null) (tx.result State "unknown-product" Product)
          (let Book (tx.book State Product)
            (let Time (tx.market-time State Req)
              (tx.book-result State Product (if (< Time 0) (tx.set Book "valid" false) (tx.delta Book Data Time)))))))))

(define tx.disconnect-books
  State Products Position ->
    (if (= Position (len Products)) State
      (let Product (at Products Position)
        (tx.disconnect-books (tx.set-book State Product (tx.set (tx.book State Product) "valid" false))
          Products (+ Position 1)))))

(define tx.venue-open
  Orders Open Offset Found ->
    (if (= Offset (len Open)) Found
      (let Order (get Orders (at Open Offset))
        (let Phase (tx.phase Order)
          (let VenueId (get Order "venue-id")
            (if (= Phase "pending-send") (tx.venue-open Orders Open (+ Offset 1) Found)
              (if (or (= VenueId null) (or (= Phase "sent-unknown") (= Phase "reconciliation-required"))) null
                (tx.venue-open Orders Open (+ Offset 1) (tx.set Found VenueId true)))))))))

(define tx.matches-open-record
  State Data Id ->
    (let Client (get (get State "venue-orders") Id)
      (let Order (tx.order State Client)
        (and (not (= Order null))
          (and (contains ["open" "pending" "active"] (get Data "status"))
            (and (tx.order-from-evidence Order Data)
              (= (tx.total-filled Order) (tx.decimal (get Data "filled_size") 8))))))))

(define tx.rows-open
  State Rows Offset Found ->
    (if (= Offset (len Rows)) Found
      (let Data (at Rows Offset)
        (let Id (get Data "id")
          (if (or (not (text? Id 1 72))
                (or (not (= (get Found Id) null)) (not (tx.matches-open-record State Data Id)))) null
            (tx.rows-open State Rows (+ Offset 1) (tx.set Found Id true)))))))

(define tx.holds-explained
  State Rows Offset ->
    (if (= Offset (len Rows)) true
      (let Row (at Rows Offset)
        (let Asset (get Row "currency")
          (if (= (tx.amount-limit Asset) 0) (tx.holds-explained State Rows (+ Offset 1))
            (let Scale (if (= Asset "USD") 16 8)
              (let Held (tx.decimal (get Row "hold") Scale)
                (let Available (tx.decimal (get Row "available") Scale)
                  (let Total (tx.balance State Asset)
                    (and (tx.bounded Held (tx.reserved State Asset))
                      (and (<= Held Total) (and (= Available (- Total Held))
                        (tx.holds-explained State Rows (+ Offset 1))))))))))))))

(define tx.reconcile
  State Req ->
    (let From (get Req "from-cursor")
      (let To (get Req "to-cursor")
        (let Cursor (get State "cursor")
          (if (or (not (get State "started"))
                (not (and (= true (get Req "fills-complete"))
                       (and (tx.bounded From Cursor) (and (tx.bounded To 9000000000000000000) (>= To Cursor))))))
            (tx.restrict State "history-incomplete" Cursor)
            (let Rows (get Req "open-orders")
              (let Actual (if (and (list? Rows) (<= (len Rows) 1024)) (tx.rows-open State Rows 0 (obj [])) null)
                (let Expected (tx.venue-open (get State "orders") (tx.open-ids State) 0 (obj []))
                  (if (or (= Expected null) (not (= Expected Actual)))
                    (tx.restrict State "open-orders-unexplained" (obj [["expected" Expected] ["actual" Actual]]))
                    (let Balances (tx.statement (get Req "accounts"))
                      (if (or (= Balances null) (not (tx.balance-equal (get State "balances") Balances)))
                        (tx.restrict State "balance-difference" Balances)
                        (if (not (tx.holds-explained State (get Req "accounts") 0))
                          (tx.restrict State "unexplained-venue-hold" null)
                          (let New (merge State (obj [["cursor" To] ["blocked" false]
                                     ["mode" (if (= "reconciliation-required" (get State "mode")) "paused" (get State "mode"))]]))
                            (tx.result New "reconciled" Balances))))))))))))))


(define tx.adjust
  State Req ->
    (let Reason (get Req "reason")
      (let Approver (get Req "approver")
        (let Asset (get Req "asset")
          (let Amount (get Req "amount")
            (let Limit (tx.amount-limit Asset)
              (if (not (and (get State "blocked")
                         (and (text? Reason 8 400) (and (text? Approver 1 100)
                           (and (not (= Approver (get Req "actor")))
                             (and (> Limit 0) (and (int? Amount) (and (>= Amount (- 0 Limit)) (<= Amount Limit)))))))))
                (tx.result State "invalid-adjustment" null)
                (if (or (not (tx.postable State Asset Amount))
                        (and (< Amount 0) (> (- 0 Amount) (tx.available State Asset))))
                  (tx.result State "adjustment-exceeds-balance" null)
                  (let Updated (tx.change-balance State Asset Amount)
                    (tx.result (tx.journal Updated "adjustment" (obj [["reason" Reason] ["approver" Approver]
                                 ["actor" (get Req "actor")]]) (tx.postings Asset "asset" "adjustment" Amount))
                               "adjusted" (tx.balance-view Updated Asset)))))))))))

(define tx.kill-orders
  State Ids Offset ->
    (if (= Offset (len Ids)) State
      (tx.kill-orders (get (tx.cancel State (at Ids Offset)) "state") Ids (+ Offset 1))))

(define tx.kill
  State ->
    (let New (tx.kill-orders (merge State (obj [["mode" "cancel-only"] ["kill-latched" true]])) (tx.open-ids State) 0)
      (tx.result New "cancel-only" (obj [["outstanding" (tx.open-ids New)] ["reserved" (get New "reserved")]]))))

(define tx.resume
  State ->
    (if (or (not (get State "started"))
          (or (get State "blocked")
            (and (get State "kill-latched") (> (len (tx.open-ids State)) 0))))
      (tx.result State "resume-blocked" (tx.open-ids State))
      (tx.mode (tx.set State "kill-latched" false) "trading")))

(define tx.limits
  State Req ->
    (let Ceiling (get Req "ceiling")
      (let Fee (get Req "fee-ppm")
        (if (and (tx.bounded Ceiling (tx.amount-limit "USD")) (tx.bounded Fee 250000))
          (tx.result (merge State (obj [["risk-ceiling" Ceiling] ["fee-ppm" Fee]])) "limits-updated"
            (obj [["ceiling" Ceiling] ["fee-ppm" Fee]]))
          (tx.result State "invalid-limits" null)))))

(define tx.disable
  State Product ->
    (let Metadata (tx.metadata-at State Product)
      (if (= Metadata null) (tx.result State "unknown-product" Product)
        (tx.result (tx.set State "disabled" (tx.set (get State "disabled") Product true))
          "instrument-disabled" Product))))

(define tx.expire-orders
  State Ids Offset ->
    (if (= Offset (len Ids)) State
      (let Order (tx.order State (at Ids Offset))
        (let Updated (if (and (not (= "cancel-pending" (tx.phase Order)))
                            (>= (get State "clock") (get Order "expiry")))
                        (get (tx.cancel-order State Order) "state") State)
          (tx.expire-orders Updated Ids (+ Offset 1))))))

(define tx.work-list
  State Ids Offset Work ->
    (if (= Offset (len Ids)) Work
      (let Id (at Ids Offset)
        (let Order (tx.order State Id)
          (let Phase (tx.phase Order)
            (tx.work-list State Ids (+ Offset 1)
              (if (or (tx.cancel-dispatchable Order)
                    (and (= Phase "pending-send") (= "trading" (get State "mode"))))
                (prepend Id Work) Work)))))))

(define tx.tick
  State ->
    (let Orders (tx.open-ids State)
      (let New (tx.expire-orders State Orders 0)
        (tx.result New "ticked" (tx.work-list New (tx.open-ids New) 0 [])))))

(define tx.rotate
  State Epoch ->
    (if (and (tx.positive Epoch 9000000000000000000) (> Epoch (get State "credential-epoch")))
      (tx.mode (merge (tx.recover-unstarted State (tx.open-ids State) 0) (obj [["credential-epoch" Epoch] ["blocked" true]])) "reconciliation-required")
      (tx.result State "invalid-credential-epoch" null)))

(define tx.channel-complete
  Channels Position ->
    (if (= Position (len Channels)) false
      (let Channel (at Channels Position)
        (if (= "full" (get Channel "name"))
          (let Products (get Channel "product_ids")
            (and (list? Products) (and (contains Products "BTC-USD") (contains Products "BCH-USD"))))
          (tx.channel-complete Channels (+ Position 1))))))

(define tx.feed-rest-work
  Product -> [(obj [["kind" "product"] ["product" Product]])
              (obj [["kind" "snapshot"] ["product" Product]])])

(define tx.received-receipt
  State Body ->
    (let Id (if (= null (get Body "client-oid")) (get Body "client_oid") (get Body "client-oid"))
      (if (not (tx.uuid Id)) State
        (let Order (tx.order State Id)
          (if (or (= Order null) (not (get Order "submitted"))) State
            (let Data (merge Body (obj [["client_oid" Id] ["id" (get Body "order_id")]]))
              (if (tx.order-from-evidence Order Data)
                (get (tx.acknowledge State Order (get Data "id")) "state")
                (get (tx.restrict State "contradictory-received-receipt" Id) "state"))))))))

(define tx.owned-venue-order
  State Id -> (and (text? Id 1 72) (not (= (get (get State "venue-orders") Id) null))))

(define tx.match-work
  State Body ->
    (let Maker (get Body "maker_order_id")
      (let Taker (get Body "taker_order_id")
        (if (tx.owned-venue-order State Maker) Maker
          (if (tx.owned-venue-order State Taker) Taker null)))))

(define tx.apply-venue-market-frame
  State Body Req ->
    (let Kind (get Body "type")
      (let Account (if (= Kind "received") (tx.received-receipt State Body) State)
        (let Market (tx.market-delta Account Req)
          (let New (get Market "state")
            (if (= "resnapshot-required" (get Market "status"))
              (tx.result New "feed-resnapshot" [(obj [["kind" "snapshot"] ["product" (get Body "product_id")]])])
              (let Work (if (= Kind "match") (tx.match-work New Body) null)
                (if (or (= Work null) (< (get New "clock") (get New "next-private"))) Market
                  (tx.result (tx.private-next New) "feed-fills"
                    [(obj [["kind" "order-fills"] ["venue-id" Work]])])))))))))

(define tx.venue-frame
  State Req ->
    (let Body (get Req "body")
      (let Kind (get Body "type")
        (if (= Kind "subscriptions")
          (let Channels (get Body "channels")
            (if (and (list? Channels) (tx.channel-complete Channels 0))
              (tx.result State "feed-subscribed" [(at (tx.feed-rest-work "BTC-USD") 0)
                                                   (at (tx.feed-rest-work "BTC-USD") 1)
                                                   (at (tx.feed-rest-work "BCH-USD") 0)
                                                   (at (tx.feed-rest-work "BCH-USD") 1)])
              (tx.result (tx.disconnect-books State (tx.products) 0) "session-rejected" null)))
          (if (= Kind "error") (tx.result (tx.disconnect-books State (tx.products) 0) "session-rejected" Body)
            (let Product (get Body "product_id")
              (if (= null (tx.base Product)) (tx.result State "ignored-frame" null)
                (tx.apply-venue-market-frame State Body Req))))))))

(define tx.rest-fill-page
  State VenueId Rows Offset ->
    (if (< Offset 0) (tx.result State "fill-page-observed" null)
      (let Row (at Rows Offset)
        (if (not (= VenueId (get Row "order_id")))
          (tx.restrict State "fill-page-order-mismatch" VenueId)
          (let Result (tx.fill State Row)
            (if (contains ["duplicate-fill" "unsettled-fill" "partially-filled" "fully-filled" "cancel-pending"]
                  (get Result "status"))
              (tx.rest-fill-page (get Result "state") VenueId Rows (- Offset 1)) Result))))))

(define tx.rest-result
  State Req ->
    (let Kind (get Req "kind")
      (let Response (get Req "response")
        (let Code (get Response "status")
          (if (not (and (= "response" (get Response "network")) (and (tx.bounded Code 299) (>= Code 200))))
            (if (= Kind "snapshot")
              (let Product (get Req "product")
                (if (= null (tx.base Product)) (tx.result State "unknown-product" Product)
                  (tx.book-result State Product (tx.set (tx.book State Product) "valid" false))))
              (tx.result State "rest-unavailable" null))
            (let Normalized (tx.set Req "body" (get Response "body"))
              (if (= Kind "snapshot") (tx.market-snapshot State Normalized)
                (if (= Kind "product") (tx.metadata State Normalized)
                  (if (= Kind "order-fills")
                    (let Rows (get Response "body")
                      (if (and (list? Rows) (<= (len Rows) 100)) (tx.rest-fill-page State (get Req "venue-id") Rows (- (len Rows) 1))
                        (tx.restrict State "invalid-fill-page" null)))
                    (tx.result State "rest-observed" (get Response "body")))))))))))
