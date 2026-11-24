\\ The router consumes whole Ethernet frames. All offsets below are into
\\ byte vectors. An exclusive end lets each parser prove its own reads.

(define rt.byte-word
  Bytes I -> (+ (* 256 (at Bytes I)) (at Bytes (+ I 1))))

(define rt.div
  N D -> (floor-div (- N (floor-mod N D)) D))

(define rt.net-word
  N -> [(rt.div N 256) (floor-mod N 256)])

(define rt.patch
  Bytes At Replacement ->
    (append (rt.slice Bytes 0 At)
      (append Replacement (rt.slice Bytes (+ At (len Replacement)) (len Bytes)))))

(define rt.fold-word
  N -> (+ (floor-mod N 65536) (rt.div N 65536)))

(define rt.sum-words
  Bytes First End ->
    (if (= First End) 0
      (if (= (+ First 1) End)
        (let Offset (* First 2)
          (if (= (+ Offset 1) (len Bytes)) (* 256 (at Bytes Offset))
            (rt.byte-word Bytes Offset)))
        (let Mid (rt.div (+ First End) 2)
          (rt.fold-word (+ (rt.sum-words Bytes First Mid) (rt.sum-words Bytes Mid End)))))))

(define rt.checksum
  Bytes -> (- 65535 (rt.sum-words Bytes 0 (rt.div (+ (len Bytes) 1) 2))))

(define rt.pseudo
  Family Src Dst Protocol Length ->
    (append (append Src Dst)
      (if (= Family 4) (append [0 Protocol] (rt.net-word Length))
        (append [0 0] (append (rt.net-word Length) [0 0 0 Protocol])))))

(define rt.transport-checksum
  Family Src Dst Protocol Payload ->
    (rt.checksum (append (rt.pseudo Family Src Dst Protocol (len Payload)) Payload)))

(define rt.mac-allowed
  Mac -> (and (= (len Mac) 6)
              (and (= (floor-mod (at Mac 0) 2) 0) (not (= Mac [0 0 0 0 0 0])))))

(define rt.parse-ethernet
  Frame ->
    (if (> (len Frame) 9218) (obj [["reason" "frame-oversized"]])
      (if (< (len Frame) 14) (obj [["reason" "truncated-ethernet"]])
        (let Type (rt.byte-word Frame 12)
          (if (= Type 33024)
            (if (< (len Frame) 18) (obj [["reason" "truncated-vlan"]])
              (obj [["reason" "ok"] ["type" (rt.byte-word Frame 16)]
                ["vlan" (floor-mod (rt.byte-word Frame 14) 4096)] ["tagged" true]
                ["src" (rt.slice Frame 6 12)] ["dst" (rt.slice Frame 0 6)] ["offset" 18]]))
            (obj [["reason" "ok"] ["type" Type] ["vlan" 0] ["tagged" false]
              ["src" (rt.slice Frame 6 12)] ["dst" (rt.slice Frame 0 6)] ["offset" 14]]))))))

(define rt.parse-ipv4
  Net ->
    (if (< (len Net) 20) (obj [["reason" "truncated-ipv4"]])
      (if (not (= (rt.div (at Net 0) 16) 4)) (obj [["reason" "invalid-ipv4-version"]])
        (let HeaderLength (* 4 (floor-mod (at Net 0) 16))
          (let Total (rt.byte-word Net 2)
            (if (or (< HeaderLength 20) (< Total HeaderLength)) (obj [["reason" "invalid-ipv4-length"]])
              (if (> Total (len Net)) (obj [["reason" "truncated-ipv4"]])
                (if (not (= (rt.checksum (rt.slice Net 0 HeaderLength)) 0)) (obj [["reason" "ipv4-checksum"]])
                  (if (not (rt.contains [0 16384] (rt.byte-word Net 6))) (obj [["reason" "ipv4-fragment"]])
                    (if (> HeaderLength 20) (obj [["reason" "ipv4-options"]])
                      (obj [["reason" "ok"] ["family" 4] ["src" (rt.slice Net 12 16)] ["dst" (rt.slice Net 16 20)]
                        ["protocol" (at Net 9)] ["ttl" (at Net 8)] ["ttl-offset" 8] ["src-offset" 12]
                        ["dst-offset" 16] ["l4" HeaderLength] ["end" Total] ["bytes" (rt.slice Net 0 Total)]])))))))))))

(define rt.ipv6-extension
  Protocol ->
    (or (= Protocol 0)
      (or (= Protocol 43)
        (or (= Protocol 44)
          (or (= Protocol 50)
            (or (= Protocol 51)
              (or (= Protocol 60) (= Protocol 135))))))))

(define rt.parse-ipv6
  Net ->
    (if (< (len Net) 40) (obj [["reason" "truncated-ipv6"]])
      (if (not (= (rt.div (at Net 0) 16) 6)) (obj [["reason" "invalid-ipv6-version"]])
        (let Total (+ 40 (rt.byte-word Net 4))
          (let Protocol (at Net 6)
            (if (> Total (len Net)) (obj [["reason" "truncated-ipv6"]])
              (if (rt.ipv6-extension Protocol) (obj [["reason" "ipv6-extension"]])
                (obj [["reason" "ok"] ["family" 6] ["src" (rt.slice Net 8 24)] ["dst" (rt.slice Net 24 40)]
                  ["protocol" Protocol] ["ttl" (at Net 7)] ["ttl-offset" 7] ["src-offset" 8]
                  ["dst-offset" 24] ["l4" 40] ["end" Total] ["bytes" (rt.slice Net 0 Total)]]))))))))

(define rt.parse-ip
  2048 Net -> (rt.parse-ipv4 Net)
  34525 Net -> (rt.parse-ipv6 Net)
  _ _ -> (obj [["reason" "ether-type"]]))

(define rt.parse-transport
  Packet ->
    (let Protocol (get Packet "protocol")
      (let Segment (rt.slice (get Packet "bytes") (get Packet "l4") (get Packet "end"))
        (if (= Protocol 6)
          (if (< (len Segment) 20) (obj [["reason" "truncated-tcp"]])
            (let Header (* 4 (rt.div (at Segment 12) 16))
              (if (or (< Header 20) (> Header (len Segment))) (obj [["reason" "invalid-tcp-length"]])
                (rt.verify-transport Packet Segment (at Segment 13) 16))))
          (if (= Protocol 17)
            (if (< (len Segment) 8) (obj [["reason" "truncated-udp"]])
              (if (not (= (rt.byte-word Segment 4) (len Segment))) (obj [["reason" "invalid-udp-length"]])
                (rt.verify-transport Packet Segment 0 6)))
            (obj [["reason" "ok"]]))))))

(define rt.verify-transport
  Packet Segment Flags CheckOffset ->
    (let Family (get Packet "family")
      (let Protocol (get Packet "protocol")
        (let NoChecksum (and (= Protocol 17) (= (rt.byte-word Segment CheckOffset) 0))
          (if (if NoChecksum (not (= Family 4))
                (not (= (rt.transport-checksum Family (get Packet "src") (get Packet "dst") Protocol Segment) 0)))
            (obj [["reason" "transport-checksum"]])
            (obj [["reason" "ok"] ["source" (rt.byte-word Segment 0)] ["destination" (rt.byte-word Segment 2)]
              ["flags" Flags] ["check-offset" CheckOffset] ["no-checksum" NoChecksum] ["segment" Segment]]))))))

(define rt.ipv4-checksum
  Net ->
    (let Zeroed (rt.patch Net 10 [0 0])
      (rt.patch Zeroed 10 (rt.net-word (rt.checksum (rt.slice Zeroed 0 20))))))

(define rt.forward-ip
  Packet ->
    (let Net (rt.patch (get Packet "bytes") (get Packet "ttl-offset") [(- (get Packet "ttl") 1)])
      (if (= (get Packet "family") 4) (rt.ipv4-checksum Net) Net)))

