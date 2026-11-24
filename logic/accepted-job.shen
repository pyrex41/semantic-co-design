(define job.step
  _ empty [submit D] -> [[pending D] [receipt D]]
  _ [Status D] [submit D] -> [[Status D] [receipt D]]
  _ [Status D] [submit Other] -> [[Status D] conflict]
  withdraw [pending D] cancel -> [[withdrawn D] cancelled]
  keep [pending D] cancel -> [[pending D] cancelled]
  _ [pending D] work -> [[delivered D] [deliver D]]
  _ State _ -> [State ignored])
