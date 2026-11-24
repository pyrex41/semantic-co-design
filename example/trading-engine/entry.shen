
(define tx.status
  State ->
    (obj [["schema" (get State "schema")] ["mode" (get State "mode")] ["clock" (get State "clock")]
          ["fence" (get State "fence")] ["environment" (get State "environment")] ["cursor" (get State "cursor")]
          ["credential-epoch" (get State "credential-epoch")] ["risk-ceiling" (get State "risk-ceiling")]
          ["outstanding" (tx.open-ids State)] ["venue-risk-blocked" (get State "blocked")]
          ["kill-latched" (get State "kill-latched")]
          ["balances" (obj [["USD" (tx.balance-view State "USD")]
                            ["BTC" (tx.balance-view State "BTC")] ["BCH" (tx.balance-view State "BCH")]])]
          ["books" (obj [["BTC-USD" (tx.book-view (tx.book State "BTC-USD"))]
                         ["BCH-USD" (tx.book-view (tx.book State "BCH-USD"))]])]]))

(define tx.wire
  "product" _ Product -> (tx.get-http (tx.product-path Product) (obj []) false)
  "snapshot" _ Product -> (tx.get-http (concat [(tx.product-path Product) "/book"]) (obj [["level" 3]]) false)
  "accounts" _ _ -> (tx.get-http "/accounts" (obj []) true)
  "open-orders" _ _ -> (tx.get-http "/orders" (obj [["status" "open"] ["market_type" "spot"]]) true)
  "fills" Req Product -> (tx.set (tx.get-http "/fills" (obj [["product_id" Product] ["market_type" "spot"]
                                                    ["limit" 100] ["after" (get Req "after")]]) true)
                            "response-headers" ["CB-BEFORE" "CB-AFTER"])
  "order-fills" Req _ ->
    (if (text? (get Req "venue-id") 1 72)
      (tx.set (tx.get-http "/fills" (obj [["order_id" (get Req "venue-id")] ["market_type" "spot"]
                                        ["after" (get Req "after")] ["limit" 100]]) true)
        "response-headers" ["CB-BEFORE" "CB-AFTER"])
      null)
  "lookup" Req _ -> (if (tx.uuid (get Req "client-id"))
                      (tx.get-http (tx.client-path (get Req "client-id")) (obj []) true) null)
  "subscribe" _ _ -> (obj [["method" "WS"] ["private" true] ["sign-method" "GET"]
                            ["path" "/users/self/verify"]
                            ["body" (obj [["type" "subscribe"] ["product_ids" (tx.products)] ["channels" ["full"]]])]])
  _ _ _ -> null)

(define tx.wire-entry
  State Req ->
    (let Kind (get Req "kind")
      (let Product (get Req "product")
        (if (and (contains ["product" "snapshot" "fills"] Kind) (= null (tx.base Product)))
          (tx.result State "unknown-product" Product)
          (let Request (tx.wire Kind Req Product)
            (if (= Request null) (tx.result State "invalid-wire-request" null)
              (tx.result State "wire-request" (tx.connect-request State Request))))))))

(define tx.report
  "blotter" State _ -> (tx.result State "blotter" (get State "orders"))
  "journal" State _ -> (tx.result State "journal" (get State "journal"))
  "executions" State _ -> (tx.result State "executions" (get State "executions"))
  "exceptions" State _ -> (tx.result State "exceptions" (get State "exceptions"))
  "status" State _ -> (tx.result State "status" (tx.status State))
  "wire" State Req -> (tx.wire-entry State Req)
  "order" State Req ->
    (let Order (tx.order State (get Req "client-id"))
      (tx.result State (if (= Order null) "unknown-order" "order") Order)))

(define tx.propose-adjustment
  State Req ->
    (let Id (get Req "adjustment-id")
      (let Pending (get State "adjustments")
        (if (or (not (get State "blocked"))
                (or (not (tx.uuid Id)) (not (= null (get Pending Id)))))
          (tx.result State "invalid-adjustment-id" null)
          (let Proposal (obj [["actor" (get Req "actor")] ["reason" (get Req "reason")]
                               ["amount" (get Req "amount")] ["asset" (get Req "asset")]] )
            (tx.result (tx.set State "adjustments" (tx.set Pending Id Proposal)) "adjustment-proposed" Proposal))))))

