(define default-workflow ->
  (obj [["threshold" 10000] ["quorum" 1] ["above_quorum" 2] ["delegation" true] ["expires_hours" 72]]))

(define valid-quorum? N -> (and (int? N) (and (>= N 1) (<= N 10))))

(define compile-policy Source ->
  (do (require (obj? Source) 422 "invalid-workflow" "Enter a workflow object.")
  (let Threshold (get Source "threshold")
  (let Low (get Source "quorum")
  (let High (get Source "above_quorum")
  (let Delegation (get Source "delegation")
  (let Hours (get Source "expires_hours")
    (do (require (= (keys Source) ["above_quorum" "delegation" "expires_hours" "quorum" "threshold"])
               422 "workflow-fields" "Supply threshold, quorum, above_quorum, delegation and expires_hours.")
    (do (require (and (and (int? Threshold) (and (>= Threshold 0) (<= Threshold 9223372036854775807)))
                      (and (valid-quorum? Low) (and (valid-quorum? High) (>= High Low))))
               422 "invalid-quorum" "Enter a positive quorum from 1 to 10 and a non-negative threshold.")
    (do (require (and (bool? Delegation) (and (int? Hours) (and (>= Hours 1) (<= Hours 720))))
               422 "invalid-workflow" "Set delegation and an expiry from 1 to 720 hours.")
      (obj [["abi" 1] ["threshold" Threshold] ["quorum" Low] ["above_quorum" High]
            ["delegation" Delegation] ["expires_hours" Hours]])))))))))))

(define required-votes Policy Amount ->
  (if (> Amount (get Policy "threshold")) (get Policy "above_quorum") (get Policy "quorum")))

(define workflow-list Who Req ->
  (do (permit "tenant-admin" Who)
    (rows-page (sql "SELECT version::text AS id,version,source,source_digest,artifact_digest,published_at::text,active FROM workflow WHERE tenant=%s AND version>%s ORDER BY version LIMIT 31"
                 [(tenant Who) (if (= (after Req) "") 0 (int (after Req)))]))))

(define publish-workflow Who _ Body ->
  (let Tenant (tenant Who)
  (let Source (get Body "workflow")
  (let Policy (get Body "internal-artifact")
  (let Version (+ 1 (get (one "SELECT COALESCE(max(version),0) AS version FROM workflow WHERE tenant=%s" [Tenant]) "version"))
    (do (require (= (get Policy "abi") 1) 503 "compiler-abi" "The workflow compiler uses an incompatible artifact format.")
    (do (sql "UPDATE workflow SET active=false WHERE tenant=%s AND active" [Tenant])
    (do (sql "INSERT INTO workflow(tenant,version,source,artifact,source_digest,artifact_digest,active) VALUES (%s,%s,%s::jsonb,%s::jsonb,%s,%s,true)"
          [Tenant Version (json Source) (json Policy) (digest Source) (digest Policy)])
    (do (audit Tenant (actor Who) "workflow.published" (str Version)
          (obj [["source_digest" (digest Source)] ["artifact_digest" (digest Policy)]]))
      (obj [["version" Version] ["artifact_digest" (digest Policy)]]))))))))))

(define document-key Req -> (get (get Req "params") "document"))
(define submission-key Req -> (get (get Req "params") "submission"))

(define document-by-id Who Id ->
  (must (one "SELECT d.id,d.author,d.revision,d.status,r.title,r.body,r.amount FROM document d JOIN revision r ON (r.tenant,r.document,r.version)=(d.tenant,d.id,d.revision) WHERE d.tenant=%s AND d.id=%s AND d.status<>'deleted'"
          [(tenant Who) Id]) "document-missing" "This document is unavailable."))

(define may-read-history Who Row ->
  (or (= (actor Who) (get Row "author")) (permitted? "audit" (role Who))))

(define has-review-revision Who Id Revision ->
  (and (permitted? "review" (role Who))
    (exists? "SELECT 1 FROM submission s JOIN assignment a ON (a.tenant,a.submission)=(s.tenant,s.id) WHERE s.tenant=%s AND s.document=%s AND s.revision=%s AND a.actor=%s"
      [(tenant Who) Id Revision (actor Who)])))

(define may-read-document Who Row ->
  (or (may-read-history Who Row) (has-review-revision Who (get Row "id") (get Row "revision"))))

(define read-doc Who Id ->
  (let Row (document-by-id Who Id)
    (if (may-read-document Who Row) Row (failure 404 "document-missing" "This document is unavailable."))))

(define owned-doc Who Id ->
  (let Row (document-by-id Who Id)
    (if (or (= (actor Who) (get Row "author")) (= (role Who) "administrator"))
        Row
        (failure 404 "document-missing" "This document is unavailable."))))

(define editable Row Body ->
  (do (expected Row "revision" Body "expected_revision")
    (require (contains ["draft" "rejected"] (get Row "status")) 409 "document-locked" "Only drafts and rejected documents can be changed.")))

