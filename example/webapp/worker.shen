(define claim-delivery ->
  (let Lease (token)
  (let Row (one "WITH ready AS (SELECT tenant,id FROM delivery WHERE status IN ('pending','failed','unknown') AND next_at<=now() AND attempts<5 ORDER BY next_at,id LIMIT 1 FOR UPDATE SKIP LOCKED) UPDATE delivery d SET status='leased', lease=%s, lease_until=clock_timestamp()+interval '30 seconds', attempts=attempts+1 FROM ready WHERE (d.tenant,d.id)=(ready.tenant,ready.id) RETURNING d.tenant,d.id,d.recipient,d.subject,d.body,d.lease,d.attempts" [Lease])
    Row)))

(define release-expired-leases ->
  (sql "UPDATE delivery SET status='unknown',next_at=now()+interval '1 minute',lease=NULL,lease_until=NULL WHERE status='leased' AND lease_until<=now()" []))

(define send-delivery Row ->
  (let Outcome (mail (obj [["to" (get Row "recipient")] ["id" (get Row "id")]
                           ["subject" (get Row "subject")] ["body" (get Row "body")]]))
    (do (begin)
    (do (sql "UPDATE delivery SET status=%s, next_at=now()+make_interval(secs=>30*attempts*attempts),lease=NULL,lease_until=NULL WHERE tenant=%s AND id=%s AND lease=%s"
         [Outcome (get Row "tenant") (get Row "id") (get Row "lease")])
    (do (commit)
      (obj [["worked" true] ["id" (get Row "id")] ["status" Outcome]]))))))

(define expire-review ->
  (let Due (one "SELECT tenant,id FROM submission WHERE status='pending' AND due_at<=now() ORDER BY due_at LIMIT 1" [])
  (let Tenant (get Due "tenant")
  (let Id (get Due "id")
    (if Due
      (do (sql "SELECT slug FROM tenant WHERE slug=%s FOR UPDATE" [Tenant])
      (let Row (one "UPDATE submission s SET status='expired',version=s.version+1 FROM document d, identity i WHERE d.tenant=s.tenant AND d.id=s.document AND i.actor=d.author AND s.tenant=%s AND s.id=%s AND s.status='pending' AND s.due_at<=now() RETURNING s.document,s.revision,s.policy,i.email AS owner_email" [Tenant Id])
        (if Row (terminal Tenant Id Row "expired" "worker") false)))
      false)))))

(define delete-blobs Rows ->
  (if (= (len Rows) 0) true
      (do (blob-delete (get (at Rows 0) "id"))
          (delete-blobs (drop Rows 1)))))

(define reap-staged ->
  (delete-blobs
    (sql "WITH expired AS (SELECT tenant,id FROM attachment WHERE state='staged' AND expires_at<=now() ORDER BY expires_at LIMIT 20 FOR UPDATE SKIP LOCKED) DELETE FROM attachment a USING expired WHERE (a.tenant,a.id)=(expired.tenant,expired.id) RETURNING a.id" [])))

(define reap-orphans Rows ->
  (if (= (len Rows) 0) true
    (let Id (at Rows 0)
      (do (if (exists? "SELECT 1 FROM attachment WHERE id=%s" [Id]) false (blob-delete Id))
          (reap-orphans (drop Rows 1))))))

(define maintenance ->
  (do (expire-review)
  (do (reap-staged)
  (do (reap-orphans (blob-aged))
  (do (sql "DELETE FROM login_intent WHERE expires_at<=now()" [])
      (sql "DELETE FROM session WHERE expires_at<=now()" []))))))

(define worker-once _ ->
  (do (begin)
  (do (release-expired-leases)
  (do (maintenance)
  (let Row (claim-delivery)
    (do (commit)
      (if Row (send-delivery Row) (obj [["worked" false]]))))))))