(define rt.translate-ip
  Packet Transport Src Dst Sport Dport ->
    (let Net (rt.patch (rt.patch (rt.forward-ip Packet) (get Packet "src-offset") Src) (get Packet "dst-offset") Dst)
      (let Segment (rt.patch (get Transport "segment") 0 (append (rt.net-word Sport) (rt.net-word Dport)))
        (let CheckOffset (get Transport "check-offset")
          (let Zeroed (rt.patch Segment CheckOffset [0 0])
            (let Protocol (get Packet "protocol")
              (let Check (if (get Transport "no-checksum") 0
                             (rt.transport-checksum (get Packet "family") Src Dst Protocol Zeroed))
                (let ValidCheck (if (and (= Protocol 17) (and (= Check 0) (not (get Transport "no-checksum")))) 65535 Check)
                  (let Reassembled (rt.patch Net (get Packet "l4") (rt.patch Zeroed CheckOffset (rt.net-word ValidCheck)))
                    (if (= (get Packet "family") 4) (rt.ipv4-checksum Reassembled) Reassembled))))))))))

(define rt.l2
  Port Dst Type Payload ->
    (append Dst
      (append (get Port "mac")
        (append (if (= (get Port "vlan") 0) []
                  (append [129 0] (rt.net-word (get Port "vlan"))))
          (append (rt.net-word Type) Payload)))))

(define rt.prefix-matches-at
  Address Prefix Bits I ->
    (if (= Bits 0) true
      (if (>= Bits 8)
        (and (= (at Address I) (at Prefix I)) (rt.prefix-matches-at Address Prefix (- Bits 8) (+ I 1)))
        (let Divisor (at [256 128 64 32 16 8 4 2] Bits)
          (= (rt.div (at Address I) Divisor) (rt.div (at Prefix I) Divisor))))))

(define rt.prefix-matches
  Address Prefix Bits ->
    (and (= (len Address) (len Prefix)) (rt.prefix-matches-at Address Prefix Bits 0)))

(define rt.interface-at
  Interfaces Id I ->
    (if (= I (len Interfaces)) null
      (if (= (get (at Interfaces I) "id") Id) (at Interfaces I)
        (rt.interface-at Interfaces Id (+ I 1)))))

(define rt.interface
  Config Id -> (rt.interface-at (get Config "interfaces") Id 0))

(define rt.local-address-at
  Addresses Size I ->
    (if (= I (len Addresses)) []
      (if (= (len (at Addresses I)) Size) (at Addresses I)
        (rt.local-address-at Addresses Size (+ I 1)))))

(define rt.local-address
  Interface Family -> (rt.local-address-at (get Interface "addresses") (if (= Family 4) 4 16) 0))

(define rt.contains
  Values Value -> (contains Values Value))

(define rt.policy-at
  Policy Packet I ->
    (if (= I (len Policy)) true
      (let Rule (at Policy I)
        (if (and (or (= (get Rule "protocol") 0) (= (get Rule "protocol") (get Packet "protocol")))
              (and (rt.prefix-matches (get Packet "src") (get Rule "src") (get Rule "src-bits"))
                (rt.prefix-matches (get Packet "dst") (get Rule "dst") (get Rule "dst-bits"))))
          (get Rule "allow") (rt.policy-at Policy Packet (+ I 1))))))

(define rt.route-better
  Route Best ->
    (or (= Best null)
      (or (> (get Route "bits") (get Best "bits"))
        (and (= (get Route "bits") (get Best "bits")) (< (get Route "metric") (get Best "metric"))))))

(define rt.best-route-at
  Config Routes Tenant Address I Best ->
    (if (= I (len Routes)) Best
      (let Route (at Routes I)
        (rt.best-route-at Config Routes Tenant Address (+ I 1)
          (if (and (= Tenant (get (rt.interface Config (get Route "out")) "tenant"))
                (and (rt.prefix-matches Address (get Route "prefix") (get Route "bits")) (rt.route-better Route Best)))
            Route Best)))))

(define rt.best-route
  State Tenant Addr ->
    (let Config (get State "config")
      (rt.best-route-at Config (get State "peer-routes") Tenant Addr 0
        (rt.best-route-at Config (get Config "routes") Tenant Addr 0 null))))

(define rt.neighbor-at
  Neighbors Port Addr Now I ->
    (if (= I (len Neighbors)) null
      (let N (at Neighbors I)
        (if (and (= (get N "port") Port) (and (= (get N "address") Addr) (> (get N "until") Now)))
          N (rt.neighbor-at Neighbors Port Addr Now (+ I 1))))))

(define rt.neighbor
  State Port Addr Now ->
    (let Static (rt.neighbor-at (get (get State "config") "neighbors") Port Addr Now 0)
      (if (= Static null) (rt.neighbor-at (get State "neighbors") Port Addr Now 0) Static)))

(define rt.send
  Port Mac Family Network ->
    (obj [["port" (get Port "id")] ["frame" (rt.l2 Port Mac (if (= Family 4) 2048 34525) Network)]]))

(define rt.count
  State Reason ->
    (let Counters (get State "counters")
      (rt.put State "counters" (rt.put Counters Reason (+ 1 (rt.get-or Counters Reason 0))))))

(define rt.result
  State Reason Frames ->
    (obj [["state" (rt.count State Reason)] ["reason" Reason] ["tx" Frames] ["generation" (get State "generation")]]))

(define rt.ipv4-message
  Src Dst Protocol Ttl Body ->
    (rt.ipv4-checksum
      (append [69 0]
        (append (rt.net-word (+ 20 (len Body)))
          (append [0 0 64 0 Ttl Protocol 0 0]
            (append Src (append Dst Body)))))))

(define rt.ipv6-message
  Src Dst Protocol Ttl Body ->
    (append [96 0 0 0]
      (append (rt.net-word (len Body))
        (append [Protocol Ttl] (append Src (append Dst Body))))))

(define rt.ip-message
  Family Src Dst Protocol Ttl Body ->
    (if (= Family 4) (rt.ipv4-message Src Dst Protocol Ttl Body) (rt.ipv6-message Src Dst Protocol Ttl Body)))

(define rt.control-checksum
  Family Src Dst Message ->
    (rt.patch Message 2 (rt.net-word (if (= Family 4) (rt.checksum Message)
      (rt.transport-checksum 6 Src Dst 58 Message)))))

(define rt.is-icmp-error
  Packet Family ->
    (let Protocol (get Packet "protocol")
      (let Offset (get Packet "l4")
        (if (and (< Offset (get Packet "end")) (= Protocol (if (= Family 4) 1 58)))
          (let Type (at (get Packet "bytes") Offset)
            (if (= Family 4) (rt.contains [3 4 5 11 12] Type) (< Type 128)))
          false))))

(define rt.unicast-ip
  Family Address ->
    (if (= Family 4)
      (and (> (at Address 0) 0) (< (at Address 0) 224))
      (and (not (= (at Address 0) 255)) (not (= Address [0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0])))))

(define rt.error-message
  State Packet InPort SourceMac Reason Mtu Now ->
    (let Family (get Packet "family")
      (let Source (rt.local-address InPort Family)
        (let Destination (get Packet "src")
          (if (or (= Source []) (or (rt.is-icmp-error Packet Family)
                (or (not (rt.unicast-ip Family Destination))
                  (or (not (rt.unicast-ip Family (get Packet "dst"))) (not (rt.mac-allowed SourceMac))))))
            (rt.result State Reason [])
            (let Kind (if (= Family 4)
                         (if (= Reason "hop-limit") 11 3)
                         (if (= Reason "hop-limit") 3 (if (= Reason "mtu") 2 1)))
              (let Code (if (= Reason "mtu") (if (= Family 4) 4 0) 0)
                (let Body (append [Kind Code 0 0 0 0]
                           (append (rt.net-word Mtu)
                             (rt.slice (get Packet "bytes") 0
                               (if (> (get Packet "end") (if (= Family 4) 28 1232))
                                 (if (= Family 4) 28 1232) (get Packet "end")))))
                  (rt.control-frames State Reason Now
                    [(rt.send InPort SourceMac Family
                      (rt.ip-message Family Source Destination (if (= Family 4) 1 58) 64
                        (rt.control-checksum Family Source Destination Body)))] [])))))))))

