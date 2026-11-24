\\ Classic IPv4 unicast eBGP. Transport hands over one bounded BGP record.
\\ Learned paths terminate here. Explicitly selected static prefixes are exported.

(define bgp.marker
  _ -> [255 255 255 255 255 255 255 255 255 255 255 255 255 255 255 255])

(define bgp.wire
  Type Body -> (append (bgp.marker 0) (append (rt.net-word (+ 19 (len Body))) (prepend Type Body))))

(define bgp.open
  Peer -> (bgp.wire 1
           (append [4] (append (rt.net-word (get Peer "local-as"))
             (append (rt.net-word (get Peer "hold")) (append (get Peer "router-id") [0]))))))

(define bgp.options
  Bytes I End Count ->
    (if (= I End) "ok"
      (if (or (> (+ I 2) End) (= Count 24)) "bad-open-length"
        (let Next (+ (+ I 2) (at Bytes (+ I 1)))
          (if (> Next End) "bad-open-length"
            (if (not (= (at Bytes I) 2)) "unsupported-optional-parameter"
              (if (bgp.capabilities Bytes (+ I 2) Next 0)
                (bgp.options Bytes Next End (+ Count 1)) "bad-open-length")))))))

(define bgp.capabilities
  Bytes I End Count ->
    (if (= I End) true
      (if (or (> (+ I 2) End) (= Count 24)) false
        (let Next (+ (+ I 2) (at Bytes (+ I 1)))
          (and (<= Next End) (bgp.capabilities Bytes Next End (+ Count 1)))))))

(define bgp.open-status
  Body Peer ->
    (if (< (len Body) 10) "bad-open-length"
      (if (not (= (at Body 0) 4)) "unsupported-version"
        (if (not (= (rt.byte-word Body 1) (get Peer "remote-as"))) "bad-peer-as"
          (if (and (> (rt.byte-word Body 3) 0) (< (rt.byte-word Body 3) 3)) "unacceptable-hold"
            (if (not (rt.unicast-ip 4 (rt.slice Body 5 9))) "bad-router-id"
              (if (not (= (+ 10 (at Body 9)) (len Body))) "bad-open-length"
                (bgp.options Body 10 (len Body) 0))))))))

(define bgp.notification
  "bad-marker" -> (bgp.wire 3 [1 1])
  "bad-length" -> (bgp.wire 3 [1 2])
  "bad-type" -> (bgp.wire 3 [1 3])
  "unsupported-version" -> (bgp.wire 3 [2 1 0 4])
  "bad-peer-as" -> (bgp.wire 3 [2 2])
  "bad-router-id" -> (bgp.wire 3 [2 3])
  "unsupported-optional-parameter" -> (bgp.wire 3 [2 4])
  "unacceptable-hold" -> (bgp.wire 3 [2 6])
  "bad-open-length" -> (bgp.wire 3 [2 0])
  "bad-update" -> (bgp.wire 3 [3 1])
  "missing-attribute" -> (bgp.wire 3 [3 3])
  "unknown-well-known-attribute" -> (bgp.wire 3 [3 2])
  "hold-timeout" -> (bgp.wire 3 [4 0])
  _ -> (bgp.wire 3 [5 0]))

(define bgp.peer-at
  Rows Id I ->
    (if (= I (len Rows)) null
      (if (= (get (at Rows I) "id") Id) (at Rows I) (bgp.peer-at Rows Id (+ I 1)))))

(define bgp.result
  State Reason Send Close ->
    (obj [["state" State] ["reason" Reason] ["send" Send] ["close" Close]
          ["generation" (get State "generation")]]))

(define bgp.without-at
  Rows Peer I Kept ->
    (if (= I (len Rows)) Kept
      (let Row (at Rows I)
        (bgp.without-at Rows Peer (+ I 1) (if (= (get Row "peer") Peer) Kept (append Kept [Row]))))))

(define bgp.routes
  State Routes ->
    (if (= Routes (get State "peer-routes")) State
      (merge State (obj [["peer-routes" Routes] ["generation" (+ 1 (get State "generation"))]]))))

(define bgp.session
  State Id Session -> (rt.put State "sessions" (rt.put (get State "sessions") Id Session)))