(define tx.approve-adjustment
  State Req ->
    (let Id (get Req "adjustment-id")
      (let Pending (get State "adjustments")
        (let Proposal (get Pending Id)
          (if (= Proposal null) (tx.result State "unknown-adjustment" null)
            (let Decision (tx.adjust State (tx.set Proposal "approver" (get Req "actor")))
              (if (= "adjusted" (get Decision "status"))
                (tx.result (tx.set (get Decision "state") "adjustments" (remove Pending Id))
                  "adjusted" (get Decision "data")) Decision)))))))

(define tx.activate-release
  State Req ->
    (let Change (obj [["actor" (get Req "actor")] ["from" (get Req "previous-release")]
                     ["to" (get Req "release")] ["schema" (get State "schema")]])
      (tx.result (tx.journal State "release-activation" Change []) "release-activated" Change)))

(define tx.event
  "activate-release" State Req -> (tx.activate-release State Req)
  "acquire-writer" State Req -> (tx.writer State Req)
  "start" State Req -> (tx.start State Req)
  "metadata" State Req -> (tx.metadata State Req)
  "snapshot" State Req -> (tx.market-snapshot State Req)
  "delta" State Req -> (tx.market-delta State Req)
  "venue-frame" State Req -> (tx.venue-frame State Req)
  "rest-result" State Req -> (tx.rest-result State Req)
  "disconnect" State _ -> (tx.result (tx.disconnect-books State (tx.products) 0) "resnapshot-required" null)
  "configure-strategy" State Req -> (tx.strategy-set State Req)
  "strategy" State Req -> (tx.strategy State Req)
  "admit" State Req -> (tx.admit State Req)
  "claim" State Req -> (tx.claim State (get Req "client-id"))
  "authorize-send" State Req -> (tx.authorize-send State Req)
  "http-result" State Req -> (tx.http-result State Req)
  "send-result" State Req -> (tx.send-result State Req)
  "cancel" State Req -> (tx.cancel State (get Req "client-id"))
  "cancel-result" State Req -> (tx.cancel-result State Req)
  "fill" State Req -> (tx.fill State (get Req "body"))
  "order-evidence" State Req -> (tx.order-evidence State Req)
  "reconcile" State Req -> (tx.reconcile State Req)
  "pause" State _ -> (tx.mode State "paused")
  "kill" State _ -> (tx.kill State)
  "resume" State _ -> (tx.resume State)
  "limits" State Req -> (tx.limits State Req)
  "disable" State Req -> (tx.disable State (get Req "product"))
  "tick" State _ -> (tx.tick State)
  "rotate" State Req -> (tx.rotate State (get Req "epoch"))
  "propose-adjustment" State Req -> (tx.propose-adjustment State Req)
  "approve-adjustment" State Req -> (tx.approve-adjustment State Req))

(define tx.run-event
  Action State Envelope ->
    (let Time (get Envelope "time")
      (let Req (get Envelope "event")
        (if (not (and (obj? Req) (tx.bounded Time 9000000000000000000)))
          (tx.result State "invalid-command" null)
          (if (< Time (get State "clock")) (tx.result State "stale-clock" null)
            (if (and (not (= Action "acquire-writer"))
                  (or (= (get State "fence") 0) (not (= (get State "fence") (get Envelope "fence")))))
              (tx.result State "stale-fence" null)
              (tx.event Action (tx.set State "clock" Time)
                (merge Req (obj [["actor" (get Envelope "actor")] ["release" (get Envelope "release")]
                                 ["previous-release" (get Envelope "previous-release")]])))))))))

(define tx.entry
  Action Envelope ->
    (let Saved (get Envelope "state")
      (let State (if (= Saved null) (tx.initial) Saved)
        (if (not (= 1 (get State "schema"))) (tx.result State "unsupported-state-schema" null)
          (if (contains ["status" "blotter" "order" "journal" "executions" "exceptions" "wire"] Action)
            (tx.report Action State (get Envelope "event"))
            (tx.run-event Action State Envelope))))))
