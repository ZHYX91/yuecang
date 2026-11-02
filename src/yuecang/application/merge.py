"""Bounded textual three-way merging; unsupported structure stays unresolved."""
from __future__ import annotations

import json
import time

from yuecang.core.hashing import sha256_json, sha256_text
from yuecang.core.text_diff import compare_texts
from yuecang.storage.project import DomainError, canonical, uid

FIELDS = {"title", "author", "language", "edition", "translator"}
TEXT_TYPES = {"paragraph", "heading", "code", "footnote"}
BLOCK_BUDGET = 200_000
TOTAL_BUDGET = 2_000_000
CHANGE_BUDGET = 256


def _head(wb, branch_id, expected):
    branch = wb.project.row("branches", branch_id)
    if branch["head_revision_id"] != expected:
        raise DomainError("revision_conflict", "分支已经变化，请刷新后重新比较", 409)
    return branch


def fork_revision(wb, revision_id, name):
    revision = wb.revision(revision_id)
    if not isinstance(name, str) or not name.strip() or len(name) > 200:
        raise DomainError("branch_name", "请填写200字以内的分支名称")
    branch_id = uid()
    with wb.project.transaction() as db:
        db.execute("INSERT INTO branches VALUES(?,?,?,?,0)", (branch_id, revision["book_id"], name.strip(), revision_id))
        wb.project.event(db, "branch.forked", branch_id, {"revision_id": revision_id})
    return wb.project.row("branches", branch_id)


def edit_metadata(wb, branch_id, expected_revision_id, changes):
    branch = _head(wb, branch_id, expected_revision_id)
    revision = wb.revision(expected_revision_id)
    if not isinstance(changes, dict) or not changes or set(changes) - FIELDS:
        raise DomainError("metadata_fields", "只可修改题名、作者、语言、版本标签和译者")
    if any(not isinstance(v, str) or len(v) > 1000 for v in changes.values()) or any(k in changes and not changes[k].strip() for k in ("title", "language")):
        raise DomainError("metadata_value", "元数据必须为有限长度文字，题名和语言不能为空")
    metadata = {**revision["metadata"], **changes}
    if metadata == revision["metadata"]:
        return {"revision_id": expected_revision_id, "branch_id": branch_id, "unchanged": True, "revision": revision}
    new_id = uid()
    with wb.project.transaction() as db:
        current = wb.project.row("branches", branch_id, db)
        if current["head_revision_id"] != expected_revision_id or current["version"] != branch["version"]:
            raise DomainError("revision_conflict", "分支已经变化，元数据未保存", 409)
        db.execute("INSERT INTO revisions VALUES(?,?,?,?,?,?,?,?,?)", (new_id, revision["book_id"], revision["edition_id"], expected_revision_id,
                   revision["document_sha"], canonical(metadata), "draft", canonical(revision["issues"]), time.time()))
        db.execute("UPDATE branches SET head_revision_id=?,version=version+1 WHERE id=?", (new_id, branch_id))
        wb._revision_refs(db, new_id, revision["document_sha"], revision["blocks"])
        db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?)", (uid(), expected_revision_id, new_id, "user", None,
                   canonical({"type": "metadata", "before": revision["metadata"], "after": metadata}), time.time()))
        wb.project.event(db, "revision.metadata_edited", new_id)
    return {"revision_id": new_id, "branch_id": branch_id, "revision": wb.revision(new_id)}


def _chain(wb, revision):
    result = {}
    while revision:
        if revision["id"] in result or len(result) >= 10000:
            raise DomainError("merge_history", "修订历史存在循环或超过比较上限")
        result[revision["id"]] = revision
        # History traversal loads relation rows, not a copy of every full book.
        revision = wb.project.row("revisions", revision["parent_id"]) if revision.get("parent_id") else None
    return result


