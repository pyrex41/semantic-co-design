\\ What Heartbeat owes across nodes. A step is [Kind Node Event Effects World].

\\ No ping is received that was not sent first: per sender, pings in flight never go
\\ below zero. It holds of a run only if the two logs were put in a causal order.
(define hb.no-ping-from-nowhere
  Steps -> (hb.walk Steps [0 0]))

(define hb.walk
  [] _ -> true
  [[_ Node Event Effects _] | Steps] Flight ->
    (let After (hb.receive Event Flight)
      (if (= After lost)
          [violated Node Event]
          (hb.walk Steps (hb.sends Node Effects After)))))

(define hb.receive
  [pinged Src _] Flight -> (if (> (nth (+ Src 1) Flight) 0) (hb.add Src -1 Flight) lost)
  _ Flight -> Flight)

(define hb.sends
  _ [] Flight -> Flight
  Node [[ping _ _] | Effects] Flight -> (hb.sends Node Effects (hb.add Node 1 Flight))
  Node [_ | Effects] Flight -> (hb.sends Node Effects Flight))

(define hb.add
  0 D [A B] -> [(+ A D) B]
  1 D [A B] -> [A (+ B D)])
