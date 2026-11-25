(define h Tag Text -> (element Tag (obj []) Text))
(define link Href Label -> (element "a" (obj [["href" Href]]) Label))
(define section Title Body -> (h "section" [(h "h2" Title) Body]))
(define fine Text -> (element "p" (obj [["class" "detail"]]) Text))
(define tpath Who Tail -> (concat ["/t/" (tenant Who) Tail]))
(define dpath Who Row -> (tpath Who (concat ["/documents/" (get Row "id")])))
(define rpath Who Row -> (tpath Who (concat ["/reviews/" (get Row "id")])))

(define page Title Who Content -> (page-status 200 Title Who Content))

(define page-status Code Title Who Content ->
  (html-response Code
    (element "html" (obj [["lang" "en"]])
      [(h "head"
          [(element "meta" (obj [["charset" "utf-8"]]) [])
           (element "meta" (obj [["name" "viewport"] ["content" "width=device-width, initial-scale=1"]]) [])
           (h "title" (concat [Title " · Document Desk"]))
           (element "link" (obj [["rel" "stylesheet"] ["href" "/assets/approval.css"]]) [])])
       (h "body"
          [(element "a" (obj [["href" "#main"] ["class" "skip"]]) "Skip to content")
           (h "header" [(link "/" "Document Desk") (if Who (fine (get Who "email")) "")])
           (element "div" (obj [["class" (if (tenant Who) "workspace" "workspace landing")]])
             [(if (tenant Who) (navigation Who) "")
              (element "main" (obj [["id" "main"]])
                [(h "h1" Title) Content])])
           (h "footer" (if Who (form "page-logout" (obj []) (obj [["csrf" (get Who "csrf")]]) (obj [])) ""))])])))

(define navigation Who ->
  (element "nav" (obj [["aria-label" "Tenant workspace"]])
    [(h "strong" (get Who "name")) (fine (role Who))
     (h "ul"
        [(h "li" (link (tpath Who "") "Workspace"))
         (h "li" (link (tpath Who "/documents") "Documents"))
         (if (permitted? "review" (role Who)) (h "li" (link (tpath Who "/inbox") "Review inbox")) "")
         (if (permitted? "audit" (role Who)) (h "li" (link (tpath Who "/audit") "Audit history")) "")
         (if (permitted? "tenant-admin" (role Who))
            [(h "li" (link (tpath Who "/members") "Members"))
             (h "li" (link (tpath Who "/workflows") "Workflows"))
             (h "li" (link (tpath Who "/deliveries") "Notifications"))] "")])]))

(define action Name Who Params Data ->
  (form Name (put Params "tenant" (tenant Who)) (obj [["csrf" (get Who "csrf")] ["command_key" (token)]]) Data))

(define home-list Rows ->
  (if (= (len Rows) 0) []
    (let Row (at Rows 0)
      (prepend (h "li" [(link (concat ["/t/" (get Row "slug")]) (get Row "name"))
                       (fine (get Row "role"))])
               (home-list (drop Rows 1))))))

(define browser-home Req ->
  (let Data (home-data Req)
    (if Data
      (page "Your workspaces" Data
        (if (len (get Data "tenants")) (h "ul" (home-list (get Data "tenants")))
          (h "p" "Sign in with the email address on your tenant invitation to join a workspace.")))
      (page "Document approvals" null
        [(h "p" "Draft documents, route them for review and keep a record of each decision.")
         (link "/login" "Sign in")]))))

(define find-form Value ->
  (element "form" (obj [["method" "get"] ["class" "search"]])
    [(element "label" (obj [["for" "search"]]) "Search")
     (element "input" (obj [["id" "search"] ["name" "q"] ["type" "search"] ["value" Value]]) [])
     (element "button" (obj [["type" "submit"]]) "Find")]))

(define page-next Data Req ->
  (let Next (get Data "next")
    (if Next (link (concat ["?after=" (quote Next) "&q=" (quote (search Req))]) "Next page") "")))

(define describe Row ->
  (fine (concat [(get Row "status") " · Revision " (get Row "revision")])))

