# patch_snow_notify

Pushes the post-check aggregate summary back into ServiceNow via the Table
API. **This role is a stub pending your real ServiceNow instance details** --
the request shape below follows the standard SNOW Table API contract, but
the table name, field names, and auth mechanism must be confirmed against
your actual instance before enabling it.

## Why it's a stub

At design time we didn't yet know:
- Your SNOW instance URL
- Which table carries the patch cycle record (`change_request` directly, or
  a child table/related list for per-server patch results)
- Which field(s) should receive the summary (`work_notes`, `comments`, or a
  custom field)
- Auth mechanism (basic auth service account vs OAuth)

## What it does today

With `snow_notify_enabled: false` (the default), this role only logs what
*would* be sent and does nothing else -- safe to leave wired into
`postcheck.yml` right now.

Once enabled (`snow_notify_enabled: true`) with real connection details, it:
1. Resolves the Change Request's `sys_id` from its human-readable `number`
   (`change_id`) via a `GET` with `sysparm_query=number={{ change_id }}`.
2. `PATCH`es that record's `work_notes` with a formatted per-host summary
   table (hostname, precheck/patch/postcheck status, kernel change, new
   failed units, services not recovered).

## Required vars to fill in before enabling

```yaml
snow_notify_enabled: true
snow_instance_url: "https://yourinstance.service-now.com"
snow_change_table: "change_request"      # confirm the actual table name
snow_auth_type: basic                    # or: oauth
snow_username: "{{ vault_snow_username }}"   # store via AAP Credential, not in group_vars
snow_password: "{{ vault_snow_password }}"
# snow_oauth_token: "{{ vault_snow_oauth_token }}"   # if snow_auth_type: oauth
```

Store `snow_username`/`snow_password` (or `snow_oauth_token`) as an AAP
Credential injected into extra_vars or environment, never committed in
group_vars.

## Input contract

Called with `snow_payload` set: a list of per-host summary dicts (see
`roles/patch_postcheck/tasks/main.yml`, the aggregation task).