(define rt.arp-body
  Operation SourceMac SourceIp TargetMac TargetIp ->
    (append [0 1 8 0 6 4 0 Operation]
      (append SourceMac (append SourceIp (append TargetMac TargetIp)))))

(define rt.solicited-address
  Address -> (append [255 2 0 0 0 0 0 0 0 0 0 1 255] (rt.slice Address 13 16)))

(define rt.ndp-message
  Source Destination Target Mac Kind Flags ->
    (let Body (append [Kind 0 0 0 Flags 0 0 0]
                 (append Target (append [(if (= Kind 135) 1 2) 1] Mac)))
      (rt.ipv6-message Source Destination 58 255 (rt.control-checksum 6 Source Destination Body))))

(define rt.neighbor-query
  Port Family Address ->
    (let Source (rt.local-address Port Family)
      (if (= Source []) []
        [(if (= Family 4)
          (obj [["port" (get Port "id")] ["frame"
            (rt.l2 Port [255 255 255 255 255 255] 2054
              (rt.arp-body 1 (get Port "mac") Source [0 0 0 0 0 0] Address))]])
          (rt.send Port (append [51 51 255] (rt.slice Address 13 16)) 6
            (rt.ndp-message Source (rt.solicited-address Address) Address (get Port "mac") 135 0)))])))

(define rt.active-records-at
  Records Now I Kept ->
    (if (= I (len Records)) Kept
      (let Record (at Records I)
        (rt.active-records-at Records Now (+ I 1)
          (if (> (get Record "until") Now) (append Kept [Record]) Kept)))))

(define rt.active-records
  Records Now -> (rt.active-records-at Records Now 0 []))

(define rt.route-wire
  State Packet InPort SourceMac Destination Network Now ->
    (let Route (rt.best-route State (get InPort "tenant") Destination)
      (if (= Route null) (rt.error-message State Packet InPort SourceMac "no-route" 0 Now)
        (let Port (rt.interface (get State "config") (get Route "out"))
          (if (or (= Port null) (not (get Port "forward"))) (rt.result State "egress-disabled" [])
            (if (> (len Network) (get Port "mtu")) (rt.error-message State Packet InPort SourceMac "mtu" (get Port "mtu") Now)
              (let Address (if (= (get Route "via") []) Destination (get Route "via"))
                (let Neighbor (rt.neighbor State (get Port "id") Address Now)
                  (if (= Neighbor null)
                    (rt.defer-frame State Port Address (get Packet "family") Network Now)
                    (rt.result State "forwarded" [(rt.send Port (get Neighbor "mac") (get Packet "family") Network)]))))))))))

(define rt.defer-frame
  State Port Address Family Network Now ->
    (let Pending (rt.active-records (get State "pending") Now)
      (if (>= (len Pending) (get (get State "config") "pending-limit"))
        (rt.result (rt.put State "pending" Pending) "neighbor-queue-full" [])
        (let Next (append Pending [(obj [["port" (get Port "id")] ["address" Address]
                         ["family" Family] ["network" Network] ["until" (+ Now 3)]])])
          (rt.control-frames (rt.put State "pending" Next) "neighbor-pending" Now
            (if (= (rt.neighbor-at Pending (get Port "id") Address Now 0) null)
              (rt.neighbor-query Port Family Address) []) [])))))

(define rt.release-neighbor-at
  Pending I Port Address Mac Now Remain Frames ->
    (if (= I (len Pending)) (obj [["pending" Remain] ["frames" Frames]])
      (let N (at Pending I)
        (if (<= (get N "until") Now)
          (rt.release-neighbor-at Pending (+ I 1) Port Address Mac Now Remain Frames)
          (if (and (= (get N "address") Address) (= (get N "port") (get Port "id")))
            (rt.release-neighbor-at Pending (+ I 1) Port Address Mac Now Remain
              (append Frames [(rt.send Port Mac (get N "family") (get N "network"))]))
            (rt.release-neighbor-at Pending (+ I 1) Port Address Mac Now (append Remain [N]) Frames))))))

(define rt.drop-neighbor-at
  Neighbors I Port Address Now Kept ->
    (if (= I (len Neighbors)) Kept
      (let N (at Neighbors I)
        (rt.drop-neighbor-at Neighbors (+ I 1) Port Address Now
          (if (or (<= (get N "until") Now) (and (= (get N "address") Address) (= (get N "port") Port)))
            Kept (append Kept [N]))))))

(define rt.learn-neighbor
  State Port Address Mac Now Reply ->
    (let Kept (rt.drop-neighbor-at (get State "neighbors") 0 (get Port "id") Address Now [])
      (let Released (rt.release-neighbor-at (get State "pending") 0 Port Address Mac Now [] [])
        (let Limit (get (get State "config") "neighbor-limit")
          (let Capacity (if (>= (len Kept) Limit) (rt.slice Kept 1 (len Kept)) Kept)
            (let Neighbor (obj [["port" (get Port "id")] ["address" Address] ["mac" Mac] ["until" (+ Now 60)]])
              (rt.control-frames (rt.put (rt.put State "pending" (get Released "pending")) "neighbors" (append Capacity [Neighbor]))
                "neighbor-learned" Now Reply (get Released "frames"))))))))

(define rt.arp-input
  State Port Ether Net Now ->
    (if (< (len Net) 28) (rt.result State "truncated-arp" [])
      (let Operation (rt.byte-word Net 6)
        (let SrcMac (rt.slice Net 8 14)
          (let Src (rt.slice Net 14 18)
            (let Dst (rt.slice Net 24 28)
              (if (or (not (= (rt.slice Net 0 6) [0 1 8 0 6 4]))
                    (or (not (rt.contains [1 2] Operation))
                      (or (not (rt.mac-allowed SrcMac)) (not (= SrcMac (get Ether "src"))))))
                (rt.result State "invalid-arp" [])
                (if (not (rt.local-or-alias State Port Dst)) (rt.result State "arp-not-local" [])
                  (if (and (= Operation 2) (not (= (rt.slice Net 18 24) (get Port "mac"))))
                    (rt.result State "invalid-arp-target" [])
                    (if (not (rt.unicast-ip 4 Src)) (rt.result State "arp-nonunicast-sender" [])
                      (rt.learn-neighbor State Port Src SrcMac Now
                        (if (= Operation 1)
                          [(obj [["port" (get Port "id")] ["frame"
                            (rt.l2 Port SrcMac 2054 (rt.arp-body 2 (get Port "mac") Dst SrcMac Src))]])]
                          []))))))))))))

(define rt.ndp-input
  State Port Ether Packet Now ->
    (let Net (get Packet "bytes")
      (let Src (get Packet "src")
        (let Destination (get Packet "dst")
          (if (not (= (get Packet "ttl") 255)) (rt.result State "ndp-hop-limit" [])
            (if (not (= (get Packet "end") 72)) (rt.result State "ndp-options-unsupported" [])
              (let Kind (at Net 40)
                (let Target (rt.slice Net 48 64)
                  (let Mac (rt.slice Net 66 72)
                    (if (or (not (= (at Net 41) 0))
                          (or (not (= (rt.transport-checksum 6 Src Destination 58 (rt.slice Net 40 72)) 0))
                            (or (not (= (rt.slice Net 64 66) [(if (= Kind 135) 1 2) 1]))
                              (or (not (= Mac (get Ether "src"))) (not (rt.mac-allowed Mac))))))
                      (rt.result State "invalid-ndp" [])
                      (if (or (not (rt.unicast-ip 6 Src)) (not (rt.unicast-ip 6 Target)))
                        (rt.result State "ndp-nonunicast-address" [])
                        (if (= Kind 135)
                          (if (and (rt.local-or-alias State Port Target)
                                (or (= Destination Target) (= Destination (rt.solicited-address Target))))
                            (rt.learn-neighbor State Port Src Mac Now
                              [(rt.send Port Mac 6 (rt.ndp-message Target Src Target (get Port "mac") 136 224))])
                            (rt.result State "ndp-not-local" []))
                          (if (rt.local-or-alias State Port Destination)
                            (rt.learn-neighbor State Port Target Mac Now [])
                            (rt.result State "ndp-not-local" []))))))))))))))