(define documents Who Req ->
  (rows-page
    (sql "SELECT d.id,d.author,d.revision,d.status,r.title,r.amount FROM document d JOIN revision r ON (r.tenant,r.document,r.version)=(d.tenant,d.id,d.revision) WHERE d.tenant=%s AND d.id>%s AND d.status<>'deleted' AND (d.author=%s OR %s OR EXISTS(SELECT 1 FROM submission s JOIN assignment a ON (a.tenant,a.submission)=(s.tenant,s.id) WHERE s.tenant=d.tenant AND s.document=d.id AND s.revision=d.revision AND a.actor=%s AND %s)) AND strpos(lower(r.title),lower(%s))>0 ORDER BY d.id LIMIT 31"
      [(tenant Who) (after Req) (actor Who) (permitted? "audit" (role Who))
       (actor Who) (permitted? "review" (role Who)) (search Req)])))

(define document-detail Who Req ->
  (let Row (read-doc Who (document-key Req))
  (let Tenant (tenant Who)
  (let Id (get Row "id")
  (let All (may-read-history Who Row)
     (merge Row
       (obj [["revisions" (sql "SELECT version,title,amount,created_at::text FROM revision WHERE tenant=%s AND document=%s AND (%s OR version=%s) ORDER BY version DESC" [Tenant Id All (get Row "revision")])]
             ["files" (sql "SELECT id,revision,filename,length,digest FROM attachment WHERE tenant=%s AND document=%s AND state='attached' AND (%s OR revision=%s) ORDER BY revision,id" [Tenant Id All (get Row "revision")])]
             ["submissions" (sql "SELECT id,revision,policy,version,quorum,status,due_at::text FROM submission WHERE tenant=%s AND document=%s AND (%s OR revision=%s) ORDER BY revision DESC" [Tenant Id All (get Row "revision")])]])))))))

(define document-revision Who Req ->
  (let Doc (document-by-id Who (document-key Req))
  (let Id (get Doc "id")
  (let Revision (int (get (get Req "params") "revision"))
    (do (require (or (may-read-history Who Doc) (has-review-revision Who Id Revision))
                 404 "revision-missing" "This revision is unavailable.")
      (must (one "SELECT version,title,body,amount,created_at::text FROM revision WHERE tenant=%s AND document=%s AND version=%s"
             [(tenant Who) Id Revision]) "revision-missing" "This revision is unavailable."))))))

(define check-draft Body ->
  (require (and (text? (get Body "title") 1 160)
                (and (text? (get Body "body") 1 16000) (>= (get Body "amount") 0)))
           422 "invalid-draft" "Enter a title, document text and a non-negative amount."))

(define revision-insert Tenant Id Version Body ->
  (sql "INSERT INTO revision(tenant,document,version,title,body,amount) VALUES (%s,%s,%s,%s,%s,%s)"
    [Tenant Id Version (trim (get Body "title")) (get Body "body") (get Body "amount")]))

(define create-document Who _ Body ->
  (let Tenant (tenant Who)
  (let Id (token)
    (do (check-draft Body)
    (do (require (< (get (one "SELECT count(*) AS count FROM document WHERE tenant=%s AND status<>'deleted'" [Tenant]) "count") 1000)
                  429 "document-quota" "This tenant has reached its document limit.")
    (do (sql "INSERT INTO document(tenant,id,author,revision,status) VALUES (%s,%s,%s,1,'draft')" [Tenant Id (actor Who)])
    (do (revision-insert Tenant Id 1 Body)
    (do (audit Tenant (actor Who) "document.created" Id (obj [["revision" 1]]))
      (obj [["id" Id] ["revision" 1] ["status" "draft"]])))))))))

(define edit-document Who Params Body ->
  (let Tenant (tenant Who)
  (let Id (get Params "document")
  (let Row (owned-doc Who Id)
  (let Version (+ (get Row "revision") 1)
    (do (editable Row Body)
    (do (check-draft Body)
    (do (require (<= Version 100) 429 "revision-quota" "This document has reached its revision limit.")
    (do (sql "UPDATE document SET revision=%s,status='draft',updated_at=now() WHERE tenant=%s AND id=%s" [Version Tenant Id])
    (do (revision-insert Tenant Id Version Body)
    (do (audit Tenant (actor Who) "document.revised" Id (obj [["revision" Version]]))
      (obj [["id" Id] ["revision" Version] ["status" "draft"]]))))))))))))

(define delete-document Who Params Body ->
  (let Tenant (tenant Who)
  (let Id (get Params "document")
  (let Row (owned-doc Who Id)
    (do (editable Row Body)
    (do (sql "UPDATE document SET status='deleted',updated_at=now() WHERE tenant=%s AND id=%s" [Tenant Id])
    (do (audit Tenant (actor Who) "document.deleted" Id (obj [["revision" (get Row "revision")]]))
      (obj [["id" Id] ["status" "deleted"]]))))))))

