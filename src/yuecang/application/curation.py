"""Freeze comparison evidence and assemble confirmed source ranges."""
from __future__ import annotations

from collections import defaultdict, deque
import json
import time

from yuecang.core.curation import CurationLimits, plan_rows, structured
from yuecang.core.source_analysis import identifier, span
from yuecang.storage.project import DomainError, canonical, digest, uid


class Curation:
    def __init__(self, wb, *, limits=None):
        self.wb = wb
        self.project = wb.project
        self.limits = limits or CurationLimits()

    def _snapshot(self, db, book_id, edition_id, source_ids, allow_composite):
        book = self.project.row("books", book_id, db)
        edition = self.project.row("editions", edition_id, db)
        if edition["book_id"] != book_id:
            raise DomainError("curation_scope", "目标版本须属于所选图书")

        sources = [self.project.row("sources", sid, db) for sid in source_ids]
        params = {f"source{i}": sid for i, sid in enumerate(source_ids)}
        scope = ",".join(f":{key}" for key in params)
        params.update(book=book_id, edition=edition_id, base=source_ids[0], composite=allow_composite)
        eligible = "{alias}.book_id=:book AND ({alias}.edition_id=:edition OR (:composite AND {alias}.source_id<>:base))"
        for sid in source_ids:
            if not db.execute(f"SELECT 1 FROM assignments a WHERE a.source_id=:sid AND {eligible.format(alias='a')} LIMIT 1",
                              {**params, "sid": sid}).fetchone():
                if allow_composite:
                    raise DomainError("curation_scope", "底本须归属目标版本，补充来源须归属同一图书")
                raise DomainError("curation_scope", "底本和补充来源须先明确归属到所选图书和版本")

        order = "ORDER BY source_id,block_id,start,end,id LIMIT :limit"
        count = self.limits.max_assignments
        assignments = [dict(row) for row in db.execute(
            f"SELECT a.* FROM assignments a WHERE a.source_id IN ({scope}) AND {eligible.format(alias='a')} {order}",
            {**params, "limit": count + 1})]
        overlaps = []
        if len(assignments) <= count:
            # Other assignments matter where they intersect eligible coverage.
            overlaps = [dict(row) for row in db.execute(f"""
                SELECT a.* FROM assignments a WHERE a.source_id IN ({scope}) AND NOT ({eligible.format(alias='a')})
                AND EXISTS (SELECT 1 FROM assignments b WHERE b.source_id=a.source_id AND b.block_id=a.block_id
                    AND {eligible.format(alias='b')} AND (
                        (a.start<b.end AND a.end>b.start) OR (a.start=a.end AND a.start=b.start AND a.end=b.end)))
                {order}
                """, {**params, "limit": count - len(assignments) + 1})]
        snapshot = {"book": {k: book[k] for k in ("id", "title", "author", "language")}, "edition": edition,
                    "sources": sources, "assignments": assignments, "overlaps": overlaps}
        if allow_composite:
            if any(row["book_id"] != book_id for row in overlaps):
                raise DomainError("curation_scope", "所选归属范围同时关联了其他图书，请先核对归属")
            related_ids = {row["edition_id"] for row in [*assignments, *overlaps]} - {edition_id}
            related = {eid: self.project.row("editions", eid, db) for eid in sorted(related_ids)}
            if any(row["book_id"] != book_id for row in related.values()):
                raise DomainError("curation_scope", "来源归属的版本与所选图书不一致，请先核对归属")
            snapshot["related_editions"] = related
        return snapshot

    def _load(self, snapshot, check):
        if len(snapshot["assignments"]) + len(snapshot["overlaps"]) > self.limits.max_assignments:
            return [], ["assignment_budget"]
        sources = snapshot["sources"]
        if any(not source["blocks_sha"] for source in sources):
            raise DomainError("curation_provenance", "已归属来源没有可核查的提取结果")
        if sum(self.project.object_path(s["blocks_sha"]).stat().st_size for s in sources) > self.limits.max_extraction_bytes:
            return [], ["extraction_budget"]
        by_source = defaultdict(list)
        for row in snapshot["assignments"]:
            by_source[row["source_id"]].append(row)
        composite = "related_editions" in snapshot
        other_editions = defaultdict(deque)
        if composite:
            for row in snapshot["overlaps"]:
                other_editions[(row["source_id"], row["block_id"])].append(row)
        sides, range_count = [], 0
        for source in sources:
            check()
            blocks = self.project.read_json(source["blocks_sha"])
            order = {block["id"]: i for i, block in enumerate(blocks)}
            assigned = sorted(by_source[source["id"]], key=lambda r: (order.get(r["block_id"], -1), r["start"], r["end"]))
            merged = []
            for row in assigned:
                if row["block_id"] not in order:
                    raise DomainError("curation_provenance", "已归属范围与来源提取结果不一致")
                block = blocks[order[row["block_id"]]]
                if not 0 <= row["start"] <= row["end"] <= len(block["text"]):
                    raise DomainError("curation_provenance", "已归属范围与来源提取结果不一致")
                version_range = (row["edition_id"], row["start"], row["end"])
                previous = merged[-1] if merged else None
                if previous and previous["block_id"] == row["block_id"] and row["start"] <= previous["end"]:
                    previous["end"] = max(previous["end"], row["end"])
                    previous["edition_ranges"].add(version_range)
                else:
                    merged.append({**row, "edition_ranges": {version_range}})
            fragments = []
            active, previous_block = [], None
            for row in merged:
                ordinal = order[row["block_id"]]
                block = blocks[ordinal]
                start, end = row["start"], row["end"]
                length = len(block["text"])
                if structured(block) and (start != 0 or end != length):
                    raise DomainError("curation_scope", "带结构或资源的内容须整块归属后再生成整理计划")
                source_span = span(source["id"], block["id"], start, end)
                if composite:
                    if block["id"] != previous_block:
                        active = []
                        previous_block = block["id"]
                    pending = other_editions.get((source["id"], block["id"]))
                    while pending and (pending[0]["start"] < end or pending[0]["start"] == pending[0]["end"] == start == end):
                        active.append(pending.popleft())
                    active = [other for other in active if other["end"] > start or other["start"] == other["end"] == start == end]
                    edition_ranges = row["edition_ranges"] | {
                        (other["edition_id"], max(other["start"], start), min(other["end"], end)) for other in active}
                    range_count += len(edition_ranges)
                    if range_count > self.limits.max_assignments:
                        return [], ["assignment_budget"]
                    source_span["edition_ids"] = sorted({eid for eid, _, _ in edition_ranges})
                    source_span["edition_ranges"] = [{"edition_id": eid, "start": a, "end": b}
                                                     for eid, a, b in sorted(edition_ranges)]
                fragments.append({"span": source_span, "ordinal": ordinal,
                                  "block_length": length, "text": block["text"][start:end], "type": block["type"],
                                  "level": block.get("level"), "attributes": block.get("attributes", {}),
                                  "resources": block.get("resources", [])})
            sides.append(fragments)
        return sides, []

    def _current(self, db, plan, source_ids):
        try:
            snapshot = self._snapshot(db, plan["book_id"], plan["edition_id"], source_ids, plan.get("allow_composite", False))
        except DomainError as exc:
            if exc.code not in {"not_found", "curation_scope"}:
                raise
            raise DomainError("curation_stale", "来源归属或图书版本已变化，请重新生成整理计划", 409) from exc
        if digest(snapshot) != plan["snapshot_sha"]:
            raise DomainError("curation_stale", "来源提取、归属或图书版本信息已变化，请重新生成整理计划", 409)

    @staticmethod
    def _row(db, plan_id):
        return db.execute("SELECT * FROM candidates WHERE id=? AND kind='curation_plan'", (plan_id,)).fetchone()

    def _read(self, plan_id):
        if not isinstance(plan_id, str) or not plan_id:
            raise DomainError("curation_plan", "整理计划标识无效")
        with self.project.connect() as db:
            row = self._row(db, plan_id)
        if row is None:
            raise DomainError("curation_plan", "整理计划不存在", 404)
        evidence = json.loads(row["evidence"])
        plan = self.project.read_json(evidence["payload_sha"])
        plan["state"] = row["state"]
        if "result" in evidence:
            plan["result"] = evidence["result"]
        return plan, evidence

    def get(self, plan_id):
        return self._read(plan_id)[0]

    def create(self, book_id, edition_id, base_source_id, donor_source_ids, *, allow_composite=False, job=None):
        if type(allow_composite) is not bool:
            raise DomainError("curation_scope", "综合整理版选项必须为布尔值")
        if (not isinstance(donor_source_ids, list) or not donor_source_ids
                or any(not isinstance(value, str) or not value
                       for value in (book_id, edition_id, base_source_id, *donor_source_ids))):
            raise DomainError("curation_scope", "请选择底本和至少一个补充来源")
        if base_source_id in donor_source_ids or len(set(donor_source_ids)) != len(donor_source_ids):
            raise DomainError("curation_scope", "底本和补充来源必须互不重复")

        plan_id = identifier(["curation_plan", job["id"]]) if job else uid()
        over_donor_budget = len(donor_source_ids) > self.limits.max_donors
        source_ids = [base_source_id, *donor_source_ids[:self.limits.max_donors]]

        def check():
            if job:
                with self.project.connect() as db:
                    self.wb._job_guard(db, job)

        with self.project.connect() as db:
            db.execute("BEGIN")
            self.wb._job_guard(db, job)
            existing = self._row(db, plan_id)
            if not existing:
                snapshot = self._snapshot(db, book_id, edition_id, source_ids, allow_composite)
        if existing:
            return self.get(plan_id)

        rows = []
        if over_donor_budget:
            reasons = ["source_count_budget"]
        else:
            sides, reasons = self._load(snapshot, check)
            if not reasons:
                rows, reasons = plan_rows(sides[0], sides[1:], limits=self.limits, check=check)
        editions = {edition_id: snapshot["edition"], **snapshot.get("related_editions", {})}
        source_editions = defaultdict(set)
        scoped_assignments = snapshot["assignments"] + (snapshot["overlaps"] if allow_composite else [])
        for assignment in scoped_assignments:
            source_editions[assignment["source_id"]].add(assignment["edition_id"])
        plan = {"schema_version": 1, "id": plan_id, "book_id": book_id, "edition_id": edition_id,
                "base_source_id": base_source_id, "donor_source_ids": donor_source_ids,
                "allow_composite": allow_composite,
                "editions": {eid: {k: ed[k] for k in ("id", "label", "language", "translator", "book_id")}
                             for eid, ed in editions.items()},
                "source_editions": {sid: sorted(ids) for sid, ids in source_editions.items()},
                "source_names": {source["id"]: source["name"] for source in snapshot["sources"]},
                "snapshot_sha": digest(snapshot), "incomplete": bool(reasons), "limit_reasons": reasons, "rows": rows}
        owner = f"{job['id']}:{job['token']}" if job else "curation:" + plan_id
        payload_sha = self.project.put_json(plan, "curation_plan", owner=owner)
        with self.project.transaction() as db:
            self.wb._job_guard(db, job)
            if not self._row(db, plan_id):
                self._current(db, plan, source_ids)
                evidence = {key: plan[key] for key in ("book_id", "edition_id", "base_source_id", "donor_source_ids", "allow_composite")}
                evidence.update(payload_sha=payload_sha, base_source_name=plan["source_names"][base_source_id],
                                book_title=snapshot["book"]["title"])
                db.execute("INSERT INTO candidates VALUES(?,?,?,?,?,?,?)",
                           (plan_id, base_source_id, None, "curation_plan", canonical(evidence), "ready", time.time()))
                self.project.ref(db, "curation_plan", plan_id, payload_sha, "payload")
                self.project.event(db, "curation.created", plan_id)
        return self.get(plan_id)

    @staticmethod
    def _select(plan, choices):
        known = {row["id"] for row in plan["rows"]}
        if (not isinstance(choices, dict) or set(choices) - known
                or any(not isinstance(value, str) for value in choices.values())):
            raise DomainError("curation_choices", "整理选择必须使用本计划的行和选项标识")
        resolved, selections, by_block = {}, [], defaultdict(list)
        for row in plan["rows"]:
            selected = choices.get(row["id"], row["default_option_id"])
            option = next((o for o in row["options"] if o["id"] == selected), None)
            if option is None:
                raise DomainError("curation_choices", "请逐项确认所有差异、补充内容和无法定位的范围")
            resolved[row["id"]] = selected
            selections.extend(option["spans"])
            for part in option["spans"]:
                by_block[(part["source_id"], part["block_id"])].append((part["start"], part["end"]))
        for ranges in by_block.values():
            ordered = sorted(ranges)
            if any(a == b or a[1] > b[0] for a, b in zip(ordered, ordered[1:])):
                raise DomainError("curation_overlap", "选择包含重复或重叠的来源范围")
        return selections, resolved

    def _result(self, evidence, choices_sha):
        if evidence.get("choices_sha") != choices_sha:
            raise DomainError("curation_applied", "整理计划已有结果，其他选择或标题须重新生成计划", 409)
        result = evidence["result"]
        return {**result, "revision": self.wb.revision(result["revision_id"]),
                "branch": self.project.row("branches", result["branch_id"])}

    def assemble(self, plan_id, snapshot_sha, choices, title=None):
        plan, evidence = self._read(plan_id)
        if snapshot_sha != plan["snapshot_sha"]:
            raise DomainError("curation_stale", "整理计划快照不匹配，请刷新计划", 409)
        if plan["incomplete"]:
            raise DomainError("curation_incomplete", "比对达到处理上限，请缩小来源范围后重新生成计划", 409)
        if title is not None and not isinstance(title, str):
            raise DomainError("curation_title", "整理稿标题必须为文字")
        selections, resolved = self._select(plan, choices)
        try:
            choices_sha = digest([title or None, resolved])
        except UnicodeError as exc:
            raise DomainError("curation_title", "整理稿标题包含无效字符") from exc
        if plan["state"] == "applied":
            return self._result(evidence, choices_sha)

        source_ids = [plan["base_source_id"], *plan["donor_source_ids"]]

        def precondition(db):
            row = self._row(db, plan_id)
            if row is None or row["state"] != "ready" or json.loads(row["evidence"])["payload_sha"] != evidence["payload_sha"]:
                raise DomainError("curation_applied", "整理计划状态已变化，请刷新计划", 409)
            self._current(db, plan, source_ids)

        def commit(db, result):
            completed = {**evidence, "choices_sha": choices_sha, "result": result}
            db.execute("UPDATE candidates SET evidence=?,state='applied' WHERE id=?", (canonical(completed), plan_id))
            self.project.event(db, "curation.applied", plan_id, result)

        with self.project.connect() as db:
            db.execute("BEGIN")
            self._current(db, plan, source_ids)
        allow_composite = plan.get("allow_composite", False)
        record = {"curation_plan_id": plan_id, "snapshot_sha": snapshot_sha, "choices_sha": choices_sha,
                  "choices": resolved, "allow_composite": allow_composite}
        if allow_composite:
            record["source_editions"] = plan["editions"]
        selection_editions = [part["edition_ranges"] for part in selections] if allow_composite else None
        try:
            return self.wb.assemble(plan["book_id"], selections, edition_id=plan["edition_id"], title=title,
                                    allow_composite=allow_composite, selection_editions=selection_editions,
                                    assign_selected=False, precondition=precondition, commit_hook=commit,
                                    record_extra=record)
        except DomainError as exc:
            if exc.code != "curation_applied":
                raise
            current, current_evidence = self._read(plan_id)
            if current["state"] == "applied":
                return self._result(current_evidence, choices_sha)
            raise
