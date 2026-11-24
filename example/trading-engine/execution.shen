(define tx.http
  Method Path Query Body Private ->
    (obj [["method" Method] ["path" Path] ["query" Query] ["body" Body] ["private" Private]]))

(define tx.get-http
  Path Query Private -> (tx.http "GET" Path Query null Private))

(define tx.product-path
  Product -> (concat ["/products/" Product]))

(define tx.client-path
  Client -> (concat ["/orders/client:" Client]))

(define tx.submit-request
  Order ->
    (tx.http "POST" "/orders" (obj [])
      (obj [["type" "limit"] ["time_in_force" "GTC"] ["client_oid" (get Order "client-id")]
            ["side" (get Order "side")] ["product_id" (get Order "product")] ["stp" "dc"]
            ["price" (tx.format-decimal (get Order "price") 2)]
            ["size" (tx.format-decimal (get Order "size") 8)] ["post_only" (get Order "post-only")]]) true))

(define tx.cancel-request
  Order -> (tx.http "DELETE" (tx.client-path (get Order "client-id"))
    (obj [["product_id" (get Order "product")]]) null true))

(define tx.lookup-request
  Order -> (tx.get-http (tx.client-path (get Order "client-id")) (obj []) true))

(define tx.private-next
  State -> (tx.set State "next-private" (+ (get State "clock") 100)))

(define tx.order-work
  State Order Stage ->
    (tx.result (tx.private-next (tx.put-order State Order)) "claimed"
      (obj [["client-id" (get Order "client-id")] ["stage" Stage]
            ["credential-epoch" (get State "credential-epoch")]
            ["request" (tx.connect-request State (if (= Stage "send") (tx.submit-request Order) (tx.cancel-request Order)))]])))

(define tx.claim-cancel
  State Order ->
    (if (not (= (get Order "cancel-outcome") null)) (tx.result State "cancel-awaiting-venue" null)
      (tx.order-work State (tx.set Order "cancel-outcome" "unknown") "cancel")))

(define tx.send-instrument-valid
  State Order ->
    (let Product (get Order "product")
      (let Live (get (get State "metadata") Product)
        (and (not (get (get State "disabled") Product))
          (and (get Live "online")
            (and (not (= null (tx.active-book State Product 60000)))
              (and (= 0 (shen.mod (get Order "size") (get Live "quantity-increment")))
                (and (= 0 (shen.mod (get Order "price") (get Live "price-increment")))
                  (or (not (get Live "post-only")) (get Order "post-only"))))))))))

(define tx.currently-sendable
  State Order ->
    (and (< (get State "clock") (get Order "expiry"))
      (and (tx.send-instrument-valid State Order)
        (let Orders (get State "orders")
          (<= (tx.gross-exposure Orders (keys Orders) 0 0) (get State "risk-ceiling"))))))

(define tx.claim-order
  State Order ->
    (if (not (= "trading" (get State "mode"))) (tx.result State "send-fenced" null)
      (if (not (tx.currently-sendable State Order)) (tx.mark-terminal State Order "cancelled")
        (tx.order-work State (merge Order (obj [["phase" "sent-unknown"] ["submitted" true]])) "send"))))

(define tx.claim
  State Id ->
    (let Order (if (tx.uuid Id) (tx.order State Id) null)
      (if (= Order null) (tx.result State "unknown-order" null)
        (if (< (get State "clock") (get State "next-private")) (tx.result State "rate-limited" null)
          (if (= (tx.phase Order) "pending-send") (tx.claim-order State Order)
            (if (= (tx.phase Order) "cancel-pending") (tx.claim-cancel State Order)
              (tx.result State "no-work" (tx.phase Order))))))))

(define tx.order-from-evidence
  Order Data ->
    (and (= (get Order "product") (get Data "product_id"))
      (and (= (get Order "side") (get Data "side"))
        (and (= (get Order "price") (tx.decimal (get Data "price") 2))
          (and (= (get Order "size") (tx.decimal (get Data "size") 8))
            (and (or (= (get Order "client-id") (get Data "client_oid"))
                     (and (text? (get Order "venue-id") 1 72) (= (get Order "venue-id") (get Data "id"))))
              (text? (get Data "id") 1 72)))))))

