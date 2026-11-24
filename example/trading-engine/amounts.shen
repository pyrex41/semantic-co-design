\\ Positive fixed-point values. -1 marks invalid venue decimal text.
(define tx.floor
  N D -> (floor-div (- N (floor-mod N D)) D))

(define tx.min
  A B -> (if (< A B) A B))

(define tx.max
  A B -> (if (> A B) A B))

(define tx.digit
  "0" -> 0
  "1" -> 1
  "2" -> 2
  "3" -> 3
  "4" -> 4
  "5" -> 5
  "6" -> 6
  "7" -> 7
  "8" -> 8
  "9" -> 9
  _ -> -1)

(define tx.power10
  0 -> 1
  N -> (* 10 (tx.power10 (- N 1))))

(define tx.rescale
  Value Places ->
    (let Factor (tx.power10 Places)
      (if (<= Value (tx.floor 9223372036854775807 Factor)) (* Value Factor) -1)))

(define tx.decimal-digits
  Text Offset Size Scale Places Value ->
    (if (= Offset Size) (tx.rescale Value (- Scale (tx.max Places 0)))
      (let C (at Text Offset)
        (if (= C ".")
          (if (or (>= Places 0) (or (= Offset 0) (= (+ Offset 1) Size))) -1
            (tx.decimal-digits Text (+ Offset 1) Size Scale 0 Value))
          (let Digit (tx.digit C)
            (if (< Digit 0) -1
              (if (= Places Scale)
                (if (= Digit 0) (tx.decimal-digits Text (+ Offset 1) Size Scale Places Value) -1)
                (if (> Value (tx.floor (- 9223372036854775807 Digit) 10)) -1
                  (tx.decimal-digits Text (+ Offset 1) Size Scale
                    (if (< Places 0) -1 (+ Places 1)) (+ (* Value 10) Digit))))))))))

(define tx.decimal
  Text Scale ->
    (if (text? Text 1 40)
      (let Size (len Text)
        (if (or (= Size 0) (> Size 40)) -1
          (tx.decimal-digits Text 0 Size Scale -1 0)))
      -1))

(define tx.zeroes
  0 -> ""
  N -> (concat ["0" (tx.zeroes (- N 1))]))

(define tx.format-decimal
  Value Scale ->
    (let Divisor (tx.power10 Scale)
      (let Fraction (str (floor-mod Value Divisor))
        (concat [(str (tx.floor Value Divisor)) "." (tx.zeroes (- Scale (len Fraction))) Fraction]))))

(define tx.bounded
  Value Limit -> (and (int? Value) (and (>= Value 0) (<= Value Limit))))

(define tx.positive
  Value Limit -> (and (tx.bounded Value Limit) (> Value 0)))

\\ Price is USD cents, quantity is 1e-8 base, cash is 1e-16 USD.
\\ The bound on the unscaled product guarantees the last multiplication.
(define tx.notional
  Price Quantity ->
    (if (and (tx.positive Price 1000000000) (tx.positive Quantity 10000000000))
      (if (<= Price (tx.floor 5000000000000 Quantity)) (* (* Price Quantity) 1000000) -1)
      -1))

\\ Round the fee reserve upward, keeping the intermediate below signed 64-bit.
(define tx.fee-reserve
  Quote Ppm ->
    (+ (* (tx.floor Quote 1000000) Ppm)
       (tx.floor (+ (* (floor-mod Quote 1000000) Ppm) 999999) 1000000)))
