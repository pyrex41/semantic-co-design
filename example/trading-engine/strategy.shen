(define tx.strategy-set
  State Req ->
    (let Product (get Req "product")
      (let Lower (get Req "lower")
        (let Upper (get Req "upper")
          (let Lifetime (get Req "lifetime")
            (let MaxAge (get Req "max-age")
              (let Clip (get Req "clip")
                (let Enabled (get Req "enabled")
                  (if (not (and (not (= null (tx.base Product)))
                             (and (tx.bounded Lower 10000000000)
                               (and (tx.bounded Upper 10000000000)
                                 (and (<= Lower Upper)
                                   (and (tx.positive Lifetime 86400000)
                                     (and (tx.positive MaxAge 60000)
                                       (and (tx.positive Clip 10000000000) (bool? Enabled)))))))))
                    (tx.result State "invalid-strategy" null)
                    (let Policy (obj [["lower" Lower] ["upper" Upper] ["clip" Clip]
                                       ["lifetime" Lifetime] ["max-age" MaxAge] ["enabled" Enabled]
                                       ["release" (get Req "release")]])
                      (tx.result (tx.set State "strategies" (tx.set (get State "strategies") Product Policy))
                        "strategy-configured" Policy)))))))))))

(define tx.buy-target
  Lower -> Lower)

(define tx.inventory-direction
  Balance Lower Upper ->
    (if (< Balance Lower) "buy" (if (> Balance Upper) "sell" null)))

(define tx.floor-step
  Value Step -> (- Value (shen.mod Value Step)))

(define tx.ceil-step
  Value Step -> (+ Value (shen.mod (- Step (shen.mod Value Step)) Step)))

(define tx.open-product
  Orders Ids Offset Product ->
    (if (= Offset (len Ids)) false
      (let Order (get Orders (at Ids Offset))
        (if (= Product (get Order "product")) true
          (tx.open-product Orders Ids (+ Offset 1) Product)))))

(define tx.strategy-intent
  State Product Policy Metadata Checkpoint Req ->
    (let Balance (tx.balance State (tx.base Product))
      (let Lower (tx.buy-target (get Policy "lower"))
        (let Upper (get Policy "upper")
          (let Side (tx.inventory-direction Balance Lower Upper)
            (if (= Side null) (tx.result State "within-target-band" null)
              (let Step (get Metadata "price-increment")
                (let Size (tx.floor-step (tx.min (get Policy "clip")
                              (if (= Side "buy") (- Lower Balance) (- Balance Upper)))
                              (get Metadata "quantity-increment"))
                  (let Price (if (= Side "buy") (tx.floor-step (get Checkpoint "bid") Step)
                                (tx.ceil-step (get Checkpoint "ask") Step))
                    (if (or (<= Price 0) (<= Size 0)) (tx.result State "no-quantized-intent" null)
                      (tx.result State "intent"
                        (obj [["client-id" (get Req "client-id")] ["side" Side] ["product" Product]
                              ["price" Price] ["size" Size] ["lifetime" (get Policy "lifetime")]
                              ["strategy-release" (get Policy "release")]]))))))))))))

(define tx.strategy
  State Req ->
    (let Product (get Req "product")
      (let Policy (get (get State "strategies") Product)
        (let Metadata (tx.metadata-at State Product)
          (if (or (not (= "trading" (get State "mode"))) (not (get Policy "enabled")))
            (tx.result State "strategy-paused" null)
            (if (or (get (get State "disabled") Product) (not (get Metadata "online"))) (tx.result State "instrument-disabled" null)
              (let Book (tx.active-book State Product (get Policy "max-age"))
                (if (= Book null) (tx.result State "stale-market" null)
                  (if (tx.open-product (get State "orders") (tx.open-ids State) 0 Product)
                    (tx.result State "working-order" null)
                    (let Checkpoint (tx.book-view Book)
                      (if (or (= 0 (get Checkpoint "bid")) (>= (get Checkpoint "bid") (get Checkpoint "ask")))
                        (tx.result State "invalid-top-of-book" null)
                        (tx.strategy-intent State Product Policy Metadata Checkpoint Req))))))))))))
