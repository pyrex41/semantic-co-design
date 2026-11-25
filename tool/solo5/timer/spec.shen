\\ A node written only to exercise the timer and clock paths of an effect shim.
\\
\\ State [Fired Last]: timers fired since the last restart, and the last clock reading.
\\ Every reading is also said aloud, so the outside sees the clock the node saw.

(define timer.init _ _ -> [0 0])

(define timer.step
  _ [restart] -> [[0 0] [[now 0]]]
  State [arm Id Delay] -> [State [[set Id Delay] [now Id]]]
  State [disarm Id] -> [State [[cancel Id]]]
  [Fired Last] [fired Id] -> [[(+ Fired 1) Last] [[now Id] [say Id Fired]]]
  [Fired _] [time Token Now] -> [[Fired Now] [[say Token Now]]])
