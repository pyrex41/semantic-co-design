\\ Heartbeat: two nodes ping each other on a timer and note the time.
\\ It exists to give tool/host the roles AcceptedJob does not use: send, recv,
\\ timer-set, timer-cancel and clock-read.
\\
\\ State  [Me Seen Beats Last]: pings received, pings sent, the last clock reading.

(define hb.init
  Me _ -> [Me 0 0 0])

(define hb.step
  [Me _ _ _] [restart] -> [[Me 0 0 0] [[arm 1 20]]]
  [Me Seen Beats Last] [tick 1] ->
    [[Me Seen (+ Beats 1) Last] [[ping (- 1 Me) Beats] [now Beats] [arm 1 20]]]
  [Me Seen Beats Last] [pinged _ _] -> [[Me (+ Seen 1) Beats Last] []]
  [Me Seen Beats _] [clocked _ Now] -> [[Me Seen Beats Now] []]
  [Me Seen Beats Last] [status Req] -> [[Me Seen Beats Last] [[report Req Seen Beats Last]]]
  [Me Seen Beats Last] [stop Req] ->
    [[Me Seen Beats Last] [[disarm 1] [report Req Seen Beats Last]]])
