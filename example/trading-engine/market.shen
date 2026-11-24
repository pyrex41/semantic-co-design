(define tx.set
  Record Key Value -> (merge Record (obj [[Key Value]])))

(define tx.products
  -> ["BTC-USD" "BCH-USD"])

(define tx.base
  "BTC-USD" -> "BTC"
  "BCH-USD" -> "BCH"
  _ -> null)

(define tx.empty-book
  -> (obj [["valid" false] ["sequence" 0] ["time" 0] ["orders" (obj [])]]))

(define tx.product-metadata
  Product Data ->
    (let Price (tx.decimal (get Data "quote_increment") 2)
      (let Quantity (tx.decimal (get Data "base_increment") 8)
        (let Minimum (tx.decimal (get Data "min_market_funds") 16)
          (if (and (= Product (get Data "id"))
                (and (= (tx.base Product) (get Data "base_currency"))
                  (and (= "USD" (get Data "quote_currency"))
                    (and (> Price 0) (and (> Quantity 0) (>= Minimum 0))))))
            (obj [["price-increment" Price] ["quantity-increment" Quantity] ["minimum" Minimum]
                  ["online" (and (= "online" (get Data "status"))
                    (and (not (get Data "cancel_only"))
                      (and (not (get Data "auction_mode")) (not (get Data "trading_disabled")))))]
                  ["post-only" (= true (get Data "post_only"))]])
            null)))))

(define tx.valid-order-row
  Side Price Quantity Id ->
    (and (or (= Side "buy") (= Side "sell"))
      (and (tx.positive Price 1000000000000)
        (and (tx.bounded Quantity 10000000000000) (text? Id 1 72)))))

(define tx.levels
  Side Rows Offset Orders ->
    (if (= Offset (len Rows)) Orders
      (let Row (at Rows Offset)
        (let Price (tx.decimal (at Row 0) 2)
          (let Quantity (tx.decimal (at Row 1) 8)
            (let Id (at Row 2)
              (if (and (tx.valid-order-row Side Price Quantity Id)
                    (and (> Quantity 0) (= null (get Orders Id))))
                (tx.levels Side Rows (+ Offset 1) (tx.set Orders Id [Side Price Quantity]))
                null)))))))

(define tx.snapshot
  Data Time ->
    (let Sequence (get Data "sequence")
      (let Bids (get Data "bids")
        (let Asks (get Data "asks")
          (if (and (tx.bounded Sequence 9000000000000000000)
                (and (list? Bids) (and (list? Asks) (<= (+ (len Bids) (len Asks)) 1024))))
            (let BidOrders (tx.levels "buy" Bids 0 (obj []))
              (if (= BidOrders null) null
                (let Orders (tx.levels "sell" Asks 0 BidOrders)
                  (if (= null Orders) null
                    (obj [["valid" (not (get Data "auction_mode"))]
                          ["sequence" Sequence] ["time" Time] ["orders" Orders]])))))
            null)))))

(define tx.reduce-order
  Orders Order Id Reduction ->
    (if (= Order null) null
      (let Remaining (- (at Order 2) Reduction)
        (if (< Remaining 0) null
          (if (= Remaining 0) (remove Orders Id)
            (tx.set Orders Id [(at Order 0) (at Order 1) Remaining]))))))

(define tx.change-order
  Orders Order Id Data ->
    (if (= Order null) Orders
      (let Mode (get Data "reason")
        (if (or (= Mode "modify_order") (= Mode "replace")) null
          (let New (tx.decimal (get Data "new_size") 8)
            (if (or (< New 0) (> New 10000000000000)) null
              (if (= New 0) (remove Orders Id)
                (tx.set Orders Id [(at Order 0) (at Order 1) New]))))))))

(define tx.apply-delta
  "received" Orders _ -> Orders
  "activate" Orders _ -> Orders
  "done" Orders Data ->
    (let Id (get Data "order_id")
      (if (text? Id 1 72) (remove Orders Id) null))
  "change" Orders Data ->
    (let Id (get Data "order_id")
      (if (text? Id 1 72) (tx.change-order Orders (get Orders Id) Id Data) null))
  "match" Orders Data ->
    (let Id (get Data "maker_order_id")
      (let Quantity (tx.decimal (get Data "size") 8)
        (if (and (text? Id 1 72) (> Quantity 0))
          (let Resting (get Orders Id)
            (if (and (not (= Resting null))
                  (and (= (at Resting 0) (get Data "side"))
                    (= (at Resting 1) (tx.decimal (get Data "price") 2))))
              (tx.reduce-order Orders Resting Id Quantity) null))
          null)))
  "open" Orders Data ->
    (let Id (get Data "order_id")
      (let Price (tx.decimal (get Data "price") 2)
        (let Quantity (tx.decimal (get Data "remaining_size") 8)
          (let Side (get Data "side")
            (if (and (tx.valid-order-row Side Price Quantity Id)
                  (and (> Quantity 0) (and (< (len Orders) 1024) (= null (get Orders Id)))))
              (tx.set Orders Id [Side Price Quantity]) null)))))
  _ _ _ -> null)

(define tx.delta
  Book Data Time ->
    (let Sequence (get Data "sequence")
      (let Previous (get Book "sequence")
        (if (not (get Book "valid")) Book
          (if (not (tx.bounded Sequence 9000000000000000000)) (tx.set Book "valid" false)
            (if (<= Sequence Previous) Book
              (if (not (= Sequence (+ Previous 1))) (tx.set Book "valid" false)
                (let Orders (tx.apply-delta (get Data "type") (get Book "orders") Data)
                  (if (= Orders null) (tx.set Book "valid" false)
                    (merge Book (obj [["orders" Orders] ["sequence" Sequence] ["time" Time]])))))))))))

(define tx.best-next
  "buy" Price Size Best ->
    (if (> Price (at Best 0)) [Price Size]
      (if (= Price (at Best 0)) [Price (+ (at Best 1) Size)] Best))
  "sell" Price Size Best ->
    (if (or (= 0 (at Best 0)) (< Price (at Best 0))) [Price Size]
      (if (= Price (at Best 0)) [Price (+ (at Best 1) Size)] Best)))

(define tx.bests
  Orders Ids Offset Bid Ask ->
    (if (= Offset (len Ids)) [Bid Ask]
      (let Row (get Orders (at Ids Offset))
        (if (= "buy" (at Row 0))
          (tx.bests Orders Ids (+ Offset 1) (tx.best-next "buy" (at Row 1) (at Row 2) Bid) Ask)
          (tx.bests Orders Ids (+ Offset 1) Bid (tx.best-next "sell" (at Row 1) (at Row 2) Ask))))))

(define tx.book-view
  Book ->
    (let Orders (get Book "orders")
      (let Best (tx.bests Orders (keys Orders) 0 [0 0] [0 0])
        (let Bid (at Best 0)
          (let Ask (at Best 1)
            (obj [["valid" (get Book "valid")] ["sequence" (get Book "sequence")]
                  ["time" (get Book "time")] ["orders" (len Orders)]
                  ["bid" (at Bid 0)] ["bid_size" (at Bid 1)]
                  ["ask" (at Ask 0)] ["ask_size" (at Ask 1)]]))))))
