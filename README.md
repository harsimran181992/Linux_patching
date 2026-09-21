# RHEL 8/9 Patching Automation

Three independent AAP job templates, triggered by ServiceNow, for pre-check,
patch apply, and post-check. See the repo's design discussion for the full
rationale; this file covers what you need to operate and extend it.

## Layout

```
patching/
├── ansible.cfg              # only used for standalone `ansible-playbook` runs from here
├── inventory/
│   └── hosts.placeholder.yml
└── playbooks/
    ├── precheck.yml / patch.yml / postcheck.yml   # → AAP job templates
    ├── group_vars/           # all.yml, rhel8.yml, rhel9.yml
    └── roles/
        ├── patch_common, patch_facts_collect, patch_precheck,
        └── patch_apply, patch_postcheck, patch_snow_notify
```

`roles/` and `group_vars/` deliberately sit **inside `playbooks/`, next to the
playbook files themselves** -- not at the `patching/` root. This is not
cosmetic: see "AAP path resolution" below for why the root-level layout
actively breaks under AAP/ansible-runner.

## Architecture

```
ServiceNow                    AAP Controller                 Managed RHEL 8/9 hosts
───────────                   ───────────────                ───────────────────────
CAB approval    ──launch──▶   playbooks/precheck.yml   ──▶   writes
                               extra_vars: change_id,          /var/log/patch_mgmt/<change_id>/
                               target_hosts[]                         precheck.json

Patch window    ──launch──▶   playbooks/patch.yml      ──▶   reads precheck.json (safety gate),
  start                       (same change_id/target_hosts)          applies dnf update, writes
                                                                patch_summary.json

Patch window    ──launch──▶   playbooks/postcheck.yml  ──▶   reads precheck.json +
  end                         (same change_id/target_hosts)          patch_summary.json, re-collects
                                                                facts, writes postcheck.json (raw
                                                                snapshot) then comparison.json (the diff)
                ◀─REST push (patch_snow_notify)──────────    (aggregated once per run,
                                                                not once per host)
```

## extra_vars contract (all 3 playbooks)

```json
{
  "change_id": "CHG0012345",
  "target_hosts": [
    {"hostname": "web01.example.com", "ip": "10.1.2.3"},
    {"hostname": "web02.example.com", "ip": "10.1.2.4"}
  ]
}
```

- `change_id` is the ServiceNow Change Request/Task number. It is the
  correlation key across the three independent launches and namespaces the
  on-host state directory -- required on every launch, every playbook
  `assert`s on it up front.
- `target_hosts` is never read from a static AAP inventory. Each playbook's
  first play (`localhost`) turns this list into an in-memory
  `dynamic_targets` group via `add_host`
  (`playbooks/roles/patch_common/tasks/bootstrap_targets.yml`). The job
  template's Machine credential still applies (credentials attach per-job,
  not per-inventory-host), so this works without the hosts ever existing in
  a curated inventory.
- The attached inventory (`inventory/hosts.placeholder.yml`) only exists
  because AAP requires one on every job template -- it is never used to
  target real hosts, and in practice AAP resolves `localhost` for play 1
  regardless of what that inventory contains.

## AAP path resolution (read this before moving anything)

**AAP/ansible-runner does not run from inside `patching/`, and does not
discover `patching/ansible.cfg`.** In production ansible-runner invokes
roughly `ansible-playbook patching/playbooks/precheck.yml -i <aap-inventory>`
with the working directory at the *project root* (`/runner/project`), not
`patching/`. Ansible's cwd-relative `ansible.cfg` auto-discovery only checks
the current directory, so a `patching/ansible.cfg` is silently never loaded
in that context -- every setting in it (`roles_path`, `inventory`) is
ignored. This was reproduced directly against this exact layout:

```
ERROR! the role 'patch_common' was not found in
/runner/project/patching/playbooks/roles:/runner/requirements_roles:
/runner/.ansible/roles:/usr/share/ansible/roles:/etc/ansible/roles:
/runner/project/patching/playbooks
```