(define rt.slice
  Bytes Start End -> (take (drop Bytes Start) (- End Start)))

(define rt.put
  Object Key Value -> (merge Object (obj [[Key Value]])))

(define rt.get-or
  Object Key Default ->
    (let Value (get Object Key)
      (if (= Value null) Default Value)))

(define rt.flow-key
  Tenant Protocol Src Sport Dst Dport -> [Tenant Protocol Src Sport Dst Dport])

(define rt.flow-at
  Flows Key Direction I Now ->
    (if (= I (len Flows)) null
      (let Flow (at Flows I)
        (if (and (> (get Flow "until") Now) (= (get Flow Direction) Key))
          I (rt.flow-at Flows Key Direction (+ I 1) Now)))))

(define rt.listener-at
  Listeners Key I ->
    (if (= I (len Listeners)) null
      (let Listener (at Listeners I)
        (if (= [(get Listener "tenant") (get Listener "protocol") (get Listener "address") (get Listener "port")] Key)
          Listener (rt.listener-at Listeners Key (+ I 1))))))

(define rt.translation-at
  Listeners Address I ->
    (if (= I (len Listeners)) false
      (or (= (get (at Listeners I) "translation") Address)
        (rt.translation-at Listeners Address (+ I 1)))))

(define rt.choose-index
  "reverse" Serial Tried Count -> (floor-mod (+ (- Count 1) (- Serial Tried)) Count)
  _ Serial Tried Count -> (floor-mod (+ Serial Tried) Count))

(define rt.choose-backend-at
  Rule Backends Serial Tried ->
    (if (= Tried (len Backends)) null
      (let Backend (at Backends (rt.choose-index Rule Serial Tried (len Backends)))
        (if (and (get Backend "healthy") (not (get Backend "draining"))) Backend
          (rt.choose-backend-at Rule Backends Serial (+ Tried 1))))))

(define rt.port-owned-at
  Flows Address Protocol Port I ->
    (if (= I (len Flows)) false
      (let Flow (at Flows I)
        (if (and (= (get Flow "protocol") Protocol)
              (and (= (get Flow "translation") Address) (= (get Flow "allocated") Port)))
          true (rt.port-owned-at Flows Address Protocol Port (+ I 1))))))

(define rt.owned-ports
  Flows Address Protocol I Ports ->
    (if (= I (len Flows)) Ports
      (let Flow (at Flows I)
        (rt.owned-ports Flows Address Protocol (+ I 1)
          (if (and (= (get Flow "protocol") Protocol) (= (get Flow "translation") Address))
            (prepend (get Flow "allocated") Ports) Ports)))))

(define rt.allocate-at
  Owned Low High ->
    (if (> Low High) 0
      (if (rt.contains Owned Low) (rt.allocate-at Owned (+ Low 1) High) Low)))

(define rt.tenant-count-at
  Flows Tenant I Count ->
    (if (= I (len Flows)) Count
      (rt.tenant-count-at Flows Tenant (+ I 1)
        (+ Count (if (= (get (at Flows I) "tenant") Tenant) 1 0)))))

(define rt.syn
  Flags -> (= (floor-mod Flags 64) 2))

(define rt.lifetime
  Protocol -> (if (= Protocol 6) 300 30))

(define rt.translate-flow
  State Packet Transport InPort SourceMac Flow Reverse Now ->
    (let Src (if Reverse (get Flow "listener") (get Flow "translation"))
      (let Dst (if Reverse (get Flow "client") (get Flow "backend"))
        (let Sport (if Reverse (get Flow "listener-port") (get Flow "allocated"))
          (let Dport (if Reverse (get Flow "client-port") (get Flow "backend-port"))
            (rt.route-wire State Packet InPort SourceMac Dst
              (rt.translate-ip Packet Transport Src Dst Sport Dport) Now))))))

(define rt.remembered-flow
  Defer State Packet Transport InPort SourceMac Position Reverse Now ->
    (let Flows (get State "flows")
      (let Flow (at Flows Position)
        (if (not (= (get State "mode") "single"))
          (rt.translate-flow State Packet Transport InPort SourceMac Flow Reverse Now)
          (let Until (+ Now (rt.lifetime (get Packet "protocol")))
            (let Refreshed (rt.put Flow "until" Until)
              (if (and Defer (> Until (rt.get-or Flow "reserved-until" (get Flow "until"))))
                (let Reserved (rt.put Refreshed "reserved-until" (+ Until 30))
                  (rt.resume-plan (rt.put State "flows" (rt.patch Flows Position [Reserved]))
                    Packet Transport InPort SourceMac Reserved Reverse))
                (rt.translate-flow (rt.put State "flows" (rt.patch Flows Position [Refreshed]))
                  Packet Transport InPort SourceMac Refreshed Reverse Now))))))))

(define rt.add-flow
  Defer State Packet Transport InPort SourceMac Listener Backend Key Flows Allocated Now ->
    (let Protocol (get Packet "protocol")
      (let Flow (obj [["fwd" Key]
                       ["rev" (rt.flow-key (get InPort "tenant") Protocol (get Backend "address") (get Backend "port")
                               (get Listener "translation") Allocated)]
                       ["tenant" (get InPort "tenant")] ["protocol" Protocol]
                       ["front" (get Listener "front")] ["back" (get Listener "back")]
                       ["client" (get Packet "src")] ["client-port" (get Transport "source")]
                       ["listener" (get Packet "dst")] ["listener-port" (get Transport "destination")]
                       ["backend" (get Backend "address")] ["backend-port" (get Backend "port")]
                       ["translation" (get Listener "translation")] ["allocated" Allocated]
                       ["until" (+ Now (rt.lifetime Protocol))]])
        (let Serial (+ 1 (get State "flow-serial"))
          (if (= (get State "mode") "single")
            (let Chosen (if Defer (rt.put Flow "reserved-until" (+ (get Flow "until") 30)) Flow)
              (let Bound (rt.put (rt.put State "flows" (append Flows [Chosen])) "flow-serial" Serial)
                (if Defer
                  (rt.resume-plan Bound Packet Transport InPort SourceMac Chosen false)
                  (rt.translate-flow Bound Packet Transport InPort SourceMac Chosen false Now))))
            (let Offer (obj [["id" [(get State "epoch") Serial]] ["epoch" (get State "epoch")]
                              ["generation" (get State "generation")] ["configuration" (digest (get State "config"))]
                              ["flow" Flow]])
              (let Pending (obj [["offer" Offer] ["packet" Packet] ["transport" Transport]
                                   ["port" (get InPort "id")] ["source-mac" SourceMac]])
                (let Result (rt.put (rt.result (merge State (obj [["offer" Pending] ["flow-serial" Serial] ["flows" Flows]]))
                                "replica-required" []) "replicate" Offer)
                  (if Defer (rt.put Result "durable" true) Result)))))))))

(define rt.new-flow
  Defer Selection State Packet Transport InPort SourceMac Listener Key Now ->
    (if (and (= (get Packet "protocol") 6) (not (rt.syn (get Transport "flags"))))
      (rt.result State "unbound-tcp" [])
      (if (rt.offer-live State Now)
        (let Offer (get (get State "offer") "offer")
          (if (= Key (get (get Offer "flow") "fwd"))
            (rt.put (rt.result State "replica-required" []) "replicate" Offer)
            (rt.result State "replica-in-flight" [])))
        (let Flows (rt.active-records (get State "flows") Now)
          (let Config (get State "config")
            (if (or (>= (len Flows) (get Config "flow-limit"))
                  (>= (rt.tenant-count-at Flows (get InPort "tenant") 0 0) (get Config "tenant-limit")))
              (rt.result State "flow-limit" [])
              (let Backend (rt.choose-backend-at Selection (get Listener "backends") (get State "flow-serial") 0)
                (if (= Backend null) (rt.result State "backend-unavailable" [])
                  (let Port (rt.allocate-at (rt.owned-ports Flows (get Listener "translation") (get Packet "protocol") 0 [])
                               (get Config "port-low") (get Config "port-high"))
                    (if (= Port 0) (rt.result State "port-exhausted" [])
                      (rt.add-flow Defer State Packet Transport InPort SourceMac Listener Backend Key Flows Port Now)))))))))))