(define bgp.close
  State Id Reason Send ->
    (bgp.result
      (bgp.session (bgp.routes State (bgp.without-at (get State "peer-routes") Id 0 [])) Id null)
      Reason Send true))

(define bgp.reject
  State Id Reason -> (bgp.close State Id Reason [(bgp.notification Reason)]))

(define bgp.bits-size
  Bits -> (rt.div (+ Bits 7) 8))

(define bgp.normalize
  Bytes Bits ->
    (if (= (shen.mod Bits 8) 0) Bytes
      (let Last (- (bgp.bits-size Bits) 1)
        (let Unit (at [256 128 64 32 16 8 4 2] (shen.mod Bits 8))
          (rt.patch Bytes Last [(* Unit (rt.div (at Bytes Last) Unit))])))))

(define bgp.prefixes
  Bytes I End Rows ->
    (if (= I End) Rows
      (if (>= (len Rows) 64) null
        (let Bits (at Bytes I)
          (let Size (bgp.bits-size Bits)
            (let Next (+ (+ I 1) Size)
              (if (or (> Bits 32) (> Next End)) null
                (let Prefix (bgp.normalize (append (rt.slice Bytes (+ I 1) Next) (take [0 0 0 0] (- 4 Size))) Bits)
                  (bgp.prefixes Bytes Next End (append Rows [(obj [["prefix" Prefix] ["bits" Bits]])]))))))))))

(define bgp.as-segment
  Bytes Offset First End Local ->
    (if (= First End) false
      (if (= (+ First 1) End) (= (rt.byte-word Bytes (+ Offset (* First 2))) Local)
        (let Mid (rt.div (+ First End) 2)
          (or (bgp.as-segment Bytes Offset First Mid Local) (bgp.as-segment Bytes Offset Mid End Local))))))

(define bgp.as-path
  Bytes I End Local Found Segments ->
    (if (= I End) Found
      (if (or (> (+ I 2) End) (= Segments 24)) null
        (let Kind (at Bytes I)
          (let Count (at Bytes (+ I 1))
            (let Next (+ (+ I 2) (* 2 Count))
              (if (or (not (rt.contains [1 2] Kind)) (or (= Count 0) (> Next End))) null
                (bgp.as-path Bytes Next End Local (or Found (bgp.as-segment Bytes (+ I 2) 0 Count Local))
                  (+ Segments 1)))))))))

(define bgp.attribute
  1 Value _ -> (if (and (= (len Value) 1) (<= (at Value 0) 2)) Value null)
  2 Value Local -> (bgp.as-path Value 0 (len Value) Local false 0)
  3 Value _ -> (if (and (= (len Value) 4) (rt.unicast-ip 4 Value)) Value null)
  4 Value _ -> (if (= (len Value) 4) Value null)
  5 Value _ -> (if (= (len Value) 4) Value null)
  6 Value _ -> (if (= (len Value) 0) Value null)
  7 Value _ -> (if (= (len Value) 6) Value null)
  _ Value _ -> Value)

(define bgp.attributes
  Bytes I End Local Rows Count ->
    (if (= I End) (obj [["reason" "ok"] ["attrs" Rows]])
      (if (or (> (+ I 3) End) (>= Count 32)) (obj [["reason" "bad-update"]])
        (let Flags (at Bytes I)
          (let Type (at Bytes (+ I 1))
            (let Extended (= (shen.mod (rt.div Flags 16) 2) 1)
              (let Start (+ I (if Extended 4 3))
                (if (> Start End) (obj [["reason" "bad-update"]])
                  (let Next (+ Start (if Extended (rt.byte-word Bytes (+ I 2)) (at Bytes (+ I 2))))
                    (let Key (str Type)
                      (if (or (> Next End) (not (= (get Rows Key) null))) (obj [["reason" "bad-update"]])
                        (if (and (>= Type 8) (< Flags 128))
                          (obj [["reason" "unknown-well-known-attribute"]])
                          (let Value (bgp.attribute Type (rt.slice Bytes Start Next) Local)
                            (if (= Value null) (obj [["reason" "bad-update"]])
                              (bgp.attributes Bytes Next End Local (rt.put Rows Key Value) (+ Count 1))))))))))))))))

