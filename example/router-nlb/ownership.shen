\\ Tuple ownership law over observed state cuts. This file uses no packet
\\ lookup, allocation, replication, or recovery definitions.

(define own.forward
  Flow -> [(get Flow "tenant") (get Flow "protocol")
           (get Flow "client") (get Flow "client-port")
           (get Flow "listener") (get Flow "listener-port")])

(define own.reverse
  Flow -> [(get Flow "tenant") (get Flow "protocol")
           (get Flow "backend") (get Flow "backend-port")
           (get Flow "translation") (get Flow "allocated")])

(define own.reservation
  Flow -> [(get Flow "protocol") (get Flow "translation") (get Flow "allocated")])

(define own.identity
  Flow -> [(own.forward Flow) (own.reverse Flow) (own.reservation Flow)
           (get Flow "front") (get Flow "back")])

(define own.canonical
  Rows I ->
    (if (= I (len Rows)) true
      (let Row (at Rows I)
        (and (obj? Row)
          (and (int? (get Row "until"))
            (and (= (get Row "fwd") (own.forward Row))
              (and (= (get Row "rev") (own.reverse Row))
                (and (not (= (own.forward Row) (own.reverse Row)))
                  (own.canonical Rows (+ I 1))))))))))

(define own.live
  Rows Now I Active ->
    (if (= I (len Rows)) Active
      (let Row (at Rows I)
        (own.live Rows Now (+ I 1)
          (if (> (get Row "until") Now) (prepend Row Active) Active)))))

(define own.overlap
  A B ->
    (let First [(own.forward B) (own.reverse B)]
      (or (= (own.reservation A) (own.reservation B))
        (or (contains First (own.forward A)) (contains First (own.reverse A))))))

(define own.conflicting-pair
  "local" A B -> (own.overlap A B)
  "replica" A B ->
    (and (own.overlap A B)
      (or (not (= (own.identity A) (own.identity B)))
        (not (= (get A "until") (get B "until"))))))

(define own.conflicts
  Domain Row Rows I ->
    (if (= I (len Rows)) false
      (or (own.conflicting-pair Domain Row (at Rows I)) (own.conflicts Domain Row Rows (+ I 1)))))

(define own.local-collision
  Rows I ->
    (if (= I (len Rows)) false
      (or (own.conflicts "local" (at Rows I) Rows (+ I 1)) (own.local-collision Rows (+ I 1)))))

(define own.same-binding
  Identity Rows I ->
    (if (= I (len Rows)) false
      (or (= Identity (own.identity (at Rows I))) (own.same-binding Identity Rows (+ I 1)))))

(define own.retained
  Prior Rows I ->
    (if (= I (len Prior)) true
      (and (own.same-binding (own.identity (at Prior I)) Rows 0) (own.retained Prior Rows (+ I 1)))))

(define own.replica-conflict
  Rows Other I ->
    (if (= I (len Rows)) false
      (or (own.conflicts "replica" (at Rows I) Other 0) (own.replica-conflict Rows Other (+ I 1)))))

(define own.check-cut
  Nodes Now I Checked ->
    (if (= I (len Nodes)) "holds"
      (let Node (at Nodes I)
        (let Prior (get Node "before")
          (let Current (get Node "after")
            (if (not (and (own.canonical Prior 0) (own.canonical Current 0))) "incoherent-tuple"
              (let Live (own.live Current Now 0 [])
                (if (own.local-collision Live 0) "local-collision"
                  (if (not (own.retained (own.live Prior Now 0 []) Live 0)) "retired-live-binding"
                    (if (own.replica-conflict Live Checked 0) "replica-conflict"
                      (own.check-cut Nodes Now (+ I 1) (append Live Checked))))))))))))

(define own.law
  Arg ->
    (let Verdict (own.check-cut (get Arg "nodes") (get Arg "clock") 0 [])
      (obj [["holds" (= Verdict "holds")] ["verdict" Verdict]])))
