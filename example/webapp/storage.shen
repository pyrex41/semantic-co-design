(define schema-v1 ->
  "CREATE TABLE IF NOT EXISTS schema_version (version integer PRIMARY KEY, installed_at timestamptz NOT NULL DEFAULT now());
   CREATE TABLE IF NOT EXISTS tenant (slug text PRIMARY KEY, name text NOT NULL);
   CREATE TABLE IF NOT EXISTS identity (actor text PRIMARY KEY, issuer text NOT NULL, subject text NOT NULL,
       email text NOT NULL, UNIQUE (issuer, subject));
   CREATE TABLE IF NOT EXISTS membership (tenant text REFERENCES tenant, actor text REFERENCES identity,
       role text NOT NULL CHECK (role IN ('administrator','author','reviewer','auditor')),
       PRIMARY KEY (tenant, actor));
   CREATE TABLE IF NOT EXISTS invitation (tenant text REFERENCES tenant, email text NOT NULL,
       role text NOT NULL CHECK (role IN ('administrator','author','reviewer','auditor')),
       expires_at timestamptz NOT NULL, consumed_at timestamptz,
       PRIMARY KEY (tenant, email));
   CREATE TABLE IF NOT EXISTS login_intent (state text PRIMARY KEY, verifier text NOT NULL, nonce text NOT NULL,
       expires_at timestamptz NOT NULL);
   CREATE TABLE IF NOT EXISTS session (id text PRIMARY KEY, actor text NOT NULL REFERENCES identity,
       csrf text NOT NULL, expires_at timestamptz NOT NULL);
   CREATE TABLE IF NOT EXISTS workflow (tenant text REFERENCES tenant, version integer CHECK (version > 0),
       source jsonb NOT NULL, artifact jsonb NOT NULL, source_digest text NOT NULL, artifact_digest text NOT NULL,
       published_at timestamptz NOT NULL DEFAULT now(), active boolean NOT NULL,
       PRIMARY KEY (tenant, version));
   CREATE UNIQUE INDEX IF NOT EXISTS workflow_active ON workflow(tenant) WHERE active;
   CREATE TABLE IF NOT EXISTS document (tenant text REFERENCES tenant, id text, author text NOT NULL REFERENCES identity,
       revision integer NOT NULL CHECK (revision > 0), status text NOT NULL CHECK
           (status IN ('draft','in-review','rejected','approved','deleted')),
       updated_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (tenant,id));
   CREATE INDEX IF NOT EXISTS document_queue ON document(tenant, id) WHERE status <> 'deleted';
   CREATE TABLE IF NOT EXISTS revision (tenant text, document text, version integer CHECK (version > 0),
       title text NOT NULL, body text NOT NULL, amount bigint NOT NULL CHECK (amount >= 0),
       created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (tenant,document,version),
       FOREIGN KEY (tenant,document) REFERENCES document(tenant,id));
   CREATE TABLE IF NOT EXISTS attachment (tenant text, id text, document text NOT NULL, revision integer NOT NULL,
       filename text NOT NULL, length bigint NOT NULL CHECK (length >= 0), digest text NOT NULL,
       state text NOT NULL CHECK (state IN ('staged','attached')), expires_at timestamptz NOT NULL,
       PRIMARY KEY (tenant,id), UNIQUE(id), FOREIGN KEY (tenant,document,revision) REFERENCES revision(tenant,document,version));
   CREATE INDEX IF NOT EXISTS attachment_expiry ON attachment(expires_at) WHERE state='staged';
   CREATE TABLE IF NOT EXISTS submission (tenant text, id text, document text NOT NULL, revision integer NOT NULL,
       policy integer NOT NULL, continuation_version integer NOT NULL DEFAULT 1, version integer NOT NULL DEFAULT 1,
       quorum integer NOT NULL CHECK (quorum > 0), status text NOT NULL CHECK
           (status IN ('pending','approved','rejected','expired')),
       due_at timestamptz NOT NULL, PRIMARY KEY (tenant,id), UNIQUE (tenant,document,revision),
       FOREIGN KEY (tenant,document,revision) REFERENCES revision(tenant,document,version),
       FOREIGN KEY (tenant,policy) REFERENCES workflow(tenant,version));
   CREATE INDEX IF NOT EXISTS submission_due ON submission(due_at) WHERE status='pending';
   CREATE TABLE IF NOT EXISTS assignment (tenant text, submission text, actor text REFERENCES identity,
       delegated_by text REFERENCES identity, PRIMARY KEY (tenant,submission,actor),
       FOREIGN KEY (tenant,submission) REFERENCES submission(tenant,id));
   CREATE INDEX IF NOT EXISTS assignment_inbox ON assignment(tenant,actor,submission);
   CREATE TABLE IF NOT EXISTS vote (tenant text, submission text, actor text REFERENCES identity,
       decision text NOT NULL CHECK (decision IN ('approve','reject')), note text NOT NULL,
       at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (tenant,submission,actor),
       FOREIGN KEY (tenant,submission,actor) REFERENCES assignment(tenant,submission,actor));
   CREATE TABLE IF NOT EXISTS audit (tenant text REFERENCES tenant, id bigint GENERATED ALWAYS AS IDENTITY,
       actor text NOT NULL, event text NOT NULL, object text NOT NULL, detail jsonb NOT NULL,
       at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (tenant,id));
   CREATE TABLE IF NOT EXISTS command_receipt (tenant text REFERENCES tenant, actor text REFERENCES identity,
       command text, key text, digest text NOT NULL, result jsonb NOT NULL,
       created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (tenant,actor,command,key));
   CREATE TABLE IF NOT EXISTS delivery (tenant text REFERENCES tenant, id text, kind text NOT NULL,
       recipient text NOT NULL, subject text NOT NULL, body text NOT NULL,
       status text NOT NULL CHECK (status IN ('pending','leased','accepted','unknown','failed')),
       lease text, lease_until timestamptz, next_at timestamptz NOT NULL DEFAULT now(),
       attempts integer NOT NULL DEFAULT 0, PRIMARY KEY (tenant,id));
   CREATE INDEX IF NOT EXISTS delivery_ready ON delivery(next_at) WHERE status IN ('pending','failed','unknown');
   INSERT INTO schema_version(version) VALUES (1) ON CONFLICT DO NOTHING")

(define schema-v2 ->
  "CREATE INDEX IF NOT EXISTS invitation_email ON invitation(email) WHERE consumed_at IS NULL;
   CREATE INDEX IF NOT EXISTS login_expiry ON login_intent(expires_at);
   CREATE INDEX IF NOT EXISTS session_expiry ON session(expires_at);
   INSERT INTO schema_version(version) VALUES (2) ON CONFLICT DO NOTHING")

(define one Query Args -> (at (sql Query Args) 0))

(define audit Tenant Actor Event Object Detail ->
  (sql "INSERT INTO audit(tenant,actor,event,object,detail) VALUES (%s,%s,%s,%s,%s::jsonb)"
       [Tenant Actor Event Object (json Detail)]))

(define deliver Tenant Identity Kind Recipient Subject Body ->
  (sql "INSERT INTO delivery(tenant,id,kind,recipient,subject,body,status) VALUES (%s,%s,%s,%s,%s,%s,'pending') ON CONFLICT DO NOTHING"
       [Tenant (digest [Tenant Identity Kind]) Kind Recipient Subject Body]))

(define exists? Query Args -> (truth (one Query Args)))

(define require Condition Status Code Detail ->
  (if Condition true (failure Status Code Detail)))

(define must Row Code Detail -> (if Row Row (failure 404 Code Detail)))

(define rows-page Rows ->
  (obj [["items" (take Rows 30)]
        ["next" (if (> (len Rows) 30) (get (at Rows 29) "id") null)]]))

(define after Req ->
  (let Cursor (get (get Req "query") "after")
    (if (text? Cursor 1 128) Cursor "")))

(define search Req ->
  (let Query (get (get Req "query") "q") (if (text? Query 1 160) Query "")))

(define expected Row Field Body Input ->
  (require (= (get Row Field) (get Body Input)) 409 "stale-version" "Reload before making this change."))

(define migrate _ ->
  (do (begin)
  (do (sql "SELECT pg_advisory_xact_lock(4268310441)" [])
  (do (sql (schema-v1) [])
  (do (sql (schema-v2) [])
  (do (commit)
      (obj [["schema" 2] ["status" "ready"]])))))))

(define provision Args ->
  (let Tenant (get Args "tenant")
  (let Name (get Args "name")
  (let Email (get Args "email")
    (do (require (and (slug? Tenant) (and (text? Name 1 120) (email? Email)))
                 422 "invalid-tenant" "Supply a tenant slug, name and administrator email.")
    (do (begin)
    (do (require (one "INSERT INTO tenant(slug,name) VALUES (%s,%s) ON CONFLICT DO NOTHING RETURNING slug" [Tenant Name])
                 409 "tenant-exists" "The tenant already exists.")
    (do (invite-row Tenant "operator" (lower Email) "administrator")
    (do (commit)
      (obj [["status" "provisioned"] ["tenant" Tenant]]))))))))))

(define check-health _ ->
  (let Version (get (one "SELECT max(version) AS version FROM schema_version" []) "version")
    (do (require (= Version 2) 503 "schema-version" "Migrate the database to schema version 2.")
        (obj [["status" "ready"] ["schema" Version]]))))

(define operator-doctor _ ->
  (do (begin)
    (let R (check-health null)
      (do (commit) R))))

(define backup Args -> (backup-files (get Args "path")))

(define restore Args -> (restore-files (get Args "path")))