(define bgp.same-prefix
  X Y -> (and (= (get X "bits") (get Y "bits")) (= (get X "prefix") (get Y "prefix"))))

(define bgp.allowed-prefix
  Rules Prefix I ->
    (if (= I (len Rules)) false
      (or (bgp.same-prefix Prefix (at Rules I)) (bgp.allowed-prefix Rules Prefix (+ I 1)))))

(define bgp.withdraw-at
  Routes Peer Prefixes I Kept ->
    (if (= I (len Routes)) Kept
      (let R (at Routes I)
        (bgp.withdraw-at Routes Peer Prefixes (+ I 1)
          (if (and (= (get R "peer") Peer) (bgp.allowed-prefix Prefixes R 0)) Kept (append Kept [R]))))))

(define bgp.announce-at
  Routes Peer Prefixes NextHop I ->
    (if (= I (len Prefixes)) Routes
      (let Prefix (at Prefixes I)
        (bgp.announce-at
          (if (and (< (len Routes) 64) (bgp.allowed-prefix (get Peer "import") Prefix 0))
            (append Routes [(merge Prefix (obj [["peer" (get Peer "id")] ["out" (get Peer "port")] ["via" NextHop] ["metric" 200]]))])
            Routes)
          Peer Prefixes NextHop (+ I 1)))))

(define bgp.finish-update
  State Peer Body Withdraw EndWithdraw AttrStart AttrEnd ->
    (let Announce (bgp.prefixes Body AttrEnd (len Body) [])
      (if (or (= Announce null) (= Withdraw null)) (bgp.reject State (get Peer "id") "bad-update")
        (let Parsed (bgp.attributes Body AttrStart AttrEnd (get Peer "local-as") (obj []) 0)
          (if (not (= (get Parsed "reason") "ok")) (bgp.reject State (get Peer "id") (get Parsed "reason"))
            (let Attrs (get Parsed "attrs")
              (if (and (not (= Announce []))
                    (or (= (get Attrs "1") null) (or (= (get Attrs "2") null) (= (get Attrs "3") null))))
                (bgp.reject State (get Peer "id") "missing-attribute")
                (let Routes (bgp.withdraw-at (get State "peer-routes") (get Peer "id") (append Withdraw Announce) 0 [])
                  (let Updated (if (and (not (= Announce []))
                                        (and (not (get Attrs "2")) (= (get Attrs "3") (get Peer "address"))))
                                  (bgp.announce-at Routes Peer Announce (get Attrs "3") 0) Routes)
                    (bgp.result (bgp.routes State Updated) "update-applied" [] false))))))))))

(define bgp.update
  State Peer Body ->
    (if (< (len Body) 4) (bgp.reject State (get Peer "id") "bad-update")
      (let EndWithdraw (+ 2 (rt.byte-word Body 0))
        (let AttrStart (+ EndWithdraw 2)
          (if (> AttrStart (len Body)) (bgp.reject State (get Peer "id") "bad-update")
            (let AttrEnd (+ AttrStart (rt.byte-word Body EndWithdraw))
              (if (> AttrEnd (len Body)) (bgp.reject State (get Peer "id") "bad-update")
                (bgp.finish-update State Peer Body (bgp.prefixes Body 2 EndWithdraw []) EndWithdraw AttrStart AttrEnd))))))))

(define bgp.export-at
  Routes Rules I Kept ->
    (if (= I (len Routes)) Kept
      (let Route (at Routes I)
        (bgp.export-at Routes Rules (+ I 1)
          (if (and (= (len (get Route "prefix")) 4) (bgp.allowed-prefix Rules Route 0))
            (append Kept (prepend (get Route "bits") (take (get Route "prefix") (bgp.bits-size (get Route "bits"))))) Kept)))))

(define bgp.export
  State Peer ->
    (let Config (get State "config")
      (let Prefixes (bgp.export-at (get Config "routes") (get Peer "export") 0 [])
        (let Source (rt.local-address (rt.interface Config (get Peer "port")) 4)
          (if (or (= Prefixes []) (= Source [])) []
            (let Attrs (append [64 1 1 0 64 2 4 2 1] (append (rt.net-word (get Peer "local-as")) (append [64 3 4] Source)))
              [(bgp.wire 2 (append [0 0] (append (rt.net-word (len Attrs)) (append Attrs Prefixes))))]))))))

