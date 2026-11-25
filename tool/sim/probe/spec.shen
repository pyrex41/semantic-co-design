\\ A node written only to exercise the simulator. One request makes four externally
\\ visible effects, two of them disk writes, so a crash between two effects, the order of
\\ syncs and the persisted prefix of several pending writes can all be observed.
\\
\\ State [Acks Loaded]: acknowledgements seen since the last restart, and the value that
\\ restart read back from key 1 (0 when the row is missing).

(define probe.init _ _ -> [0 0])

(define probe.step
  _ [restart] -> [[0 0] [[read 0 1]]]
  [Acks _] [loaded _ _ _ Value] -> [[Acks Value] []]
  State [go V] -> [State [[write 1 1 V] [say 1 V] [write 2 2 V] [say 2 V]]]
  [Acks Loaded] [acked Token] -> [[(+ Acks 1) Loaded] [[say 3 Token]]])