(define rt.transport-route
  Defer Selection State Packet Transport InPort SourceMac Now ->
    (let Destination (get Packet "dst")
      (let Key (rt.flow-key (get InPort "tenant") (get Packet "protocol")
                 (get Packet "src") (get Transport "source") Destination (get Transport "destination"))
        (let Flows (get State "flows")
          (let Existing (rt.flow-at Flows Key "fwd" 0 Now)
            (if (not (= Existing null))
              (rt.remembered-flow Defer State Packet Transport InPort SourceMac Existing false Now)
              (let Reverse (rt.flow-at Flows Key "rev" 0 Now)
                (if (not (= Reverse null))
                  (rt.remembered-flow Defer State Packet Transport InPort SourceMac Reverse true Now)
                  (let Listeners (get (get State "config") "listeners")
                    (let Listener (rt.listener-at Listeners
                                     [(get InPort "tenant") (get Packet "protocol") Destination (get Transport "destination")] 0)
                      (if (or (= Listener null) (not (= (get Listener "front") (get InPort "id"))))
                        (if (rt.translation-at Listeners Destination 0) (rt.result State "unbound-translation" [])
                          (rt.route-wire State Packet InPort SourceMac Destination (rt.forward-ip Packet) Now))
                        (rt.new-flow Defer Selection State Packet Transport InPort SourceMac Listener Key Now))))))))))))

(define rt.is-ndp
  Packet ->
    (and (= (get Packet "family") 6)
      (and (= (get Packet "protocol") 58)
        (and (> (get Packet "end") 40) (rt.contains [135 136] (at (get Packet "bytes") 40))))))

(define rt.forward-frame
  Defer Selection State Interface SourceMac Packet Now ->
    (let Protocol (get Packet "protocol")
      (if (not (rt.forwarding-policy Interface Packet))
        (rt.result State "ingress-policy" [])
        (if (or (not (rt.unicast-ip (get Packet "family") (get Packet "src")))
              (not (rt.unicast-ip (get Packet "family") (get Packet "dst"))))
          (rt.result State "nonunicast-ip" [])
          (if (<= (get Packet "ttl") 1)
            (rt.error-message State Packet Interface SourceMac "hop-limit" 0 Now)
            (if (or (= Protocol 6) (= Protocol 17))
              (let Transport (rt.parse-transport Packet)
                (if (= (get Transport "reason") "ok")
                  (rt.transport-route Defer Selection State Packet Transport Interface SourceMac Now)
                  (rt.result State (get Transport "reason") [])))
              (if (and (rt.is-icmp-error Packet (get Packet "family"))
                    (rt.translation-at (get (get State "config") "listeners") (get Packet "dst") 0))
                (rt.result State "translated-icmp-unsupported" [])
                (rt.route-wire State Packet Interface SourceMac (get Packet "dst") (rt.forward-ip Packet) Now))))))))

(define rt.owns-lease
  State Now ->
    (or (= (get State "mode") "single")
      (and (= (get State "mode") "active") (> (get State "lease-until") Now))))

(define rt.ingress-addressed
  Interface Ethernet ->
    (or (= (get Interface "mac") (get Ethernet "dst"))
      (and (= (get Ethernet "type") 2054) (= (get Ethernet "dst") [255 255 255 255 255 255]))))

(define rt.ingress
  Defer Selection State Interface Frame Now ->
    (let Ether (rt.parse-ethernet Frame)
      (if (not (= (get Ether "reason") "ok")) (rt.result State (get Ether "reason") [])
        (if (or (not (= (get Interface "vlan") (get Ether "vlan")))
              (not (= (get Ether "tagged") (not (= (get Interface "vlan") 0)))))
          (rt.result State "ingress-vlan" [])
          (if (not (rt.mac-allowed (get Ether "src"))) (rt.result State "ingress-source-mac" [])
            (let Net (rt.slice Frame (get Ether "offset") (len Frame))
              (if (= (get Ether "type") 2054)
                (if (rt.ingress-addressed Interface Ether) (rt.arp-input State Interface Ether Net Now)
                  (rt.result State "ingress-destination-mac" []))
                (let Packet (rt.parse-ip (get Ether "type") Net)
                  (if (not (= (get Packet "reason") "ok")) (rt.result State (get Packet "reason") [])
                    (if (rt.is-ndp Packet) (rt.ndp-input State Interface Ether Packet Now)
                      (if (rt.ingress-addressed Interface Ether)
                        (if (rt.contains (get Interface "addresses") (get Packet "dst"))
                          (rt.result State "local-ip-unsupported" [])
                          (rt.forward-frame Defer Selection State Interface (get Ether "src") Packet Now))
                        (rt.result State "ingress-destination-mac" []))))))))))))

(define rt.frame
  Selection Defer Arg ->
    (let State (get Arg "state")
      (let Now (get Arg "now")
        (if (not (rt.owns-lease State Now)) (rt.result State "fenced" [])
          (let Interface (rt.interface (get State "config") (get Arg "port"))
            (if (or (= Interface null) (not (get Interface "forward"))) (rt.result State "ingress-disabled" [])
              (let Frame (get Arg "frame")
                (if (not (list? Frame)) (rt.result State "invalid-frame" [])
                  (if (> (len Frame) 9218) (rt.result State "frame-oversized" [])
                    (if (not (rt.bytes-valid Frame 0 (len Frame))) (rt.result State "invalid-frame" [])
                      (rt.ingress Defer Selection State Interface Frame Now)))))))))))

(define rt.bytes-valid
  Bytes First End ->
    (if (= First End) true
      (if (= (+ First 1) End)
        (let Byte (at Bytes First) (and (int? Byte) (and (>= Byte 0) (<= Byte 255))))
        (let Mid (rt.div (+ First End) 2)
          (and (rt.bytes-valid Bytes First Mid) (rt.bytes-valid Bytes Mid End))))))

(define rt.batch
  Arg ->
    (let Frames (get Arg "frames")
      (if (or (not (list? Frames)) (> (len Frames) 32))
        (rt.result (get Arg "state") "invalid-batch" [])
        (rt.batch-at (get Arg "state") Frames 0 [] [] null))))

(define rt.batch-at
  State Frames I FramesOut Reasons Offer ->
    (if (= I (len Frames))
      (let Result (obj [["state" State] ["tx" FramesOut] ["reasons" Reasons] ["generation" (get State "generation")]])
        (if (= Offer null) Result (rt.put Result "replicate" Offer)))
      (let Output (rt.packet-entry (rt.put (at Frames I) "state" State))
        (rt.batch-at (get Output "state") Frames (+ I 1)
          (append FramesOut (get Output "tx")) (append Reasons [(get Output "reason")])
          (rt.get-or Output "replicate" Offer)))))

(define rt.range
  Value Low High -> (and (int? Value) (and (>= Value Low) (<= Value High))))

(define rt.array-bounds
  Value Low High -> (and (list? Value) (rt.range (len Value) Low High)))

(define rt.address-valid
  Address ->
    (and (list? Address)
      (and (or (= (len Address) 4) (= (len Address) 16))
        (rt.bytes-valid Address 0 (len Address)))))

(define rt.addresses-valid
  Addresses I ->
    (if (= I (len Addresses)) true
      (and (rt.address-valid (at Addresses I)) (rt.addresses-valid Addresses (+ I 1)))))

(define rt.prefix-valid
  Address Bits -> (and (rt.address-valid Address) (rt.range Bits 0 (* 8 (len Address)))))