(define item-list Kind Who Rows ->
  (if (= (len Rows) 0) []
    (let Row (at Rows 0)
      (prepend (h "li" [(link (if (= Kind "document") (dpath Who Row) (rpath Who Row)) (get Row "title"))
                       (describe Row)])
               (item-list Kind Who (drop Rows 1))))))

(define revisions-list Who Id Rows ->
  (if (= (len Rows) 0) []
    (let Row (at Rows 0)
      (prepend (h "li" [(link (tpath Who (concat ["/documents/" Id "/revisions/" (get Row "version")]))
                             (concat ["Revision " (get Row "version")]))
                       (fine (concat [(get Row "title") " · " (get Row "created_at")]))])
               (revisions-list Who Id (drop Rows 1))))))

(define files-list Who Id Rows ->
  (if (= (len Rows) 0) []
    (let Row (at Rows 0)
      (prepend (h "li" [(link (tpath Who (concat ["/documents/" Id "/files/" (get Row "id")])) (get Row "filename"))
                       (fine (concat [(get Row "length") " bytes · Revision " (get Row "revision")]))])
               (files-list Who Id (drop Rows 1))))))

(define submitted-list Who Rows ->
  (if (= (len Rows) 0) []
    (let Row (at Rows 0)
      (prepend (h "li" [(describe Row)
                        (fine (concat ["Workflow " (get Row "policy") " · " (get Row "quorum") " required approval(s)"]))])
               (submitted-list Who (drop Rows 1))))))

(define document-text Row ->
  [(fine (concat ["Amount " (get Row "amount")]))
   (element "p" (obj [["class" "document-text"]]) (get Row "body"))])

(define document-actions Who Row ->
  (let Params (obj [["document" (get Row "id")]])
  (let Values (obj [["expected_revision" (get Row "revision")]])
    (if (and (permitted? "author" (role Who))
             (and (or (= (actor Who) (get Row "author")) (= (role Who) "administrator"))
                  (contains ["draft" "rejected"] (get Row "status"))))
      [(section "Edit draft" (action "page-edit" Who Params (merge Row Values)))
       (section "Add a file" (action "page-upload" Who Params Values))
       (section "Send for review" (if (= (get Row "status") "draft")
         (action "page-submit" Who Params Values) (h "p" "Save a new revision before resubmitting.")))
       (section "Delete draft" (action "page-delete" Who Params Values))]
      ""))))

(define member-list Who Rows ->
  (if (= (len Rows) 0) []
    (let Row (at Rows 0)
      (prepend (h "li" [(h "strong" (get Row "email")) (fine (get Row "role"))
            (action "page-role" Who (obj [["actor" (get Row "actor")]]) (obj [["role" (get Row "role")]]))])
            (member-list Who (drop Rows 1))))))

(define invite-list Rows ->
  (if (= (len Rows) 0) []
    (let Row (at Rows 0)
      (prepend (h "li" [(h "strong" (get Row "email"))
         (fine (concat [(get Row "role") " · " (if (get Row "consumed_at") "Accepted" (concat ["Expires " (get Row "expires_at")]))]))])
        (invite-list (drop Rows 1))))))

(define workflow-items Rows ->
  (if (= (len Rows) 0) []
    (let Row (at Rows 0)
      (prepend (h "li" [(h "strong" (concat ["Version " (get Row "version") (if (get Row "active") " · Active" "")]))
          (fine (get Row "published_at")) (h "pre" (json (get Row "source")))])
          (workflow-items (drop Rows 1))))))

(define audit-items Rows ->
  (if (= (len Rows) 0) []
    (let Row (at Rows 0)
      (prepend (h "li" [(h "strong" (get Row "event"))
          (fine (concat [(get Row "at") " · " (get Row "object")]))
          (h "pre" (json (get Row "detail")))])
          (audit-items (drop Rows 1))))))

(define delivery-items Who Rows ->
  (if (= (len Rows) 0) []
    (let Row (at Rows 0)
      (prepend (h "li" [(h "strong" (get Row "recipient"))
          (fine (concat [(get Row "kind") " · " (get Row "status") " · Attempts " (get Row "attempts")]))
          (if (contains ["unknown" "failed"] (get Row "status"))
              (action "page-redrive" Who (obj [["delivery" (get Row "id")]]) (obj [])) "")])
          (delivery-items Who (drop Rows 1))))))