Note what *is* in that search list: `<the running playbook's own
directory>/roles`. That's Ansible's built-in, config-independent role
search path -- it doesn't need `ansible.cfg`, `ANSIBLE_ROLES_PATH`, or any
particular cwd. `group_vars`/`host_vars` have the same built-in fallback
relative to the playbook's own directory. So the fix (and the reason
`roles/` and `group_vars/` live inside `playbooks/` rather than at the
`patching/` root) is to rely on that built-in behavior instead of on
`ansible.cfg` being found at all. Verified by reproducing the AAP invocation
pattern exactly (cwd at the project root, explicit `-i`, zero `ansible.cfg`
present anywhere) against this layout -- both the role and group_vars loaded
correctly.

**Practical implication**: if you ever restructure this further, keep
`roles/` and `group_vars/` as *direct children of whichever directory holds
the `.yml` playbook files AAP's job templates point at*. Don't rely on
`patching/ansible.cfg` for anything that must work under AAP -- it's only
reliable for standalone `ansible-playbook` runs launched with `patching/` as
cwd (e.g. local testing).

## State handoff between the 3 independent runs

Pre-check, patch, and post-check are separate job launches, possibly hours
or days apart, and may run in ephemeral AAP execution-environment
containers -- nothing written to `localhost` in one job is guaranteed to
survive for the next. The managed hosts, however, are persistent real
servers that post-check always targets identically to pre-check. So each
host is the durable handoff point:

```
/var/log/patch_mgmt/<change_id>/
├── precheck.json       # written by precheck.yml -- raw pre-patch snapshot
│                          (+ precheck's own findings: hard_blockers, status, ...)
├── patch_summary.json  # written by patch.yml -- what was applied, reboot outcome
├── postcheck.json      # written by postcheck.yml -- raw post-patch snapshot,
│                          same shape as precheck.json, no comparison data
└── comparison.json     # written by postcheck.yml -- the diff: every static
                           component with a matched: true/false verdict (a
                           full checklist, not just the mismatches) and, for
                           each mismatch, a field-level field_changes list
                           (e.g. "disks.devices.sda.size: 100GB -> 120GB"
                           rather than a full pre/post dump of the whole
                           component), plus the changed-item validation and
                           the overall status
```

`postcheck.json` and `comparison.json` are deliberately two separate files,
not one merged document: `postcheck.json` is a pure, reusable snapshot (so
it stays comparable to `precheck.json` on its own terms, and so the
comparison logic can be re-run against the two raw snapshots later without
touching the host again if the logic itself changes), while `comparison.json`
is purely the result of comparing the two.

Retention: **never auto-deleted** (per design decision) -- this becomes a
permanent per-host audit trail of every patch cycle. Footprint is small
(under a couple hundred KB of JSON per cycle, mostly the package inventory,
duplicated across precheck/postcheck/comparison).

`playbooks/roles/patch_common/tasks/state_write.yml` and `state_read.yml`
are the only two places that touch this directory -- both are parameterized
includes, not duplicated per role.

## What gets compared, and how (patch_facts_collect)

`playbooks/roles/patch_facts_collect` is the single source of truth for
"what do we capture about a host" -- called identically by `patch_precheck`
(baseline) and `patch_postcheck` (comparison), so the two can never drift
apart. Output is split into:

- **`static`**: expected to be IDENTICAL pre vs post. Any difference is a
  drift finding, tiered by `playbooks/group_vars/all.yml` →
  `patch_check_severity` (critical / warning / informational). Components:
  hostname, cpu, memory, swap, filesystems, disks, blkid, lvm, network,
  routes, dns, hosts_file, ntp, services (**running** state -- not unit-file
  enablement, so a service that's enabled but crashed is caught as a
  regression, which enablement alone would miss), selinux, fstab, sysctl
  (curated allowlist), repos.
- **`changed`**: expected TO DIFFER -- used to validate patching/reboot
  actually took effect, not to flag drift. Components: kernel, os_version,
  uptime_seconds, packages. `uptime_seconds` specifically is excluded from
  the "did patching actually do anything" check below (see
  `patch_postcheck`) since it differs on essentially every run regardless
  of whether anything was actually patched.

### Patch-effect validation ("uptime alone isn't success")