(define tx.send-result
  State Req ->
    (let Order (tx.order State (get Req "client-id"))
      (if (= Order null) (tx.restrict State "response-for-unknown-order" null)
        (let Kind (get Req "outcome")
          (let Body (get Req "body")
            (if (not (get Order "submitted")) (tx.restrict State "response-before-claim" (get Req "client-id"))
              (if (= Kind "accepted")
                (if (= "rejected" (tx.phase Order)) (tx.restrict State "ack-after-rejection" (get Req "client-id"))
                  (if (tx.order-from-evidence Order Body) (tx.acknowledge State Order (get Body "id"))
                    (tx.restrict State "unproven-acknowledgement" (get Req "client-id"))))
                (if (and (= Kind "rejected") (= "sent-unknown" (tx.phase Order)))
                  (tx.mark-terminal State Order "rejected")
                  (if (and (= Kind "not-sent") (= "sent-unknown" (tx.phase Order)))
                    (tx.result (tx.put-order State (merge Order (obj [["phase" "pending-send"] ["submitted" false]]))) "pending-send" null)
                    (if (tx.terminal (tx.phase Order)) (tx.result State "already-terminal" Order)
                      (tx.restrict (tx.put-order State (tx.set Order "phase" "reconciliation-required"))
                        "send-outcome-unknown" (get Req "client-id"))))))))))))

(define tx.cancel-order
  State Order ->
    (let Phase (tx.phase Order)
      (if (tx.terminal Phase) (tx.result State "already-terminal" Order)
        (if (= Phase "pending-send") (tx.mark-terminal State Order "cancelled")
          (let Updated (tx.set Order "phase" "cancel-pending")
            (tx.result (tx.put-order State Updated) "cancel-pending" Updated))))))

(define tx.cancel
  State Id ->
    (let Order (tx.order State Id)
      (if (= Order null) (tx.result State "unknown-order" null) (tx.cancel-order State Order))))

(define tx.cancel-result
  State Req ->
    (let Order (tx.order State (get Req "client-id"))
      (if (= Order null) (tx.restrict State "cancel-for-unknown-order" null)
        (if (tx.terminal (tx.phase Order)) (tx.result State "already-terminal" Order)
          (if (or (not (= "cancel-pending" (tx.phase Order))) (not (= "unknown" (get Order "cancel-outcome"))))
            (tx.restrict State "cancel-response-before-claim" (get Req "client-id"))
            (let Outcome (get Req "outcome")
              (if (= Outcome "not-sent")
                (tx.result (tx.put-order State (tx.set Order "cancel-outcome" null)) "cancel-pending" null)
                (if (and (= Outcome "accepted")
                      (or (= (get Order "client-id") (get Req "body"))
                        (and (not (= null (get Order "venue-id"))) (= (get Order "venue-id") (get Req "body")))))
                  (tx.result (tx.put-order State (tx.set Order "cancel-outcome" "accepted")) "cancel-awaiting-proof" null)
                  (tx.restrict State "cancel-outcome-unknown" (get Req "client-id"))))))))))

(define tx.terminal-evidence-phase
  Order Data ->
    (let Status (get Data "status")
      (let Reason (get Data "done_reason")
        (if (and (= Status "rejected") (= (tx.total-filled Order) 0)) "rejected"
          (if (contains ["done" "settled"] Status)
            (if (and (= Reason "filled") (= (get Order "remaining") 0)) "fully-filled"
              (if (and (contains ["canceled" "cancelled"] Reason) (> (get Order "remaining") 0)) "cancelled" null))
            null)))))

(define tx.open-evidence
  State Order Req Data ->
    (if (tx.terminal (tx.phase Order)) (tx.restrict State "venue-reopened-terminal-order" (get Order "client-id"))
      (let Ack (tx.acknowledge State Order (get Data "id"))
        (if (and (= (get Ack "status") "cancel-pending") (= true (get Req "retry-cancel")))
          (tx.result (tx.put-order (get Ack "state") (tx.set (get Ack "data") "cancel-outcome" null))
            "cancel-pending" (get Ack "data")) Ack))))

