\\ A node written only to exercise the disk paths of an effect shim that AcceptedJob and
\\ the simulator's probe do not reach: a read on request, a write and a read in one step,
\\ and a device that refuses an operation.
\\
\\ State [Last]: the value the last read returned (0 for a missing row).
\\ Every acknowledgement and every read result is said aloud.

(define store.init _ _ -> [0])

(define store.step
  _ [restart] -> [[0] []]
  State [put Key V] -> [State [[write Key Key V]]]
  State [get Key] -> [State [[read Key Key]]]
  \\ One step writes a row and then reads a row: the same one when Other is Key.
  State [swap Key Other V] -> [State [[write Key Key V] [read Other Other]]]
  State [acked Token] -> [State [[done Token]]]
  _ [loaded _ _ Found Value] -> [[Value] [[say Found Value]]])