(define review-actions Who Row ->
  (let Params (obj [["submission" (get Row "id")]])
  (let Values (obj [["expected_version" (get Row "version")]])
    (if (and (= (get Row "status") "pending") (and (not (get Row "own_vote")) (not (get Row "overdue"))))
      [(section "Record a decision" (action "page-vote" Who Params Values))
       (if (get (get Row "artifact") "delegation")
           (section "Delegate this review" (action "page-delegate" Who Params Values)) "")]
      (fine (if (get Row "own_vote")
                (concat ["Your decision was " (get Row "own_vote") "."])
                "This review is closed."))))))

(define browser-view "workspace" Who Data _ ->
    (page (get Data "name") Who
       [(fine (concat ["Your role is " (role Who) "."]))
        (h "p" (link (tpath Who "/documents") "Browse documents"))
        (if (permitted? "review" (role Who)) (h "p" (link (tpath Who "/inbox") "Open review inbox")) "")])
  "members" Who Data _ ->
    (page "Members" Who
       [(section "Invite a member" (action "page-invite" Who (obj []) (obj [])))
        (section "Current members" (h "ul" (member-list Who (get Data "members"))))
        (section "Invitations" (h "ul" (invite-list (get Data "invitations"))))])
  "workflows" Who Data Req ->
    (page "Workflows" Who
      [(section "Publish a workflow" (action "page-publish" Who (obj []) (obj [["workflow" (default-workflow)]])))
       (section "Published versions" (h "ul" (workflow-items (get Data "items"))))
       (page-next Data Req)])
  "documents" Who Data Req ->
    (page "Documents" Who
      [(if (permitted? "author" (role Who)) (link (tpath Who "/documents/new") "New document") "")
       (find-form (search Req))
       (if (len (get Data "items")) (h "ul" (item-list "document" Who (get Data "items")))
          (h "p" "No documents match this view."))
       (page-next Data Req)])
  "new" Who _ _ ->
    (page "New document" Who (action "page-create" Who (obj []) (obj [])))
  "document" Who Data _ ->
    (page (get Data "title") Who
      [(describe Data) (document-text Data)
       (section "Files" (h "ul" (files-list Who (get Data "id") (get Data "files"))))
       (section "Revision history" (h "ul" (revisions-list Who (get Data "id") (get Data "revisions"))))
       (section "Review history" (h "ul" (submitted-list Who (get Data "submissions"))))
       (document-actions Who Data)])
  "revision" Who Data _ ->
    (page (concat [(get Data "title") " · Revision " (get Data "version")]) Who (document-text Data))
  "inbox" Who Data Req ->
    (page "Review inbox" Who
      [(find-form (search Req))
       (if (len (get Data "items")) (h "ul" (item-list "review" Who (get Data "items")))
           (h "p" "No reviews match this view."))
       (page-next Data Req)])
  "review" Who Data _ ->
    (page (get Data "title") Who
      [(describe Data) (document-text Data)
       (fine (concat ["Workflow " (get Data "policy") " · " (get Data "quorum") " required approval(s) · Due " (get Data "due_at")]))
       (section "Files" (h "ul" (files-list Who (get Data "document") (get Data "files"))))
       (review-actions Who Data)])
  "audit" Who Data Req ->
    (page "Audit history" Who
      [(link (tpath Who (concat ["/audit/export?q=" (quote (search Req))])) "Download CSV")
       (find-form (search Req))
       (h "ol" (audit-items (get Data "items")))
       (page-next Data Req)])
  "deliveries" Who Data Req ->
    (page "Notifications" Who [(h "ul" (delivery-items Who (get Data "items"))) (page-next Data Req)]))

(define get-page Tag Req ->
  (let Who (member Req false)
    (browser-view Tag Who (read-data Tag Who Req) Req)))


(define page-error Info ->
  (page-status (get Info "status") (get Info "title") null
    [(element "p" (obj [["role" "alert"]]) (get Info "detail"))
     (link (if (get (get Info "params") "tenant")
               (concat ["/t/" (get (get Info "params") "tenant")]) "/") "Return to workspace")]))