def _lineage(wb, base, chain, tip):
    mapping = {b["id"]: b["id"] for b in base["blocks"]}
    path, current = [], tip
    while current["id"] != base["id"]:
        path.append(current)
        current = chain[current["parent_id"]]
    order = [b["id"] for b in base["blocks"]]
    for revision in reversed(path):
        with wb.project.connect() as db:
            operations = db.execute("SELECT payload FROM operations WHERE output_revision=? ORDER BY created,id", (revision["id"],)).fetchall()
        for row in operations:
            operation = json.loads(row["payload"])
            if operation.get("type") == "metadata":
                continue
            if operation.get("type") not in {"replace", "delete"} or operation.get("input_block") not in mapping:
                return None
            identity = mapping.pop(operation["input_block"])
            mapping[operation["output_block"]] = identity
    if [mapping.get(b["id"]) for b in tip["blocks"]] != order:
        return None
    return {mapping[b["id"]]: b for b in tip["blocks"]}


def _changes(base, other, side):
    if base == other:
        return []
    # Empty strings have unambiguous insertion/deletion coordinates.
    if not base or not other:
        return [(0, len(base), other, side)]
    comparison = compare_texts(base, other)
    rows = comparison.get("opcodes", [])
    if not comparison["complete_difference_coverage"] or len(rows) > CHANGE_BUDGET or any(r["tag"] == "unresolved" for r in rows):
        return None
    return [(r["left_span"][0], r["left_span"][1], other[slice(*r["right_span"])], side) for r in rows]


def _overlap(a, b):
    if a[0] == a[1] or b[0] == b[1]:
        return max(a[0], b[0]) <= min(a[1], b[1])
    return max(a[0], b[0]) < min(a[1], b[1])


def _apply(base, changes, start=0, end=None):
    cursor, result = start, []
    for a, b, text, *_ in sorted(changes, key=lambda r: (r[0], r[1])):
        result.extend((base[cursor:a], text))
        cursor = b
    result.append(base[cursor:len(base) if end is None else end])
    return "".join(result)


def _merge_text(base, left, right, binding, resolutions):
    if left == right or right == base:
        return left, [], set()
    if left == base:
        return right, [], set()
    a, b = _changes(base, left, "left"), _changes(base, right, "right")
    if a is None or b is None or len(a) + len(b) > CHANGE_BUDGET:
        return None, [{"code": "diff_budget", "block_id": binding["block_id"]}], set()
    groups = []
    for change in sorted(a + b, key=lambda r: (r[0], r[1])):
        linked = [g for g in groups if any(_overlap(change, row) for row in g)]
        if linked:
            group = [change]
            for previous in linked:
                group.extend(previous)
                groups.remove(previous)
            groups.append(group)
        else:
            groups.append([change])
    merged, conflicts, used = [], [], set()
    for group in groups:
        start, end = min(r[0] for r in group), max(r[1] for r in group)
        sides = {r[3] for r in group}
        ltext = _apply(base, [r for r in group if r[3] == "left"], start, end)
        rtext = _apply(base, [r for r in group if r[3] == "right"], start, end)
        if len(sides) == 1 or ltext == rtext:
            chosen = ltext if "left" in sides else rtext
        else:
            conflict = {**binding, "start": start, "end": end, "base": base[start:end], "left": ltext, "right": rtext}
            conflict["id"] = sha256_json(conflict)[:32]
            decision = resolutions.get(conflict["id"])
            if decision is None:
                conflicts.append(conflict)
                continue
            used.add(conflict["id"])
            if isinstance(decision, str):
                decision = {"choice": decision}
            if not isinstance(decision, dict) or decision.get("choice") not in {"left", "right", "custom"}:
                raise DomainError("merge_resolution", "冲突须明确选择左侧、右侧或自定义文本")
            chosen = decision.get("text") if decision["choice"] == "custom" else conflict[decision["choice"]]
            if not isinstance(chosen, str) or len(chosen) > BLOCK_BUDGET:
                raise DomainError("merge_resolution", "冲突替换文字非法或超过上限")
        merged.append((start, end, chosen))
    return _apply(base, merged) if not conflicts else None, conflicts, used