Beyond the static/changed comparison, `patch_postcheck` also checks whether
patching had any *real* effect: it diffs `kernel`, `os_version`, and
`packages` (every `changed`-category field except `uptime_seconds`, using
the same field-level `patch_deep_diff` as the static comparison) between
pre-check and post-check. If none of those differ -- i.e. `uptime_seconds`
is the *only* thing that changed -- the run is treated as a **failure**,
not a quiet success: a reboot happening without anything actually being
patched (or a `dnf` run that silently found nothing to do) is exactly what
this pipeline exists to catch, not something to let through with a green
status. Recorded in `comparison.json`'s `changed_validation` as
`patch_had_real_effect` (bool) and `changed_fields` (the actual diff, empty
when this fires).

**Known edge case**: a host that's already fully up to date when the patch
cycle runs (nothing available to install) will also trip this -- there's no
signal in scope today to distinguish "dnf ran and found nothing to apply"
from "dnf never ran at all". If that turns out to matter in practice,
`patch_apply` already knows the difference (`dnf_changed` in
`patch_summary.json`) and that could be threaded through as an explicit
exception later.

### Field-level diff (`patch_deep_diff`)

`playbooks/roles/patch_postcheck/filter_plugins/deep_diff.py` is a small,
pure-stdlib Python filter plugin (no external dependencies -- it runs on
the controller, never shipped to a managed host) that recursively compares
two pre-check/post-check values and returns one record per actual change,
e.g. `{"path": "devices.sda.size", "change": "changed", "pre": "100GB",
"post": "120GB"}` -- instead of `comparison.json` dumping the entire `disks`
component twice for a one-field change. It's registered automatically the
moment `patch_postcheck` is used (Ansible's standard role-scoped
`filter_plugins/` auto-discovery -- same mechanism, same config-independence
as `tasks/`/`defaults/`), and is called once per static component in
`patch_postcheck/tasks/main.yml`.

It handles the different shapes in our schema:
- **dict → recurse per key**, reporting added/removed keys and diffing
  common ones (covers `disks.devices`, `blkid.devices`, `lvm.lvm_facts`,
  `network.per_interface`, and every flat component like `hostname`/`selinux`).
- **list of dicts at a configured path → diffed by identity key**, not
  position, so reordering never shows as a change (`filesystems.mounts` is
  the only one today, keyed by `mount` -- see `LIST_IDENTITY_KEYS` in the
  filter if a future component needs the same treatment).
- **any other list → diffed as a set** (added/removed values) -- correct
  for plain string lists (`network.interfaces`, `services.running`) and a
  safe explicit fallback for anything unconfigured, rather than a silent
  wrong guess.
- **scalars, or a type mismatch between pre and post** (e.g. `lvm.lvm_facts`
  is the literal string `"N/A"` on one side and a dict on the other when
  LVM availability itself changed) → direct compare, reported whole.

Unit-tested directly (pure Python, no Ansible needed) against every shape
above, including the real device data from a production host's blkid
reordering (confirmed empty diff) and a simulated `sr0` size/sector change
(confirmed a precise field-level record) before being wired in.

### Grounding notes (verified against installed ansible-core 2.19 source/docs, not assumed)

Beyond module/command research, every playbook and role here was actually
**executed** locally (ansible-core 2.19 -- against localhost with fabricated
register data standing in for the pieces this dev sandbox doesn't have,
`dnf` and a real RHEL kernel) as a developer/tester/validator loop, not just
syntax-checked. That caught several bugs pure code review would have missed:

- **AAP doesn't discover `patching/ansible.cfg`, breaking role/group_vars
  resolution.** See "AAP path resolution" above -- the headline finding of
  this round, caught from a real job-template run, not local testing (local
  testing always ran with `patching/` as cwd, which hid it).
- **A deliberate "fail this host" task placed inside the same `block:` as
  the file it just wrote silently destroyed that file's detail.** Both
  `patch_precheck` and `patch_postcheck` write their result file, then
  (when a hard blocker / critical drift was found) fail on purpose so the
  host is marked failed for the batch. That deliberate failure is still a
  task failure, so it was caught by the *same* `rescue:` meant for genuine
  unexpected errors -- which then overwrote the just-written, detailed
  `precheck.json`/`comparison.json` with a bare `{status: failed, error:
  <msg>}` record, discarding `hard_blockers`/`static_comparison` right when
  they mattered most. Caught by writing the file, deliberately triggering
  the failure, and inspecting what actually landed on disk (228 bytes
  instead of the expected several KB) -- reproduced, not assumed. Fixed by
  moving both "fail this host" tasks to be siblings *after* their
  block/rescue rather than the last step inside the block, so the
  deliberate failure no longer routes through the same rescue.
