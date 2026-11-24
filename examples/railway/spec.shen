; Railway residual specification. Layout is static and event codes are dynamic.
(define railway.step
  [Route Occupied] [request Route] -> [Route Occupied]
  [Route false] [cancel Route] -> [none false]
  [Route false] [entry Route] -> [Route true]
  [Route true] [clear Route] -> [none false]
  State Event -> State)

(define specialize
  Layout -> railway.step)
