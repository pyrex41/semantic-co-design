-- A committed terminal transition has one close event and one delivery intent.
-- The delivery protocol identifies a submission with the SHA-256 of the compact
-- JSON tuple [tenant, submission ID, "terminal"], encoded in lowercase hex.
-- Pending submissions have neither effect. Terminal effects must have a subject.
WITH subjects AS (
    SELECT s.tenant, s.id, s.document, s.revision, s.policy, s.status,
           CASE s.status WHEN 'pending' THEN 0 ELSE 1 END AS effects,
           '/t/' || s.tenant || '/documents/' || s.document || '.' AS delivery_target,
           encode(sha256(convert_to(
               '[' || to_json(s.tenant)::text || ',' || to_json(s.id)::text || ',"terminal"]',
               'UTF8'
           )), 'hex') AS delivery_id
    FROM submission s
),
close_events AS (
    SELECT tenant, object, detail
    FROM audit
    WHERE event = 'submission.closed'
),
intents AS (
    SELECT tenant, id, body
    FROM delivery
    WHERE kind = 'terminal'
),
violations AS (
    SELECT 'close-cardinality' AS violation, s.tenant, s.id AS subject
    FROM subjects s
    WHERE (SELECT count(*) FROM close_events a WHERE (a.tenant, a.object) = (s.tenant, s.id)) <> s.effects

    UNION ALL

    SELECT 'close-metadata', s.tenant, s.id
    FROM subjects s
    JOIN close_events a ON (a.tenant, a.object) = (s.tenant, s.id)
    WHERE NOT a.detail @> jsonb_build_object('status', s.status, 'revision', s.revision, 'workflow', s.policy)

    UNION ALL

    SELECT 'delivery-cardinality', s.tenant, s.id
    FROM subjects s
    WHERE (SELECT count(*) FROM intents i WHERE (i.tenant, i.id) = (s.tenant, s.delivery_id)) <> s.effects

    UNION ALL

    SELECT 'delivery-target', s.tenant, s.id
    FROM subjects s
    JOIN intents i ON (i.tenant, i.id) = (s.tenant, s.delivery_id)
    WHERE right(i.body, length(s.delivery_target)) <> s.delivery_target

    UNION ALL

    SELECT 'orphan-close', a.tenant, a.object
    FROM close_events a
    WHERE NOT EXISTS (SELECT FROM subjects s WHERE (s.tenant, s.id) = (a.tenant, a.object))

    UNION ALL

    SELECT 'orphan-delivery', i.tenant, i.id
    FROM intents i
    WHERE NOT EXISTS (SELECT FROM subjects s WHERE (s.tenant, s.delivery_id) = (i.tenant, i.id))
)
SELECT violation, tenant, subject FROM violations ORDER BY violation, tenant, subject
