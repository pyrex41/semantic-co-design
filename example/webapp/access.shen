(define secure-cookie? -> (not (= (setting "SC_INSECURE_LOCAL") "1")))

(define callback-url -> (concat [(setting "SC_ORIGIN") "/callback"]))

(define begin-login _ ->
  (let State (token)
  (let Verifier (token)
  (let Nonce (token)
  (let URL (authorize-id (callback-url) State Verifier Nonce)
    (do (sql "INSERT INTO login_intent(state,verifier,nonce,expires_at) VALUES (%s,%s,%s,now()+interval '10 minutes')"
          [(digest State) Verifier Nonce])
      (redirect URL [(cookie "auth_state" State 600 (secure-cookie?))])))))))

(define finish-login Req ->
  (let State (get (get Req "query") "state")
  (let Code (get (get Req "query") "code")
  (let Bound (get (get Req "cookies") "auth_state")
    (do (require (and (text? Code 1 4096) (secure= State Bound)) 401 "invalid-login" "Start sign-in again.")
    (let Intent (one "DELETE FROM login_intent WHERE state=%s AND expires_at>now() RETURNING verifier,nonce" [(digest State)])
      (do (require Intent 401 "expired-login" "Start sign-in again.")
      (let Claims (exchange-id (callback-url) Code (get Intent "verifier") (get Intent "nonce"))
      (let Email (get Claims "email")
      (let Issuer (get Claims "issuer")
      (let Subject (get Claims "subject")
      (let Actor (digest [Issuer Subject])
        (do (require (and (get Claims "verified") (email? Email)) 403 "email-unverified" "Sign in with a verified email address.")
        (do (sql "INSERT INTO identity(actor,issuer,subject,email) VALUES (%s,%s,%s,%s) ON CONFLICT (actor) DO UPDATE SET email=excluded.email"
                [Actor Issuer Subject (lower Email)])
        (do (claim-invites Actor (lower Email))
          (let Sid (token)
            (do (sql "INSERT INTO session(id,actor,csrf,expires_at) VALUES (%s,%s,%s,now()+interval '8 hours')"
                   [(digest Sid) Actor (token)])
              (redirect "/" [(cookie "sid" Sid 28800 (secure-cookie?))
                             (cookie "auth_state" "" 0 (secure-cookie?))]))))))))))))))))))

(define claim-invites Actor Email ->
  (let Tenants (sql "SELECT t.slug FROM tenant t JOIN invitation i ON i.tenant=t.slug WHERE i.email=%s AND i.expires_at>now() AND i.consumed_at IS NULL ORDER BY t.slug FOR UPDATE OF t" [Email])
    (claim-each Actor Email Tenants)))

(define claim-each Actor Email Rows ->
  (if (= (len Rows) 0) true
    (let Tenant (get (at Rows 0) "slug")
    (let Invite (one "UPDATE invitation SET consumed_at=now() WHERE tenant=%s AND email=%s AND expires_at>now() AND consumed_at IS NULL RETURNING role" [Tenant Email])
      (do (if Invite
             (do (sql "INSERT INTO membership(tenant,actor,role) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING"
                   [Tenant Actor (get Invite "role")])
                 (audit Tenant Actor "membership.joined" Actor (obj [["role" (get Invite "role")]])))
             false)
          (claim-each Actor Email (drop Rows 1)))))))

(define session-token Req ->
  (let Header (get (get Req "headers") "authorization")
    (if (and (not (get Req "browser")) (and (text? Header 8 256) (= (take Header 7) "Bearer ")))
        (drop Header 7)
        (get (get Req "cookies") "sid"))))

(define maybe-session Req ->
  (let Token (session-token Req)
    (if (text? Token 20 128)
        (one "SELECT s.actor,s.csrf,i.email FROM session s JOIN identity i ON i.actor=s.actor WHERE s.id=%s AND s.expires_at>now()" [(digest Token)])
        null)))

(define authenticated Req ->
  (let Session (maybe-session Req)
    (if Session Session (failure 401 "sign-in-required" "Sign in to continue."))))

(define check-csrf Req Session ->
  (let Header (get (get Req "headers") "authorization")
    (require (or (and (not (get Req "browser")) (and (text? Header 8 256) (= (take Header 7) "Bearer ")))
                 (secure= (get Req "csrf") (get Session "csrf")))
      403 "csrf" "Reload before submitting this form.")))

(define logout Req ->
  (let Session (authenticated Req)
    (do (check-csrf Req Session)
    (do (sql "DELETE FROM session WHERE id=%s" [(digest (session-token Req))])
      (redirect "/" [(cookie "sid" "" 0 (secure-cookie?))])))))