(define tx.evidence-status
  State Order Req Data ->
    (let LocalFilled (tx.total-filled Order)
      (let RemoteFilled (tx.decimal (get Data "filled_size") 8)
        (if (not (= RemoteFilled LocalFilled)) (tx.restrict State "missing-settled-fills" (get Order "client-id"))
          (if (contains ["open" "pending" "active"] (get Data "status")) (tx.open-evidence State Order Req Data)
            (if (not (and (= true (get Req "fills-complete")) (= true (get Data "settled"))))
              (tx.restrict State "incomplete-terminal-evidence" (get Order "client-id"))
              (let Final (tx.terminal-evidence-phase Order Data)
                (if (or (= Final null) (and (tx.terminal (tx.phase Order)) (not (= Final (tx.phase Order)))))
                  (tx.restrict State "unsupported-order-evidence" (get Order "client-id"))
                  (tx.mark-terminal State Order Final)))))))))

(define tx.order-evidence
  State Req ->
    (let Id (get Req "client-id")
      (let Order (tx.order State Id)
        (let Data (get Req "body")
          (if (= Order null) (tx.restrict State "unexpected-venue-order" Id)
            (if (not (get Order "submitted")) (tx.restrict State "unexpected-unsent-order" Id)
              (if (not (tx.order-from-evidence Order Data)) (tx.restrict State "missing-order-proof" Id)
                (let Ack (tx.acknowledge State Order (get Data "id"))
                  (if (= (get Ack "status") "contradictory-order-identity") Ack
                    (tx.evidence-status (get Ack "state") (get Ack "data") Req Data))))))))))

(define tx.origin
  "sandbox" "WS" -> "wss://ws-feed-public.sandbox.exchange.coinbase.com"
  "production" "WS" -> "wss://ws-feed.exchange.coinbase.com"
  "sandbox" _ -> "https://api-public.sandbox.exchange.coinbase.com"
  "production" _ -> "https://api.exchange.coinbase.com")

(define tx.auth-fields
  "WS" -> (obj [["key" "key"] ["passphrase" "passphrase"] ["signature" "signature"] ["timestamp" "timestamp"]])
  _ -> (obj [["CB-ACCESS-KEY" "key"] ["CB-ACCESS-PASSPHRASE" "passphrase"]
             ["CB-ACCESS-SIGN" "signature"] ["CB-ACCESS-TIMESTAMP" "timestamp"]]))

(define tx.connect-request
  State Request ->
    (let Method (get Request "method")
      (let Env (get State "environment")
        (merge Request (obj [["endpoint" (tx.origin Env Method)] ["scope" Env]
                             ["epoch" (get State "credential-epoch")]
                             ["auth" (obj [["scheme" "hmac-sha256/base64"] ["fields" (tx.auth-fields Method)]])]])))))

(define tx.authorize-send
  State Req ->
    (let Order (tx.order State (get Req "client-id"))
      (let Stage (get Req "stage")
        (let Request (get Req "request")
          (if (and (not (= Order null))
                (and (= (get Req "credential-epoch") (get State "credential-epoch"))
                  (if (= Stage "send")
                    (and (= "sent-unknown" (tx.phase Order))
                      (and (= "trading" (get State "mode"))
                        (and (tx.currently-sendable State Order)
                          (= (tx.connect-request State (tx.submit-request Order)) Request))))
                    (and (= Stage "cancel")
                      (and (= "cancel-pending" (tx.phase Order))
                        (and (= "unknown" (get Order "cancel-outcome"))
                          (= (tx.connect-request State (tx.cancel-request Order)) Request)))))))
            (tx.result State "transport-authorized" null)
            (tx.result State "transport-fenced" null))))))

(define tx.http-result
  State Req ->
    (let Response (get Req "response")
      (let Network (get Response "network")
        (let Code (get Response "status")
          (let Body (get Response "body")
            (let Outcome (if (= Network "not-sent") "not-sent"
                           (if (and (= Network "response") (and (tx.bounded Code 299) (>= Code 200)))
                             (if (= (get Body "status") "rejected") "rejected" "accepted") "unknown"))
              (let Event (merge Req (obj [["outcome" Outcome] ["body" Body]]))
                (if (= "send" (get Req "stage")) (tx.send-result State Event)
                  (if (= "cancel" (get Req "stage")) (tx.cancel-result State Event)
                    (tx.result State "unknown-transport-stage" null))))))))))
