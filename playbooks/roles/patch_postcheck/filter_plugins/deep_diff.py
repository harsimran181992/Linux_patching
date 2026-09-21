"""
patch_deep_diff -- recursive field-level diff between two pre-check/
post-check JSON-compatible values (dicts, lists, scalars).

Used by patch_postcheck to turn a "the whole disks component differs"
finding into "sda.size changed from 100GB to 120GB, sr0 was removed" --
one entry per actual field, not a full pre/post dump of the component.

Pure stdlib Python, no external dependencies -- runs on the Ansible
controller (this is a filter plugin, never shipped to or run on a managed
host), so there's nothing to install in any execution environment beyond
this file existing next to the role that uses it (Ansible's standard,
config-independent role-scoped filter_plugins/ auto-discovery).

Return shape: a list of change records, each one of:
  {"path": "sda.size", "change": "changed", "pre": "100.00 GB", "post": "120.00 GB"}
  {"path": "sr0",      "change": "removed", "pre": {...}}
  {"path": "sdb",      "change": "added",   "post": {...}}
An empty list means the two values were equal (this is exactly how
"matched" is derived on the Ansible side: field_changes | length == 0).

Lists are handled two ways:
  - A list of dicts at a path configured in LIST_IDENTITY_KEYS below is
    diffed as a keyed collection (matched up by that identity field, so
    reordering never shows as a change, only real additions/removals/
    per-item field changes do).
  - Any other list (scalars, or an unconfigured list of dicts) is diffed
    as a set: values only in pre = removed, values only in post = added.
    This is the correct behaviour for our plain string lists (e.g.
    network.interfaces, services.running) and a safe explicit fallback
    for anything unexpected, rather than silently guessing at identity.
"""
from __future__ import annotations

# path -> the dict key to treat as this list's identity when diffing.
# "filesystems.mounts" is the only list-of-dicts in our schema today;
# everything else that looks like "a collection of things" (disks,
# blkid, lvm's vgs/lvs/pvs, per-interface network facts) was already
# built as a dict keyed by name upstream in patch_facts_collect
# specifically so it doesn't need an entry here.
LIST_IDENTITY_KEYS = {
    "filesystems.mounts": "mount",
}


def _join(path, key):
    return f"{path}.{key}" if path else str(key)


def _diff_dicts(pre, post, path):
    changes = []
    pre_keys = set(pre.keys())
    post_keys = set(post.keys())
    for key in sorted(pre_keys - post_keys, key=str):
        changes.append({"path": _join(path, key), "change": "removed", "pre": pre[key]})
    for key in sorted(post_keys - pre_keys, key=str):
        changes.append({"path": _join(path, key), "change": "added", "post": post[key]})
    for key in sorted(pre_keys & post_keys, key=str):
        changes.extend(_diff(pre[key], post[key], _join(path, key)))
    return changes


def _diff_dict_list(pre, post, path, id_key):
    pre_by_id = {item.get(id_key): item for item in pre}
    post_by_id = {item.get(id_key): item for item in post}
    changes = []
    for ident in sorted(set(pre_by_id) - set(post_by_id), key=str):
        changes.append({"path": f"{path}[{id_key}={ident}]", "change": "removed", "pre": pre_by_id[ident]})
    for ident in sorted(set(post_by_id) - set(pre_by_id), key=str):
        changes.append({"path": f"{path}[{id_key}={ident}]", "change": "added", "post": post_by_id[ident]})
    for ident in sorted(set(pre_by_id) & set(post_by_id), key=str):
        changes.extend(_diff(pre_by_id[ident], post_by_id[ident], f"{path}[{id_key}={ident}]"))
    return changes


def _diff_scalar_list(pre, post, path):
    pre_set, post_set = set(pre), set(post)
    changes = []
    for value in sorted(pre_set - post_set, key=str):
        changes.append({"path": path, "change": "removed", "pre": value})
    for value in sorted(post_set - pre_set, key=str):
        changes.append({"path": path, "change": "added", "post": value})
    return changes


def _diff(pre, post, path):
    if isinstance(pre, dict) and isinstance(post, dict):
        return _diff_dicts(pre, post, path)

    if isinstance(pre, list) and isinstance(post, list):
        id_key = LIST_IDENTITY_KEYS.get(path)
        items_are_dicts = all(isinstance(x, dict) for x in pre) and all(isinstance(x, dict) for x in post)
        if id_key and items_are_dicts:
            return _diff_dict_list(pre, post, path, id_key)
        try:
            return _diff_scalar_list(pre, post, path)
        except TypeError:
            # Unhashable list contents (e.g. an unconfigured list of dicts) --
            # fall back to a single whole-list change rather than crashing.
            if pre != post:
                return [{"path": path, "change": "changed", "pre": pre, "post": post}]
            return []

    # Scalars, or a type mismatch between pre and post (e.g. lvm's
    # lvm_facts is the literal string "N/A" on one side and a dict on the
    # other when LVM availability itself changed) -- direct compare.
    if pre != post:
        return [{"path": path, "change": "changed", "pre": pre, "post": post}]
    return []


def patch_deep_diff(pre, post, path=""):
    return _diff(pre, post, path)


class FilterModule:
    def filters(self):
        return {"patch_deep_diff": patch_deep_diff}
