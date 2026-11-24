(define eligible-reviewers Tenant Author Limit ->
  (sql "SELECT m.actor,i.email FROM membership m JOIN identity i ON i.actor=m.actor WHERE m.tenant=%s AND m.role='reviewer' AND m.actor<>%s ORDER BY m.actor LIMIT %s"
    [Tenant Author Limit]))

(define assign-reviewers Who Id Rows ->
  (if (= (len Rows) 0) true
    (let Row (at Rows 0)
    (let Reviewer (get Row "actor")
      (do (sql "INSERT INTO assignment(tenant,submission,actor) VALUES (%s,%s,%s)" [(tenant Who) Id Reviewer])
      (do (deliver (tenant Who) (concat [Id Reviewer]) "review" (get Row "email") "Document review requested"
            (concat ["A document is ready for review at " (setting "SC_ORIGIN") "/t/" (tenant Who) "/reviews/" Id "."]))
          (assign-reviewers Who Id (drop Rows 1))))))))

(define submit-document Who Params Body ->
  (let Tenant (tenant Who)
  (let Id (get Params "document")
  (let Doc (owned-doc Who Id)
  (let Workflow (one "SELECT version,artifact FROM workflow WHERE tenant=%s AND active" [Tenant])
  (let Policy (get Workflow "artifact")
  (let Sub (token)
    (do (expected Doc "revision" Body "expected_revision")
    (do (require (= (get Doc "status") "draft") 409 "not-draft" "Revise a rejected document before submitting it again.")
    (do (require Policy 409 "workflow-missing" "A tenant administrator must publish a workflow.")
    (let Quorum (required-votes Policy (get Doc "amount"))
    (let Reviewers (eligible-reviewers Tenant (get Doc "author") Quorum)
      (do (require (= (len Reviewers) Quorum) 409 "reviewers-missing" "Invite enough active reviewers to satisfy this workflow.")
      (do (sql "INSERT INTO submission(tenant,id,document,revision,policy,quorum,status,due_at) VALUES (%s,%s,%s,%s,%s,%s,'pending',now()+make_interval(hours=>%s))"
            [Tenant Sub Id (get Doc "revision") (get Workflow "version") Quorum (get Policy "expires_hours")])
      (do (sql "UPDATE document SET status='in-review',updated_at=now() WHERE tenant=%s AND id=%s" [Tenant Id])
      (do (assign-reviewers Who Sub Reviewers)
      (do (audit Tenant (actor Who) "submission.created" Sub
             (obj [["document" Id] ["revision" (get Doc "revision")] ["workflow" (get Workflow "version")] ["quorum" Quorum]]))
         (obj [["id" Sub] ["document" Id] ["revision" (get Doc "revision")] ["version" 1] ["quorum" Quorum]
               ["status" "pending"] ["policy" (get Workflow "version")]]))))))))))))))))))

(define review-row Who Id ->
  (must (one "SELECT s.id,s.document,s.revision,s.version,s.status,s.policy,s.quorum,s.due_at::text,(s.due_at<=now()) AS overdue,w.artifact,i.email AS owner_email,d.author,r.title,r.body,r.amount,v.decision AS own_vote FROM submission s JOIN assignment a ON (a.tenant,a.submission)=(s.tenant,s.id) JOIN document d ON (d.tenant,d.id)=(s.tenant,s.document) JOIN identity i ON i.actor=d.author JOIN revision r ON (r.tenant,r.document,r.version)=(s.tenant,s.document,s.revision) JOIN workflow w ON (w.tenant,w.version)=(s.tenant,s.policy) LEFT JOIN vote v ON (v.tenant,v.submission,v.actor)=(a.tenant,a.submission,a.actor) WHERE s.tenant=%s AND s.id=%s AND a.actor=%s"
          [(tenant Who) Id (actor Who)]) "review-missing" "This review is unavailable."))

(define review-page-data Who Req ->
  (do (permit "review" Who)
    (let Row (review-row Who (submission-key Req))
      (put (remove Row "owner_email") "files"
        (sql "SELECT id,revision,filename,length,digest FROM attachment WHERE tenant=%s AND document=%s AND revision=%s AND state='attached' ORDER BY id"
           [(tenant Who) (get Row "document") (get Row "revision")])))))

(define review-inbox Who Req ->
  (do (permit "review" Who)
    (rows-page (sql "SELECT s.id,s.document,s.revision,s.version,s.status,s.quorum,s.policy,s.due_at::text,r.title,v.decision AS own_vote FROM assignment a JOIN submission s ON (a.tenant,a.submission)=(s.tenant,s.id) JOIN revision r ON (r.tenant,r.document,r.version)=(s.tenant,s.document,s.revision) LEFT JOIN vote v ON (v.tenant,v.submission,v.actor)=(a.tenant,a.submission,a.actor) WHERE a.tenant=%s AND a.actor=%s AND s.id>%s AND strpos(lower(r.title),lower(%s))>0 ORDER BY s.id LIMIT 31"
                  [(tenant Who) (actor Who) (after Req) (search Req)]))))

(define require-open Row Body ->
  (do (expected Row "version" Body "expected_version")
    (require (and (= (get Row "status") "pending") (and (not (get Row "own_vote")) (not (get Row "overdue"))))
      409 "review-closed" "The review is closed or you have already voted.")))