- **`blkid -o export`'s device enumeration order is not stable between two
  separate runs**, confirmed against a real production RHEL host (same 3
  devices, same UUIDs, reordered between pre-check and post-check with zero
  actual change) -- the same class of bug as the `ansible_facts.mounts`
  ordering issue below, just not caught in local testing since this dev
  sandbox's `blkid` returns nothing to iterate over. Fixed by parsing the
  blank-line-separated `KEY=VALUE` blocks into a dict keyed by device
  (`{device: {key: value}}`) instead of comparing the raw multi-line text,
  the same way `ansible_facts.lvm` already was.
- **Optical/CD-ROM drives (`sr*`) report size/sector figures that vary with
  attached media state**, not host configuration -- confirmed against the
  same real host: a VMware virtual CD-ROM (`sr0`) went from
  628KB/2048B-sectors to 1024MB/512B-sectors between pre-check and
  post-check with no actual config change, producing a false "critical"
  `disks` drift finding. Excluded from `static.disks` by the universal
  Linux SCSI CD-ROM naming convention (`^sr[0-9]+$`) rather than chasing
  hypervisor-specific vendor/model strings.
- `serial`/`max_fail_percentage` on a play targeting a **runtime-created**
  group (`dynamic_targets`, added via `add_host`) do not reliably resolve a
  bare `"{{ group_vars_var }}"` at the point those two keywords are
  templated -- reproduced directly (`max_fail_percentage` raised
  `'...' is undefined` even with `group_vars/all.yml` correctly loaded and
  `serial` on the very same play working). Fixed by referencing
  `hostvars['localhost'].patch_batch_serial` /
  `hostvars['localhost'].patch_max_fail_percentage` instead -- localhost is
  a real, statically-known inventory host so its group_vars are reliably
  merged at the point play keywords are evaluated.
  **Known false positive**: `ansible-playbook --syntax-check` (and
  therefore `ansible-lint`) still reports `max_fail_percentage: 'hostvars'
  is undefined` on all three playbooks, because `--syntax-check` never
  actually runs play 1's `add_host` task, so `hostvars` itself is never
  populated at check time. A real run does not hit this -- verified
  directly, repeatedly, against this exact code. Treat these 3 specific
  findings as expected in CI; do not "fix" them by reverting to a bare
  `"{{ var }}"` (that reintroduces the real bug above) or by broadly
  skipping the `syntax-check` rule family (that would also hide a genuine
  future syntax error).