(define reserve-file Who Doc FileName Length Digest ->
  (let Tenant (tenant Who)
  (let Id (token)
    (do (require (and (text? FileName 1 180) (and (>= Length 0) (<= Length 4194304)))
                 422 "invalid-file" "Enter a filename and use a file at most 4 MiB.")
    (do (require (sha256? Digest) 422 "invalid-digest" "Enter a SHA-256 digest.")
    (do (require (< (get (one "SELECT count(*) AS count FROM attachment WHERE tenant=%s AND document=%s" [Tenant (get Doc "id")]) "count") 30)
                 429 "file-quota" "This document has reached its attachment limit.")
    (do (sql "INSERT INTO attachment(tenant,id,document,revision,filename,length,digest,state,expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s,'staged',now()+interval '1 day')"
          [Tenant Id (get Doc "id") (get Doc "revision") FileName Length Digest])
      (obj [["id" Id] ["document" (get Doc "id")] ["revision" (get Doc "revision")]
            ["filename" FileName] ["length" Length] ["digest" Digest] ["state" "staged"]]))))))))

(define stage-file Who Params Body ->
  (let Doc (owned-doc Who (get Params "document"))
    (do (editable Doc Body)
      (reserve-file Who Doc (get Body "filename") (get Body "length") (get Body "digest")))))

(define lookup-file Who Doc Id ->
  (must (one "SELECT id,document,revision,filename,length,digest,state FROM attachment WHERE tenant=%s AND document=%s AND id=%s AND (state='attached' OR expires_at>now())"
           [(tenant Who) (get Doc "id") Id])
    "file-missing" "This attachment is unavailable."))

(define lock-file Who Doc Id ->
  (must (one "SELECT id,document,revision,filename,length,digest,state FROM attachment WHERE tenant=%s AND document=%s AND id=%s AND (state='attached' OR expires_at>now()) FOR UPDATE"
           [(tenant Who) (get Doc "id") Id])
    "file-missing" "This attachment is unavailable."))

(define assert-file Info Actual ->
  (require (and (= (get Info "length") (get Actual "length")) (= (get Info "digest") (get Actual "digest")))
    422 "file-mismatch" "The file does not match the recorded length and digest."))

(define write-file Who Params Body ->
  (let Doc (owned-doc Who (get Params "document"))
  (let Info (lock-file Who Doc (get Params "attachment"))
  (let Data (bytes (get Body "file"))
    (do (editable Doc Body)
    (do (require (= (get Info "revision") (get Doc "revision")) 409 "stale-version" "Stage an attachment for the current revision.")
    (do (assert-file Info (obj [["length" (len Data)] ["digest" (digest Data)]]))
    (do (blob-put (get Info "id") Data)
        Info))))))))

(define commit-file Who Info ->
  (do (assert-file Info (blob-check (get Info "id")))
  (do (sql "UPDATE attachment SET state='attached' WHERE tenant=%s AND id=%s AND state='staged'"
         [(tenant Who) (get Info "id")])
  (do (audit (tenant Who) (actor Who) "attachment.added" (get Info "document")
        (obj [["attachment" (get Info "id")] ["revision" (get Info "revision")] ["digest" (get Info "digest")]]))
    (put Info "state" "attached")))))

(define attach-file Who Params Body ->
  (let Doc (owned-doc Who (get Params "document"))
  (let Info (lock-file Who Doc (get Params "attachment"))
    (do (editable Doc Body)
    (do (require (= (get Info "revision") (get Doc "revision")) 409 "stale-version" "Stage an attachment for the current revision.")
      (if (= (get Info "state") "attached") Info (commit-file Who Info)))))))

(define upload-file Who Params Body ->
  (let Doc (owned-doc Who (get Params "document"))
  (let Data (bytes (get Body "file"))
    (do (editable Doc Body)
    (let Info (reserve-file Who Doc (get Body "filename") (len Data) (digest Data))
      (do (blob-put (get Info "id") Data)
          (commit-file Who Info)))))))

(define download-file Who Req ->
  (let Doc (document-by-id Who (document-key Req))
  (let Info (lookup-file Who Doc (get (get Req "params") "attachment"))
    (do (require (and (= (get Info "state") "attached")
             (or (may-read-history Who Doc) (has-review-revision Who (get Doc "id") (get Info "revision"))))
          404 "file-missing" "This attachment is unavailable.")
      (let Data (blob-get (get Info "id"))
        (do (require (and (= (len Data) (get Info "length")) (= (digest Data) (get Info "digest")))
                     503 "file-integrity" "The stored file did not pass integrity verification.")
          (response 200 "application/octet-stream" Data
            (obj [["Content-Disposition" (concat ["attachment; filename*=UTF-8''" (quote (get Info "filename"))])]]))))))))