(define rt.record-valid
  "interface" Config Port ->
    (and (text? (get Port "id") 1 32)
      (and (text? (get Port "tenant") 1 32)
        (and (and (rt.array-bounds (get Port "mac") 6 6) (rt.bytes-valid (get Port "mac") 0 6))
          (and (rt.mac-allowed (get Port "mac"))
            (and (and (rt.array-bounds (get Port "addresses") 1 8)
                      (rt.addresses-valid (get Port "addresses") 0))
              (and (rt.range (get Port "vlan") 0 4094)
                (and (rt.range (get Port "mtu") (if (= (rt.local-address Port 6) []) 68 1280) 9200)
                  (and (bool? (get Port "forward"))
                    (and (rt.array-bounds (get Port "policy") 0 32)
                      (rt.records-valid "policy" Config (get Port "policy") 0))))))))))
  "policy" _ Policy ->
    (and (rt.prefix-valid (get Policy "src") (get Policy "src-bits"))
      (and (rt.prefix-valid (get Policy "dst") (get Policy "dst-bits"))
        (and (= (len (get Policy "src")) (len (get Policy "dst")))
          (and (rt.range (get Policy "protocol") 0 255) (bool? (get Policy "allow"))))))
  "route" Config Route ->
    (and (rt.prefix-valid (get Route "prefix") (get Route "bits"))
      (and (rt.range (get Route "metric") 0 65535)
        (and (or (= (get Route "via") [])
                  (and (rt.address-valid (get Route "via")) (= (len (get Route "via")) (len (get Route "prefix")))))
          (let Port (rt.interface Config (get Route "out"))
            (and (not (= Port null))
              (not (= (rt.local-address Port (if (= (len (get Route "prefix")) 4) 4 6)) [])))))))
  "neighbor" Config Neighbor ->
    (and (not (= (rt.interface Config (get Neighbor "port")) null))
      (and (rt.address-valid (get Neighbor "address"))
        (and (rt.range (get Neighbor "until") 1 9223372036854775807)
          (and (and (rt.array-bounds (get Neighbor "mac") 6 6)
                    (rt.bytes-valid (get Neighbor "mac") 0 6)) (rt.mac-allowed (get Neighbor "mac"))))))
  "listener" Config Listener ->
    (and (rt.address-valid (get Listener "address"))
      (and (rt.address-valid (get Listener "translation"))
        (and (= (len (get Listener "address")) (len (get Listener "translation")))
          (and (not (= (get Listener "address") (get Listener "translation")))
            (and (rt.contains [6 17] (get Listener "protocol"))
              (and (rt.range (get Listener "port") 1 65535)
                (and (text? (get Listener "tenant") 1 32)
                  (and (rt.array-bounds (get Listener "backends") 1 16)
                    (and (rt.records-valid "backend" Config (get Listener "backends") 0)
                      (and (rt.unique "backend" (get Listener "backends") 0 [])
                        (and (rt.backend-family (get Listener "backends") (len (get Listener "address")) 0)
                          (rt.listener-ports Config Listener))))))))))))
  "backend" _ Backend ->
    (and (text? (get Backend "id") 1 32)
      (and (rt.address-valid (get Backend "address"))
        (and (rt.range (get Backend "port") 1 65535)
          (and (and (bool? (get Backend "healthy")) (bool? (get Backend "draining")))
            (or (= (get Backend "probe-port") null) (rt.range (get Backend "probe-port") 1 65535))))))
  "peer" Config Peer ->
    (and (text? (get Peer "id") 1 32)
      (and (not (= (rt.interface Config (get Peer "port")) null))
        (and (rt.array-bounds (get Peer "address") 4 4)
          (and (rt.address-valid (get Peer "address"))
            (and (rt.range (get Peer "local-as") 1 65534)
              (and (rt.range (get Peer "remote-as") 1 65534)
                (and (not (= (get Peer "local-as") (get Peer "remote-as")))
                  (and (rt.range (get Peer "hold") 3 65535)
                    (and (and (rt.array-bounds (get Peer "router-id") 4 4) (rt.address-valid (get Peer "router-id")))
                      (and (and (rt.array-bounds (get Peer "import") 0 32)
                                (rt.records-valid "peer-prefix" Config (get Peer "import") 0))
                        (and (rt.array-bounds (get Peer "export") 0 32)
                          (rt.records-valid "peer-prefix" Config (get Peer "export") 0))))))))))))
  "peer-prefix" _ Prefix ->
    (and (rt.array-bounds (get Prefix "prefix") 4 4)
      (rt.prefix-valid (get Prefix "prefix") (get Prefix "bits"))))

(define rt.records-valid
  Kind Config Rows I ->
    (if (= I (len Rows)) true
      (and (and (obj? (at Rows I)) (rt.record-valid Kind Config (at Rows I)))
        (rt.records-valid Kind Config Rows (+ I 1)))))

(define rt.backend-family
  Rows Size I ->
    (if (= I (len Rows)) true
      (and (= (len (get (at Rows I) "address")) Size) (rt.backend-family Rows Size (+ I 1)))))

(define rt.unique-key
  "neighbor" Row -> [(get Row "port") (get Row "address")]
  "listener" Row -> [(get Row "tenant") (get Row "protocol") (get Row "address") (get Row "port")]
  _ Row -> (get Row "id"))

(define rt.unique
  Kind Rows I Seen ->
    (if (= I (len Rows)) true
      (let Key (rt.unique-key Kind (at Rows I))
        (and (not (rt.contains Seen Key)) (rt.unique Kind Rows (+ I 1) (prepend Key Seen))))))

(define rt.config-valid
  Config ->
    (and (obj? Config)
      (and (= (get Config "schema") 1)
        (and (rt.array-bounds (get Config "interfaces") 2 16)
          (and (rt.records-valid "interface" Config (get Config "interfaces") 0)
            (and (rt.unique "interface" (get Config "interfaces") 0 [])
              (and (and (rt.array-bounds (get Config "routes") 0 64)
                        (rt.records-valid "route" Config (get Config "routes") 0))
                (and (and (rt.array-bounds (get Config "neighbors") 0 64)
                          (and (rt.records-valid "neighbor" Config (get Config "neighbors") 0)
                            (rt.unique "neighbor" (get Config "neighbors") 0 [])))
                  (and (and (rt.array-bounds (get Config "listeners") 0 32)
                            (and (rt.records-valid "listener" Config (get Config "listeners") 0)
                              (rt.unique "listener" (get Config "listeners") 0 [])))
                    (and (and (rt.array-bounds (get Config "peers") 0 4)
                              (and (rt.records-valid "peer" Config (get Config "peers") 0)
                                (rt.unique "peer" (get Config "peers") 0 [])))
                      (and (rt.range (get Config "flow-limit") 1 64)
                        (and (rt.range (get Config "tenant-limit") 1 (get Config "flow-limit"))
                          (and (rt.range (get Config "port-low") 1024 65535)
                            (and (rt.range (get Config "port-high") (get Config "port-low") 65535)
                              (and (<= (- (get Config "port-high") (get Config "port-low")) 63)
                                (and (rt.range (get Config "pending-limit") 1 32)
                                  (rt.range (get Config "neighbor-limit") 1 64)))))))))))))))))

(define rt.control
  State Reason -> (obj [["state" State] ["reason" Reason] ["generation" (get State "generation")]]))

(define rt.boot
  Arg ->
    (let Config (get Arg "config")
      (if (not (rt.config-valid Config)) (rt.control null "invalid-configuration")
        (let Mode (rt.get-or Arg "mode" "single")
          (if (not (rt.contains ["single" "standby"] Mode)) (rt.control null "invalid-mode")
            (rt.control
              (obj [["schema" 1] ["generation" 1] ["config" Config] ["candidate" null] ["previous" null]
                    ["counters" (obj [])] ["flows" []] ["flow-serial" 0] ["pending" []] ["neighbors" []]
                    ["peer-routes" []] ["sessions" (obj [])] ["mode" Mode] ["lease-until" 0] ["epoch" 0]
                    ["replica-epoch" 0] ["last-replica" null] ["offer" null]])
              "configured"))))))

(define rt.candidate
  Arg ->
    (let State (get Arg "state")
      (if (not (= (get Arg "generation") (get State "generation"))) (rt.control State "stale-generation")
        (if (not (rt.config-valid (get Arg "config"))) (rt.control State "invalid-configuration")
          (rt.control (rt.put State "candidate" (obj [["base" (get State "generation")] ["config" (get Arg "config")]]))
            "candidate-validated")))))

(define rt.publish
  State Config ->
    (merge State
      (obj [["config" Config] ["generation" (+ 1 (get State "generation"))] ["previous" (get State "config")]
            ["candidate" null] ["pending" []] ["neighbors" []] ["peer-routes" []] ["sessions" (obj [])] ["offer" null]])))