- `hosts` was rejected as an extra_vars name --
  `ansible-playbook --syntax-check` flags it ("Found variable using
  reserved name 'hosts'"); renamed to `target_hosts` throughout.
- `ansible_facts.services.values() | selectattr('status', 'equalto', ...)`
  raised `object of type 'dict' has no attribute 'status'` against real
  facts -- the service_facts RETURN doc already said `status` is only
  returned for systemd/RedHat-flavored sysvinit/upstart/OpenBSD entries,
  and this reproduced it: some entries genuinely lack the key. Fixed with
  `selectattr('status', 'defined')` first.
- **`ansible_facts.mounts` list order is not stable between two separate
  runs on an unchanged host** -- confirmed by running pre-check then
  post-check back to back with zero actual config change: the same 6
  mounts came back in a different order (2 positions swapped), which
  `get_mount_facts`'s use of a `ThreadPoolExecutor` for the per-mount
  size/uuid lookups explains. A naive list `!=` comparison would have
  flagged a false "critical" filesystem drift on every single patch cycle.
  Fixed with `sort(attribute='mount')` before storing.
- `ansible_facts.lvm.{vgs,pvs,lvs}` entries carry live capacity figures
  (`size_g`/`free_g`) that drift with ordinary disk usage, the same class
  of problem as the mounts fix above -- trimmed to structural identity only
  (which PV/VG/LV exists and how they relate) before storing.
- `timedatectl show` / `dnf repolist -v` / `systemctl --failed` were
  switched to `failed_when: false` after reproducing real (if
  environment-specific) failures for each; all three are informational/
  baseline captures, not hard blockers, so a transient failure to gather
  them shouldn't fail a whole host's pre-check or post-check.
- A string/int comparison crashed `_reboot_sanity_ok` when
  `patch_reboot_uptime_sanity_seconds` arrived as a string (e.g. via
  `-e var=60`, which is how extra_vars CLI/survey input commonly arrives) --
  fixed with an explicit `| int` on both sides of the comparison.

Also verified (module/command research, not executed here for lack of a
real RHEL box or `dnf`):

- `ansible_facts.services` requires the dedicated `service_facts` module --
  it is **not** produced by `setup`'s `gather_subset`.
- `ansible_facts.selinux.status` can be the literal string
  `"Missing selinux Python library"` when `python3-libselinux` isn't
  installed on the target ([Red Hat](https://access.redhat.com/solutions/5674911)).
  Falls back to `getenforce`.
- `ansible_facts.dns` is nothing more than a parse of `/etc/resolv.conf`;
  we checksum the file directly instead (one source of truth, byte-exact),
  with `follow: true` since it's often a systemd-resolved symlink
  (`ansible.builtin.stat`'s `follow` defaults to `false`).
- `ansible_facts.mounts[].size_total`/`size_available` are raw bytes
  (`statvfs` `f_frsize * f_blocks`/`f_bavail`), not MB -- confirmed from
  `ansible/module_utils/facts/utils.py`.
- `dnf check-update` exit codes: `0` = no updates, `100` = updates
  available (the normal case), `1` = real error
  ([dnf docs](https://dnf.readthedocs.io/en/latest/command_ref.html)).
- `needs-restarting -r` exit codes: `1` = reboot required, `0` = not
  required ([dnf-plugins-core docs](https://dnf-plugins-core.readthedocs.io/en/latest/needs_restarting.html)).
- `ansible.builtin.dnf`'s `security`/`bugfix`/`update_only` params (used
  for scoped patching) and `ansible.builtin.group_by` (used to load
  `group_vars/rhel8.yml`/`rhel9.yml` after runtime OS detection) confirmed
  via `ansible-doc` against the installed module source.

## RHEL version support

RHEL 8 and 9 only (confirmed scope).
`playbooks/roles/patch_common/tasks/assign_os_group.yml` asserts this and
fails fast on anything else.

**Extending to RHEL10 later**: the package-manager call is dispatched by
variable (`patch_pkg_manager`, set per `group_vars/rhelN.yml`) through
`playbooks/roles/patch_apply/tasks/pkg_mgr/{{ patch_pkg_manager }}.yml`
specifically so this is additive, not a rewrite. RHEL10 defaults to `dnf5`
(a different API than the `dnf4`-targeting `ansible.builtin.dnf` module), so
adding it means: `group_vars/rhel10.yml` with `patch_pkg_manager: dnf5` +
`playbooks/roles/patch_apply/tasks/pkg_mgr/dnf5.yml` (likely using
`ansible.builtin.dnf5`) + validation against a real RHEL10 box -- nothing in
`patch_common`, `patch_facts_collect`, or the playbooks themselves needs to
change.

## ServiceNow notification

`playbooks/roles/patch_snow_notify` is currently a **stub** -- see its
README.md. `snow_notify_enabled: false` by default; flip it on once your
instance URL, table/field names, and auth are confirmed.

## Known gaps / deliberately out of scope for this phase

- Cluster/HA awareness (Pacemaker, load-balancer drain) -- not built.
- Rollback (snapshot revert / package downgrade) -- future phase.
- Housekeeping/pruning of old `/var/log/patch_mgmt/<change_id>/` directories
  -- deliberately not built (retention decision: never auto-delete).
- Monitoring pause/resume (Uptime Kuma) -- deferred; see
  `aeda/playbooks/uptime_kuma/edit_monitor.yml` for the existing piece this
  would eventually call.