def merge_branch(wb, branch_id, expected_revision_id, other_revision_id, resolutions=None):
    _head(wb, branch_id, expected_revision_id)
    resolutions = {} if resolutions is None else resolutions
    if not isinstance(resolutions, dict):
        raise DomainError("merge_resolution", "冲突决定必须按冲突标识提供")
    left, right = wb.revision(expected_revision_id), wb.revision(other_revision_id)
    if left["book_id"] != right["book_id"] or left["edition_id"] != right["edition_id"]:
        raise DomainError("merge_edition", "只能合并同一本书、同一版本的整理稿")
    lchain, rchain = _chain(wb, left), _chain(wb, right)
    base_id = next((key for key in lchain if key in rchain), None)
    if base_id is None:
        return {"merged": False, "conflicts": [], "unresolved": [{"code": "no_common_ancestor"}]}
    base = wb.revision(base_id)
    binding = {"base_revision_id": base_id, "left_revision_id": left["id"], "right_revision_id": right["id"]}
    different_fields = [k for k in FIELDS if left["metadata"].get(k) != right["metadata"].get(k)]
    if different_fields:
        return {**binding, "merged": False, "conflicts": [], "unresolved": [{"code": "metadata_difference", "fields": sorted(different_fields)}]}
    lblocks, rblocks = _lineage(wb, base, lchain, left), _lineage(wb, base, rchain, right)
    if lblocks is None or rblocks is None:
        return {**binding, "merged": False, "conflicts": [], "unresolved": [{"code": "unsupported_structure_change"}]}
    operations, conflicts, unresolved, used, budget = [], [], [], set(), 0
    for original in base["blocks"]:
        a, b = lblocks[original["id"]], rblocks[original["id"]]
        if any(a.get(key) != b.get(key) for key in ("type", "level", "attributes", "resources")):
            unresolved.append({"code": "unsupported_structure_change", "block_id": original["id"]})
            continue
        if a["text"] == b["text"]:
            continue
        budget += sum(len(x["text"]) for x in (original, a, b))
        if budget > TOTAL_BUDGET or max(len(x["text"]) for x in (original, a, b)) > BLOCK_BUDGET:
            unresolved.append({"code": "text_budget", "block_id": original["id"]})
            continue
        if original["type"] not in TEXT_TYPES or a["type"] != original["type"] or b["type"] != original["type"] or a.get("resources") != b.get("resources"):
            unresolved.append({"code": "unsupported_structure_change", "block_id": original["id"]})
            continue
        text, pending, decisions = _merge_text(original["text"], a["text"], b["text"], {**binding, "block_id": original["id"]}, resolutions)
        used.update(decisions)
        for item in pending:
            (unresolved if item.get("code") else conflicts).append(item)
        if text is not None and (text != a["text"] or decisions):
            evidence = [{"kind": "merge_input", "revision_id": rev["id"], "block_id": block["id"], "start": 0, "end": len(block["text"]), "text_sha256": sha256_text(block["text"])}
                        for rev, block in ((base, original), (left, a), (right, b))]
            operations.append({"block_id": a["id"], "start": 0, "end": len(a["text"]), "old_text": a["text"], "text": text, "evidence": evidence})
    if set(resolutions) - used - {c["id"] for c in conflicts}:
        raise DomainError("merge_resolution_stale", "冲突决定不属于当前冻结输入，请重新比较", 409)
    if conflicts or unresolved:
        return {**binding, "merged": False, "conflicts": conflicts, "unresolved": unresolved}
    if not operations:
        return {**binding, "merged": True, "unchanged": True, "revision_id": left["id"], "branch_id": branch_id}
    for revision in (left, right):
        wb.validate_revision(revision)
    result = wb.edit(branch_id, expected_revision_id, operations, actor="merge-approved")
    return {**result, **binding, "merged": True, "conflicts": [], "unresolved": []}