(define rt.commit
  Arg ->
    (let State (get Arg "state")
      (let Candidate (get State "candidate")
        (if (= Candidate null) (rt.control State "no-candidate")
          (if (not (= (get Candidate "base") (get State "generation"))) (rt.control State "stale-generation")
            (rt.control (rt.publish State (get Candidate "config")) "committed"))))))

(define rt.rollback
  Arg ->
    (let State (get Arg "state")
      (if (not (= (get Arg "generation") (get State "generation"))) (rt.control State "stale-generation")
        (if (= (get State "previous") null) (rt.control State "no-previous-configuration")
          (rt.control (rt.publish State (get State "previous")) "rolled-back")))))

(define rt.backend-at
  Rows Id I ->
    (if (= I (len Rows)) null
      (if (= (get (at Rows I) "id") Id) I (rt.backend-at Rows Id (+ I 1)))))

(define rt.listener-position
  Rows Key I ->
    (if (= I (len Rows)) null
      (if (= (rt.unique-key "listener" (at Rows I)) Key) I (rt.listener-position Rows Key (+ I 1)))))

(define rt.health
  Field Arg ->
    (let State (get Arg "state")
      (let Config (get State "config")
        (let Rows (get Config "listeners")
          (let Position (rt.listener-position Rows (get Arg "listener") 0)
            (if (= Position null) (rt.control State "unknown-listener")
              (let Listener (at Rows Position)
                (let Backends (get Listener "backends")
                  (let Index (rt.backend-at Backends (get Arg "backend") 0)
                    (if (= Index null) (rt.control State "unknown-backend")
                      (if (or (not (bool? (get Arg "value")))
                            (and (not (= (get Arg "target") null))
                              (not (= (get Arg "target") (rt.probe-identity (at Backends Index))))))
                        (rt.control State "invalid-health")
                        (if (= (get (at Backends Index) Field) (get Arg "value")) (rt.control State "unchanged")
                          (let Updated (rt.put Listener "backends"
                                          (rt.patch Backends Index [(rt.put (at Backends Index) Field (get Arg "value"))]))
                            (rt.control
                              (merge State
                                (obj [["config" (rt.put Config "listeners" (rt.patch Rows Position [Updated]))]
                                      ["generation" (+ 1 (get State "generation"))]]))
                              "backend-updated"))))))))))))))

(define rt.tick
  Arg ->
    (let State (get Arg "state")
      (let Now (get Arg "now")
        (rt.control
          (merge State
            (obj [["flows" (rt.active-records (get State "flows") Now)]
                  ["pending" (rt.active-records (get State "pending") Now)]
                  ["neighbors" (rt.active-records (get State "neighbors") Now)]
                  ["offer" (let Offer (get State "offer")
                             (if (or (= Offer null)
                                   (<= (get (get (get Offer "offer") "flow") "until") Now)) null Offer))]]))
          (if (rt.owns-lease State Now) "ready" "fenced")))))

(define rt.status
  Arg ->
    (let State (get Arg "state")
      (let Now (get Arg "now")
        (obj [["generation" (get State "generation")] ["mode" (get State "mode")]
              ["epoch" (get State "epoch")] ["ready" (rt.owns-lease State Now)]
              ["counters" (get State "counters")] ["flows" (len (rt.active-records (get State "flows") Now))]
              ["neighbors" (len (rt.active-records (get State "neighbors") Now))]
              ["pending" (len (rt.active-records (get State "pending") Now))]
              ["peer-routes" (len (get State "peer-routes"))]]))))

(define rt.lease
  Arg ->
    (let State (get Arg "state")
      (let Epoch (get Arg "epoch")
        (let Until (get Arg "until")
          (let Now (get Arg "now")
            (if (or (= (get State "mode") "single")
                  (or (not (rt.range Epoch 1 9223372036854775807)) (not (rt.range Until Now (+ Now 30)))))
              (rt.control State "invalid-lease")
              (if (or (< Epoch (get State "epoch")) (<= Epoch (get State "replica-epoch")))
                (rt.control State "stale-lease")
                (if (= Until Now)
                  (rt.control (merge State (obj [["mode" "standby"] ["lease-until" 0] ["epoch" Epoch] ["offer" null]])) "fenced")
                  (if (and (= Epoch (get State "epoch")) (not (= (get State "mode") "active")))
                    (rt.control State "stale-lease")
                    (rt.control
                      (merge State (obj [["mode" "active"] ["lease-until" Until] ["epoch" Epoch]
                                        ["peer-routes" (if (> Epoch (get State "epoch")) [] (get State "peer-routes"))]
                                        ["neighbors" (if (> Epoch (get State "epoch")) [] (get State "neighbors"))]
                                        ["pending" (if (> Epoch (get State "epoch")) [] (get State "pending"))]
                                        ["offer" (if (> Epoch (get State "epoch")) null (get State "offer"))]]))
                      "lease-active"))))))))))

(define rt.alias-at
  Rows Port Address I ->
    (if (= I (len Rows)) false
      (let Row (at Rows I)
        (or (or (and (= Port (get Row "front")) (= Address (rt.get-or Row "address" (get Row "listener"))))
              (and (= Port (get Row "back")) (= Address (get Row "translation"))))
          (rt.alias-at Rows Port Address (+ I 1))))))

(define rt.local-or-alias
  State Port Address ->
    (or (rt.contains (get Port "addresses") Address)
      (or (rt.alias-at (get (get State "config") "listeners") (get Port "id") Address 0)
        (rt.alias-at (get State "flows") (get Port "id") Address 0))))

(define rt.listener-ports
  Config Listener ->
    (let Front (rt.interface Config (get Listener "front"))
      (let Back (rt.interface Config (get Listener "back"))
        (let Tenant (get Listener "tenant")
          (and (not (= (get Listener "front") (get Listener "back")))
            (and (= Tenant (get Front "tenant")) (= Tenant (get Back "tenant"))))))))

(define rt.selected-frame
  Defer Arg -> (rt.frame "round-robin" Defer Arg))

(define rt.packet-entry
  Arg -> (rt.selected-frame false Arg))

(define rt.stage-frame
  Arg -> (rt.selected-frame true Arg))

(define rt.offer-live
  State Now ->
    (let Pending (get State "offer")
      (and (not (= Pending null)) (> (get (get (get Pending "offer") "flow") "until") Now))))

\\ Replication uses trusted outcomes from a standby. A pending binding is
\\ absent from lookup and emission until its exact offer returns committed.

(define rt.replica
  Arg ->
    (let State (get Arg "state")
      (let Offer (get Arg "offer")
        (let Epoch (get Offer "epoch")
          (let Now (get Arg "now")
            (if (or (not (= (get State "mode") "standby"))
                  (or (not (rt.range Epoch 1 9223372036854775807))
                    (or (< Epoch (get State "replica-epoch"))
                      (or (<= Epoch (get State "epoch"))
                        (not (= (get Offer "configuration") (digest (get State "config"))))))))
              (rt.control State "replica-rejected")
              (if (= Offer (get State "last-replica"))
                (rt.put (rt.control State "replica-committed") "ack" (get Offer "id"))
                (rt.store-replica State Offer Now))))))))

(define rt.store-replica
  State Offer Now ->
    (let Flow (get Offer "flow")
      (let Id (get Offer "id")
        (let Flows (rt.active-records (get State "flows") Now)
          (if (or (not (rt.array-bounds Id 2 2))
                (or (not (= (at Id 0) (get Offer "epoch")))
                  (or (not (rt.range (at Id 1) 1 9223372036854775807))
                    (or (not (rt.range (get Flow "until") (+ Now 1) (+ Now 300)))
                      (or (not (= (get Flow "fwd") (rt.flow-key (get Flow "tenant") (get Flow "protocol")
                                                     (get Flow "client") (get Flow "client-port")
                                                     (get Flow "listener") (get Flow "listener-port"))))
                        (or (not (= (get Flow "rev") (rt.flow-key (get Flow "tenant") (get Flow "protocol")
                                                       (get Flow "backend") (get Flow "backend-port")
                                                       (get Flow "translation") (get Flow "allocated"))))
                          (not (= (rt.flow-at Flows (get Flow "fwd") "fwd" 0 Now) null))))))))
            (rt.control State "replica-conflict")
            (if (or (rt.port-owned-at Flows (get Flow "translation") (get Flow "protocol") (get Flow "allocated") 0)
                  (>= (len Flows) (get (get State "config") "flow-limit")))
              (rt.control State "replica-conflict")
              (rt.put (rt.control
                        (merge State (obj [["flows" (append Flows [Flow])] ["replica-epoch" (get Offer "epoch")]
                                          ["last-replica" Offer]]))
                        "replica-committed") "ack" Id)))))))

