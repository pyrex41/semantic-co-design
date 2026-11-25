\\ Rejected: charge reuses the key of reserve. The workers would answer every charge request
\\ from the record of the reservation, and the charge would never run.

(define rejected.definition ->
  [2 45
   [[act reserve 101 2 4 60]
    [act charge 101 3 4 30]
    [act ship 104 2 4 30]
    [end]]])
