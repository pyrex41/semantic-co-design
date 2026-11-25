\\ Which definitions the engine accepts. The engine itself assumes all of this and checks
\\ none of it, so a definition is validated here, by shen-jit, before it is compiled
\\ (model.py). (def.problems Definition) is the list of what is wrong with it; [] is valid.
\\
\\   [not-a-definition]              not [UndoAttempts UndoTimeout Program]
\\   [bad-number What N]             a count or a time that is not a positive integer
\\   [bad-instruction Pc]            not one of act, each, sleep, branch, end, or misshapen
\\   [bad-key Key]                   an idempotency key that is not a positive integer
\\   [duplicate-key Key]             one key on two activity instances: the second would be
\\                                   answered from the first one's record and never run
\\   [branch-not-forward Pc Target]  a branch to itself or backwards: a loop
\\   [branch-out-of-range Pc Target] a branch past the end of the program
\\   [does-not-end]                  the last instruction is not [end]
\\
\\ With forward branches inside the program and [end] last, every path reaches an [end].

(define def.problems
  [UndoAttempts UndoTimeout Program] ->
    (append (def.positive undo-attempts UndoAttempts)
            (append (def.positive undo-timeout UndoTimeout)
                    (append (def.instructions Program 0 (length Program))
                            (append (def.keys-of Program [])
                                    (def.ends Program)))))
    where (cons? Program)
  _ -> [[not-a-definition]])

(define def.positive
  _ N -> [] where (and (integer? N) (> N 0))
  What N -> [[bad-number What N]])

(define def.instructions
  [] _ _ -> []
  [I | Is] Pc Length -> (append (def.instruction I Pc Length) (def.instructions Is (+ Pc 1) Length)))

(define def.instruction
  [act Name _ Attempts Backoff Timeout] Pc _ -> (def.activity Attempts Backoff Timeout)
    where (symbol? Name)
  [each Name Keys Attempts Backoff Timeout] Pc _ -> (def.activity Attempts Backoff Timeout)
    where (and (symbol? Name) (cons? Keys))
  [sleep Delay] _ _ -> (def.positive sleep Delay)
  [branch Result Target] Pc Length -> (def.branch Pc Target Length)
    where (and (integer? Result) (integer? Target))
  [end] _ _ -> []
  _ Pc _ -> [[bad-instruction Pc]])

(define def.activity
  Attempts Backoff Timeout ->
    (append (def.positive attempts Attempts)
            (append (def.positive backoff Backoff) (def.positive timeout Timeout))))

(define def.branch
  Pc Target _ -> [[branch-not-forward Pc Target]] where (<= Target Pc)
  Pc Target Length -> [[branch-out-of-range Pc Target]] where (>= Target Length)
  _ _ _ -> [])

\\ Every key is a positive integer and belongs to one activity instance only.
(define def.keys-of
  [] _ -> []
  [[act _ Key | _] | Is] Seen -> (def.key Key Seen Is)
  [[each Name [Key | Keys] | Rest] | Is] Seen ->
    (append (def.key Key Seen []) (def.keys-of [[each Name Keys | Rest] | Is] [Key | Seen]))
  [_ | Is] Seen -> (def.keys-of Is Seen))

(define def.key
  Key Seen Is -> [[bad-key Key] | (def.keys-of Is Seen)] where (not (and (integer? Key) (> Key 0)))
  Key Seen Is -> [[duplicate-key Key] | (def.keys-of Is Seen)] where (element? Key Seen)
  Key Seen Is -> (def.keys-of Is [Key | Seen]))

(define def.ends
  [[end]] -> []
  [_ I | Is] -> (def.ends [I | Is])
  _ -> [[does-not-end]])
