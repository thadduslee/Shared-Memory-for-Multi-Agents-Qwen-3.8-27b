SELECT
  json_extract(p.body, '$.participant') AS user,
  json_extract(p.body, '$.information_scope') AS scope,
  json_extract(p.body, '$.status') AS status,
  json_array_length(p.body, '$.bindings') AS bound_fields,
  p.body AS full_details
FROM permission_records p
WHERE p.kind = 'assignment'
  AND p.version = (
    SELECT MAX(q.version)
    FROM permission_records q
    WHERE q.compartment = p.compartment
      AND q.kind = p.kind AND q.id = p.id
  )
ORDER BY user;