(define current-session Req -> (json-response 200 (authenticated Req)))

(define tenant-id Req -> (get (get Req "params") "tenant"))

(define member Req Write ->
  (let Session (authenticated Req)
  (let Tenant (tenant-id Req)
  (let Lock (if Write
                (one "SELECT slug,name FROM tenant WHERE slug=%s FOR UPDATE" [Tenant])
                (one "SELECT slug,name FROM tenant WHERE slug=%s FOR SHARE" [Tenant]))
  (let Grant (if Lock
                (one "SELECT role FROM membership WHERE tenant=%s AND actor=%s" [Tenant (get Session "actor")])
                null)
    (if Grant (merge Session (merge Grant (obj [["tenant" Tenant] ["name" (get Lock "name")]])))
        (failure 404 "tenant-missing" "This tenant is unavailable.")))))))

(define actor Who -> (get Who "actor"))
(define tenant Who -> (get Who "tenant"))
(define role Who -> (get Who "role"))

(define permitted? "tenant-admin" Role -> (= Role "administrator")
                   "author" Role -> (or (= Role "administrator") (= Role "author"))
                   "audit" Role -> (or (= Role "administrator") (= Role "auditor"))
                   "review" Role -> (= Role "reviewer")
                   "member" _ -> true)

(define permit Permission Who ->
  (require (permitted? Permission (role Who)) 403 "forbidden" "Your current role does not permit this action."))

(define invite-row Tenant Actor Email Role ->
  (do (require (email? Email) 422 "invalid-email" "Enter a valid email address.")
  (do (sql "INSERT INTO invitation(tenant,email,role,expires_at) VALUES (%s,%s,%s,now()+interval '7 days') ON CONFLICT (tenant,email) DO UPDATE SET role=excluded.role,expires_at=excluded.expires_at,consumed_at=NULL"
         [Tenant Email Role])
  (do (audit Tenant Actor "membership.invited" Email (obj [["role" Role]]))
    (deliver Tenant (token) "invitation" Email "Document approval invitation"
      (concat ["Sign in at " (setting "SC_ORIGIN") " with this email address to join " Tenant "."]))))))

(define list-members Who _ ->
  (do (permit "tenant-admin" Who)
    (obj [["members" (sql "SELECT m.actor,m.role,i.email FROM membership m JOIN identity i ON i.actor=m.actor WHERE m.tenant=%s ORDER BY i.email" [(tenant Who)])]
          ["invitations" (sql "SELECT email,role,expires_at::text,consumed_at::text FROM invitation WHERE tenant=%s ORDER BY email" [(tenant Who)])]])))

(define add-invite Who _ Body ->
  (let Email (lower (get Body "email"))
    (do (require (not (exists? "SELECT 1 FROM membership m JOIN identity i ON i.actor=m.actor WHERE m.tenant=%s AND i.email=%s"
                   [(tenant Who) Email]))
           409 "role-exists" "Change the existing member’s role instead.")
    (do (require (< (get (one "SELECT count(*) AS count FROM invitation WHERE tenant=%s AND consumed_at IS NULL AND expires_at>now()" [(tenant Who)]) "count") 100)
           429 "invitation-quota" "This tenant has too many pending invitations.")
    (do (invite-row (tenant Who) (actor Who) Email (get Body "role"))
      (obj [["email" Email] ["role" (get Body "role")]]))))))

(define update-member Who Params Body ->
  (let Target (get Params "actor")
  (let Grant (must (one "SELECT role FROM membership WHERE tenant=%s AND actor=%s" [(tenant Who) Target])
                "member-missing" "The member does not belong to this tenant.")
  (let Next (get Body "role")
    (do (require (or (not (= (get Grant "role") "administrator")) (or (= Next "administrator")
                    (exists? "SELECT 1 FROM membership WHERE tenant=%s AND actor<>%s AND role='administrator'" [(tenant Who) Target])))
          409 "last-admin" "Keep at least one active administrator.")
    (do (if (= Next "revoked")
           (sql "DELETE FROM membership WHERE tenant=%s AND actor=%s" [(tenant Who) Target])
           (sql "UPDATE membership SET role=%s WHERE tenant=%s AND actor=%s" [Next (tenant Who) Target]))
    (do (audit (tenant Who) (actor Who) "membership.changed" Target (obj [["from" (get Grant "role")] ["to" Next]]))
      (obj [["actor" Target] ["role" Next]]))))))))