(define bgp.refresh
  Session Now -> (rt.put Session "deadline" (+ Now (get Session "hold"))))

(define bgp.message
  State Peer Session Frame Now ->
    (let Id (get Peer "id")
      (if (or (not (rt.array-bounds Frame 19 4096)) (not (rt.bytes-valid Frame 0 (len Frame))))
        (bgp.reject State Id "bad-length")
        (if (not (= (take Frame 16) (bgp.marker 0))) (bgp.reject State Id "bad-marker")
          (if (not (= (rt.byte-word Frame 16) (len Frame))) (bgp.reject State Id "bad-length")
            (let Type (at Frame 18)
              (if (not (rt.contains [1 2 3 4] Type)) (bgp.reject State Id "bad-type")
                (let Body (drop Frame 19)
                  (if (= Type 3) (bgp.close State Id "peer-notification" [])
                    (bgp.dispatch State Peer Session Type Body Now))))))))))

(define bgp.dispatch
  State Peer Session Type Body Now ->
    (let Id (get Peer "id")
      (let Phase (get Session "phase")
        (if (and (= Phase "open-sent") (= Type 1))
          (let Status (bgp.open-status Body Peer)
            (if (not (= Status "ok")) (bgp.reject State Id Status)
              (let Advertised (rt.byte-word Body 3)
                (let Hold (if (< Advertised (get Peer "hold")) Advertised (get Peer "hold"))
                  (let Open (obj [["phase" "open-confirm"] ["hold" Hold] ["next-send" (+ Now (rt.div Hold 3))]])
                    (bgp.result (bgp.session State Id (bgp.refresh Open Now)) "open-confirm" [(bgp.wire 4 [])] false))))))
          (if (and (= Type 4) (= Body []))
            (if (or (= Phase "open-confirm") (= Phase "established"))
              (bgp.result
                (bgp.session State Id (rt.put (bgp.refresh Session Now) "phase" "established"))
                "established" (if (= Phase "open-confirm") (bgp.export State Peer) []) false)
              (bgp.reject State Id "unexpected-message"))
            (if (and (= Type 2) (= Phase "established"))
              (bgp.update (bgp.session State Id (bgp.refresh Session Now)) Peer Body)
              (bgp.reject State Id "unexpected-message")))))))

(define bgp.event
  Arg ->
    (let State (get Arg "state")
      (let Now (get Arg "now")
        (let Id (get Arg "peer")
          (let Peer (bgp.peer-at (get (get State "config") "peers") Id 0)
            (if (= Peer null) (bgp.result State "unknown-peer" [] true)
              (if (not (rt.owns-lease State Now)) (bgp.close State Id "fenced" [])
                (let Event (get Arg "event")
                  (let Session (get (get State "sessions") Id)
                    (if (= Event "connected")
                      (let Waiting (obj [["phase" "open-sent"] ["hold" 30] ["deadline" (+ Now 30)] ["next-send" (+ Now 30)]])
                        (bgp.result (bgp.session
                          (bgp.routes State (bgp.without-at (get State "peer-routes") Id 0 [])) Id Waiting)
                          "open-sent" [(bgp.open Peer)] false))
                      (if (= Event "closed") (bgp.close State Id "closed" [])
                        (if (= Session null) (bgp.result State "peer-not-connected" [] true)
                          (if (and (> (get Session "hold") 0) (>= Now (get Session "deadline")))
                            (bgp.reject State Id "hold-timeout")
                            (if (= Event "record") (bgp.message State Peer Session (get Arg "bytes") Now)
                              (if (= Event "tick")
                                (if (and (not (= (get Session "phase") "open-sent"))
                                      (and (> (get Session "hold") 0) (>= Now (get Session "next-send"))))
                                  (bgp.result (bgp.session State Id (rt.put Session "next-send" (+ Now (rt.div (get Session "hold") 3))))
                                    "keepalive" [(bgp.wire 4 [])] false)
                                  (bgp.result State "peer-wait" [] false))
                                (bgp.result State "unknown-peer-event" [] false))))))))))))))))
