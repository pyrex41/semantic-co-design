(define home-data Req ->
  (let Session (maybe-session Req)
    (if Session (merge Session
       (obj [["tenants" (sql "SELECT t.slug,t.name,m.role FROM tenant t JOIN membership m ON m.tenant=t.slug WHERE m.actor=%s ORDER BY t.name,t.slug" [(actor Session)])]]))
       null)))

(define workspace-data Who _ ->
  (obj [["tenant" (tenant Who)] ["name" (get Who "name")] ["role" (role Who)]]))

(define operation-permission "invite" -> "tenant-admin"
                             "role" -> "tenant-admin"
                             "publish" -> "tenant-admin"
                             "redrive" -> "tenant-admin"
                             "vote" -> "review"
                             "delegate" -> "review"
                             _ -> "author")

(define operation "invite" Who Params Body -> (add-invite Who Params Body)
                  "role" Who Params Body -> (update-member Who Params Body)
                  "publish" Who Params Body -> (publish-workflow Who Params Body)
                  "create" Who Params Body -> (create-document Who Params Body)
                  "edit" Who Params Body -> (edit-document Who Params Body)
                  "delete" Who Params Body -> (delete-document Who Params Body)
                  "submit" Who Params Body -> (submit-document Who Params Body)
                  "stage" Who Params Body -> (stage-file Who Params Body)
                  "write" Who Params Body -> (write-file Who Params Body)
                  "attach" Who Params Body -> (attach-file Who Params Body)
                  "upload" Who Params Body -> (upload-file Who Params Body)
                  "vote" Who Params Body -> (cast-vote Who Params Body)
                  "delegate" Who Params Body -> (delegate-review Who Params Body)
                  "redrive" Who Params Body -> (redrive Who Params Body))

(define receipt-scope "vote" Who Params -> (review-row Who (get Params "submission"))
                      "delegate" Who _ -> (permit "review" Who)
                      "edit" Who Params -> (owned-doc Who (get Params "document"))
                      "submit" Who Params -> (owned-doc Who (get Params "document"))
                      "stage" Who Params -> (owned-doc Who (get Params "document"))
                      "write" Who Params -> (owned-doc Who (get Params "document"))
                      "attach" Who Params -> (owned-doc Who (get Params "document"))
                      "upload" Who Params -> (owned-doc Who (get Params "document"))
                      _ _ _ -> true)

(define file-payload? "write" -> true
                      "upload" -> true
                      _ -> false)

(define payload-digest Tag Params Body ->
  (digest [Params (if (file-payload? Tag) (put Body "file" (digest (bytes (get Body "file")))) Body)]))

(define command-response "invite" Tenant _ _ -> (concat ["/t/" Tenant "/members"])
                         "role" Tenant _ _ -> (concat ["/t/" Tenant "/members"])
                         "publish" Tenant _ _ -> (concat ["/t/" Tenant "/workflows"])
                         "delete" Tenant _ _ -> (concat ["/t/" Tenant "/documents"])
                         "vote" Tenant _ _ -> (concat ["/t/" Tenant "/inbox"])
                         "delegate" Tenant _ _ -> (concat ["/t/" Tenant "/inbox"])
                         "redrive" Tenant _ _ -> (concat ["/t/" Tenant "/deliveries"])
                         "create" Tenant _ Result -> (concat ["/t/" Tenant "/documents/" (get Result "id")])
                         _ Tenant Params _ -> (concat ["/t/" Tenant "/documents/" (get Params "document")]))

(define result-code "create" -> 201
                    "publish" -> 201
                    "stage" -> 201
                    "upload" -> 201
                    _ -> 200)

(define reply Tag Req Tenant Params Result ->
  (if (get Req "browser")
      (redirect (command-response Tag Tenant Params Result) [])
      (json-response (result-code Tag) Result)))

(define command-context Tag Req ->
  (let Who (member Req true)
  (let Tenant (tenant Who)
  (let Actor (actor Who)
  (let Key (get Req "key")
  (let Params (get Req "params")
  (let Body (get Req "body")
    (do (permit (operation-permission Tag) Who)
    (do (check-csrf Req Who)
    (do (require (text? Key 8 128) 422 "command-key" "Supply an idempotency key from 8 to 128 characters.")
    (let Digest (payload-digest Tag Params Body)
      (obj [["who" Who] ["tenant" Tenant] ["actor" Actor] ["key" Key] ["params" Params] ["body" Body]
            ["digest" Digest] ["receipt"
              (one "SELECT digest,result FROM command_receipt WHERE tenant=%s AND actor=%s AND command=%s AND key=%s"
                [Tenant Actor Tag Key])]]))))))))))))

(define replay-command Tag Req State ->
  (let Receipt (get State "receipt")
  (let Params (get State "params")
    (do (require (= (get Receipt "digest") (get State "digest")) 409
          "idempotency-conflict" "This command key has already been used with a different request.")
    (do (receipt-scope Tag (get State "who") Params)
      (reply Tag Req (get State "tenant") Params (get Receipt "result")))))))

(define mutation Tag Req ->
  (let State (command-context Tag Req)
    (if (get State "receipt") (replay-command Tag Req State)
      (let Params (get State "params")
      (let Tenant (get State "tenant")
      (let Body (get State "body")
      (let RunBody (if (= Tag "publish") (put Body "internal-artifact" (get Req "compiled-policy")) Body)
      (let Result (operation Tag (get State "who") Params RunBody)
        (do (sql "INSERT INTO command_receipt(tenant,actor,command,key,digest,result) VALUES (%s,%s,%s,%s,%s,%s::jsonb)"
              [Tenant (get State "actor") Tag (get State "key") (get State "digest") (json Result)])
            (reply Tag Req Tenant Params Result))))))))))

(define publish-command Req ->
  (do (begin)
    (let State (command-context "publish" Req)
      (if (get State "receipt")
          (let Replay (replay-command "publish" Req State) (do (commit) Replay))
          (do (commit)
            (let Policy (compile-rule (get (get State "body") "workflow"))
              (do (begin)
                (let Result (mutation "publish" (put Req "compiled-policy" Policy))
                  (do (commit) Result)))))))))

(define read-data "workspace" Who Req -> (workspace-data Who Req)
                  "members" Who Req -> (list-members Who Req)
                  "workflows" Who Req -> (workflow-list Who Req)
                  "documents" Who Req -> (documents Who Req)
                  "new" Who _ -> (permit "author" Who)
                  "document" Who Req -> (document-detail Who Req)
                  "revision" Who Req -> (document-revision Who Req)
                  "inbox" Who Req -> (review-inbox Who Req)
                  "review" Who Req -> (review-page-data Who Req)
                  "audit" Who Req -> (audit-log Who Req)
                  "deliveries" Who Req -> (deliveries Who Req))

(define get-api Tag Req -> (json-response 200 (read-data Tag (member Req false) Req)))

(define get-file Req -> (download-file (member Req false) Req))
(define get-export Req -> (export-audit (member Req false) Req))
(define get-schema _ -> (json-response 200 (api-schema)))
(define get-health Req -> (json-response 200 (check-health Req)))
(define get-metrics _ -> (response 200 "text/plain; charset=utf-8" (metrics) (obj [])))