(define terminal Tenant Id Row Status Actor ->
  (do (sql "UPDATE document SET status=%s, updated_at=now() WHERE tenant=%s AND id=%s"
        [(if (= Status "expired") "rejected" Status) Tenant (get Row "document")])
  (do (audit Tenant Actor "submission.closed" Id (obj [["status" Status] ["revision" (get Row "revision")] ["workflow" (get Row "policy")]]))
      (deliver Tenant Id "terminal" (get Row "owner_email") "Document review finished"
        (concat ["Your document review finished with status " Status ". Open " (setting "SC_ORIGIN") "/t/" Tenant "/documents/" (get Row "document") "."])))))

(define vote-status Choice Count Quorum ->
  (if (= Choice "reject") "rejected" (if (>= Count Quorum) "approved" "pending")))

(define cast-vote Who Params Body ->
  (let Tenant (tenant Who)
  (let Id (get Params "submission")
  (let Row (review-row Who Id)
  (let Choice (get Body "decision")
    (do (require-open Row Body)
    (do (sql "INSERT INTO vote(tenant,submission,actor,decision,note) VALUES (%s,%s,%s,%s,%s)"
          [Tenant Id (actor Who) Choice (get Body "note")])
    (let Count (get (one "SELECT count(*) AS count FROM vote WHERE tenant=%s AND submission=%s AND decision='approve'" [Tenant Id]) "count")
    (let Next (vote-status Choice Count (get Row "quorum"))
    (let Version (+ (get Row "version") 1)
      (do (sql "UPDATE submission SET version=%s,status=%s WHERE tenant=%s AND id=%s" [Version Next Tenant Id])
      (do (audit Tenant (actor Who) "vote.recorded" Id (obj [["decision" Choice] ["version" Version] ["workflow" (get Row "policy")] ["note" (get Body "note")]]))
      (do (if (= Next "pending") true (terminal Tenant Id Row Next (actor Who)))
          (obj [["id" Id] ["version" Version] ["status" Next] ["votes" Count] ["quorum" (get Row "quorum")]]))))))))))))))

(define delegate-review Who Params Body ->
  (let Tenant (tenant Who)
  (let Id (get Params "submission")
  (let Row (review-row Who Id)
  (let Email (lower (get Body "email"))
    (do (require-open Row Body)
    (do (require (get (get Row "artifact") "delegation") 403 "delegation-denied" "This workflow does not allow delegation.")
    (let Target (one "SELECT m.actor,i.email FROM membership m JOIN identity i ON i.actor=m.actor WHERE m.tenant=%s AND m.role='reviewer' AND i.email=%s AND m.actor<>%s AND m.actor<>%s AND NOT EXISTS (SELECT 1 FROM assignment a WHERE a.tenant=m.tenant AND a.submission=%s AND a.actor=m.actor)"
                  [Tenant Email (get Row "author") (actor Who) Id])
      (do (require Target 422 "invalid-delegate" "Choose an unassigned active reviewer.")
      (do (sql "DELETE FROM assignment WHERE tenant=%s AND submission=%s AND actor=%s" [Tenant Id (actor Who)])
      (do (sql "INSERT INTO assignment(tenant,submission,actor,delegated_by) VALUES (%s,%s,%s,%s)" [Tenant Id (get Target "actor") (actor Who)])
      (do (sql "UPDATE submission SET version=version+1 WHERE tenant=%s AND id=%s" [Tenant Id])
      (do (audit Tenant (actor Who) "assignment.delegated" Id (obj [["to" (get Target "actor")]]))
      (do (deliver Tenant (token) "delegation" Email "Document review delegated"
             (concat ["Open the delegated review at " (setting "SC_ORIGIN") "/t/" Tenant "/reviews/" Id "."]))
        (obj [["id" Id] ["version" (+ (get Row "version") 1)] ["status" "delegated"]])))))))))))))))

(define audit-after Req -> (if (= (after Req) "") 0 (int (after Req))))

(define audit-log Who Req ->
  (do (permit "audit" Who)
    (rows-page (sql "SELECT id, actor,event,object,detail,at::text FROM audit WHERE tenant=%s AND id>%s AND strpos(lower(event || ' ' || object),lower(%s))>0 ORDER BY id LIMIT 31"
                  [(tenant Who) (audit-after Req) (search Req)]))))

(define export-audit Who Req ->
  (do (permit "audit" Who)
    (response 200 "text/csv; charset=utf-8"
      (csv-table ["id" "at" "actor" "event" "object" "detail"]
        (sql "SELECT id,at::text,actor,event,object,detail FROM audit WHERE tenant=%s AND id>%s AND strpos(lower(event || ' ' || object),lower(%s))>0 ORDER BY id"
          [(tenant Who) (audit-after Req) (search Req)]))
      (obj [["Content-Disposition" "attachment; filename=audit.csv"]]))))

(define deliveries Who Req ->
  (do (permit "tenant-admin" Who)
    (rows-page (sql "SELECT id,kind,recipient,status,attempts,next_at::text,lease_until::text FROM delivery WHERE tenant=%s AND id>%s ORDER BY id LIMIT 31"
                  [(tenant Who) (after Req)]))))

(define redrive Who Params _ ->
  (let Id (get Params "delivery")
  (let Row (one "UPDATE delivery SET status='pending',attempts=0,next_at=now(),lease=NULL,lease_until=NULL WHERE tenant=%s AND id=%s AND status IN ('unknown','failed') RETURNING id" [(tenant Who) Id])
    (do (require Row 409 "delivery-state" "Only failed or unknown deliveries can be retried.")
    (do (audit (tenant Who) (actor Who) "delivery.redriven" Id (obj []))
      (obj [["id" Id] ["status" "pending"]]))))))