(define rt.ack
  Defer Arg ->
    (let State (get Arg "state")
      (let Now (get Arg "now")
        (let Pending (get State "offer")
          (let Offer (get Pending "offer")
            (let Flow (get Offer "flow")
              (if (or (not (= (get State "mode") "active")) (not (rt.owns-lease State Now)))
                (rt.result (rt.put State "offer" null) "fenced" [])
                (if (or (= Offer null) (not (= (get Arg "ack") (get Offer "id"))))
                  (rt.result State "unknown-replica-ack" [])
                  (if (or (not (= (get Offer "generation") (get State "generation"))) (<= (get Flow "until") Now))
                    (rt.result (rt.put State "offer" null) "stale-replica-ack" [])
                    (let Bound (merge State (obj [["offer" null] ["flows" (append (get State "flows") [Flow])]]))
                      (let Port (rt.interface (get State "config") (get Pending "port"))
                        (if Defer
                          (rt.resume-plan Bound (get Pending "packet") (get Pending "transport") Port (get Pending "source-mac") Flow false)
                          (rt.translate-flow Bound (get Pending "packet") (get Pending "transport") Port
                            (get Pending "source-mac") Flow false Now)))))))))))))

(define rt.recover
  Arg ->
    (let State (get Arg "state")
      (if (not (= (get State "schema") 1)) (rt.control State "unsupported-schema")
        (rt.control
          (merge State (obj [["sessions" (obj [])] ["peer-routes" []] ["neighbors" []] ["pending" []] ["offer" null]
                            ["flows" (if (= (get State "mode") "single") (rt.restore-reservations (get State "flows") 0 []) (get State "flows"))]
                            ["mode" (if (= (get State "mode") "single") "single" "standby")] ["lease-until" 0]]))
          "recovered"))))

(define rt.forwarding-policy
  Interface Packet -> (rt.policy-at (get Interface "policy") Packet 0))

(define rt.control-frames
  State Reason Now Control Data ->
    (if (= Control []) (rt.result State Reason Data)
      (let Credit (if (= (get State "control-second") Now) (get State "control-used") 0)
        (if (>= Credit 32)
          (rt.put (rt.result (rt.count State "control-suppressed") Reason Data) "control-suppressed" true)
          (rt.result
            (merge State (obj [["control-second" Now] ["control-used" (+ Credit 1)]]))
            Reason (append Control Data))))))

(define rt.probe-identity
  Backend -> [(get Backend "address") (get Backend "probe-port")])

(define rt.probe-backends
  Listener Backends I Rows ->
    (if (or (= I (len Backends)) (= (len Rows) 32)) Rows
      (let Backend (at Backends I)
        (rt.probe-backends Listener Backends (+ I 1)
          (if (= (get Backend "probe-port") null) Rows
            (append Rows [(obj [["listener" (rt.unique-key "listener" Listener)] ["backend" (get Backend "id")]
                                ["target" (rt.probe-identity Backend)]])]))))))

(define rt.probe-listeners
  Listeners I Rows ->
    (if (or (= I (len Listeners)) (= (len Rows) 32)) Rows
      (let Listener (at Listeners I)
        (rt.probe-listeners Listeners (+ I 1) (rt.probe-backends Listener (get Listener "backends") 0 Rows)))))

(define rt.probes
  Arg ->
    (obj [["probes" (rt.probe-listeners (get (get (get Arg "state") "config") "listeners") 0 [])]]))


(define rt.replica-ack
  Arg -> (rt.ack false Arg))

(define rt.stage-ack
  Arg -> (rt.ack true Arg))

(define rt.resume-plan
  State Packet Transport Port SourceMac Flow Reverse ->
    (obj [["state" State] ["durable" true] ["tx" []]
          ["resume" (obj [["packet" Packet] ["transport" Transport] ["port" (get Port "id")]
                          ["source-mac" SourceMac] ["key" (get Flow "fwd")] ["reverse" Reverse]])]]))

(define rt.resume
  Arg ->
    (let State (get Arg "state")
      (let Now (get Arg "now")
        (if (not (rt.owns-lease State Now)) (rt.result State "fenced" [])
          (let Flows (get State "flows")
            (let Position (rt.flow-at Flows (get Arg "key") "fwd" 0 Now)
              (if (= Position null) (rt.result State "flow-expired" [])
                (rt.translate-flow State (get Arg "packet") (get Arg "transport")
                  (rt.interface (get State "config") (get Arg "port")) (get Arg "source-mac")
                  (at Flows Position) (get Arg "reverse") Now))))))))

(define rt.sending-permitted
  Arg -> (obj [["ready" (rt.owns-lease (get Arg "state") (get Arg "now"))]]))

\\ The slow transaction stages against a fixed view. Packet batches may
\\ refresh timers and counters while its durable write is in flight.

(define rt.restore-reservations
  Flows I Rows ->
    (if (= I (len Flows)) Rows
      (let Flow (at Flows I)
        (rt.restore-reservations Flows (+ I 1)
          (let Until (get Flow "until")
            (let Reserved (rt.get-or Flow "reserved-until" Until)
              (append Rows [(rt.put Flow "until" (if (> Reserved Until) Reserved Until))])))))))

(define rt.index-bound-flows
  Flows I ByKey ->
    (if (= I (len Flows)) ByKey
      (let Flow (at Flows I)
        (rt.index-bound-flows Flows (+ I 1) (rt.put ByKey (json (get Flow "fwd")) Flow)))))

(define rt.latest-flow
  Flow ByKey ->
    (let Candidate (get ByKey (json (get Flow "fwd")))
      (if (and (not (= Candidate null))
            (and (> (get Candidate "until") (get Flow "until"))
              (= Flow (rt.put Candidate "until" (get Flow "until")))))
        Candidate Flow)))

(define rt.keep-refreshes
  Flows ByKey I Rows ->
    (if (= I (len Flows)) Rows
      (rt.keep-refreshes Flows ByKey (+ I 1)
        (append Rows [(rt.latest-flow (at Flows I) ByKey)]))))

(define rt.keep-counts
  Keys Base Next Live I ->
    (if (= I (len Keys)) Live
      (let Key (at Keys I)
        (let Delta (- (get Next Key) (rt.get-or Base Key 0))
          (rt.keep-counts Keys Base Next
            (if (= Delta 0) Live (rt.put Live Key (+ (rt.get-or Live Key 0) Delta))) (+ I 1))))))

(define rt.publish-live
  Arg ->
    (let Next (get Arg "next")
      (let Live (get Arg "state")
        (let Base (get Arg "base")
          (let Reset (or (not (= (get Base "epoch") (get Next "epoch")))
                         (rt.contains ["committed" "rolled-back" "recovered"] (get Arg "reason")))
            (let Counters (get Next "counters")
              (obj [["state"
                  (merge Next
                    (obj [["flows" (if (= (get Base "flows") (get Next "flows")) (get Live "flows")
                                     (rt.keep-refreshes (get Next "flows") (rt.index-bound-flows (get Live "flows") 0 (obj [])) 0 []))]
                          ["neighbors" (if Reset (get Next "neighbors") (get Live "neighbors"))]
                          ["pending" (if Reset (get Next "pending") (get Live "pending"))]
                          ["control-second" (get Live "control-second")]
                          ["control-used" (get Live "control-used")]
                          ["counters" (rt.keep-counts (keys Counters) (get Base "counters") Counters (get Live "counters") 0)]]))]])))))))
