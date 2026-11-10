"""Freeze and apply explicitly confirmed source organization decisions."""
from __future__ import annotations

from collections import defaultdict
import json
import re
import time

from yuecang.application.source_analysis import SourceAnalysis
from yuecang.core.source_analysis import AnalysisLimits, identifier, span
from yuecang.storage.project import DomainError, canonical, digest, uid


MAX_SOURCES = 20
MAX_BLOCKS = 4096
MAX_CHARS = 2_000_000
MAX_EXTRACTION_BYTES = 32 * 1024**2
MAX_CANDIDATES = 300
MAX_GROUP_SPANS = 100
MAX_PLAN_SPANS = 4096
STRUCTURED = {"figure", "unknown", "formula", "table"}
AUTO_USED = "已关联图书"
SPAN_FIELDS = ("source_id", "block_id", "start", "end")
TARGET_FIELDS = ("book_id", "edition_id")
EVIDENCE_LIMITS = {"reference_budget", "reference_fragment_budget", "reference_loading_budget",
                   "anchor_budget", "candidate_budget", "comparison_budget", "match_budget"}
EXPLICIT_QUOTES = re.compile(r'“[^“”]*”|‘[^‘’]*’|「[^「」]*」|『[^『』]*』|«[^«»]*»|"[^"\r\n]*"')


def _subtract(start, end, rows):
    """Return the original interval minus other intervals, including [0, 0] resources."""
    if start == end:
        if not any(row["start"] == row["end"] == start for row in rows):
            yield start, end
        return
    cursor = start
    for row in sorted(rows, key=lambda item: (item["start"], item["end"])):
        left, right = row["start"], row["end"]
        if left == right or right <= cursor:
            continue
        if left >= end:
            break
        if cursor < left:
            yield cursor, left
        cursor = min(end, right)
    if cursor < end:
        yield cursor, end


def _string(value, *, empty=False, limit=240):
    if isinstance(value, str) and len(value) <= limit and (empty or value.strip()):
        try:
            value.encode("utf-8")
        except UnicodeError:
            return False
        return True
    return False


def _target(value):
    if isinstance(value, dict) and set(value) == set(TARGET_FIELDS) and all(_string(value[key]) for key in TARGET_FIELDS):
        return value
    if isinstance(value, dict) and set(value) == {"new_book"} and isinstance(value["new_book"], dict):
        supplied = value["new_book"]
        if {"key", "title"} <= supplied.keys() <= {"key", "title", "author", "language", "edition"}:
            book = {"author": "", "language": "zh", "edition": "未确定版本", **supplied}
            if all(_string(item, empty=key == "author") for key, item in book.items()):
                return {"new_book": {key: item.strip() for key, item in book.items()}}
    raise DomainError("assignment_target", "请选择已有图书和版本，或填写有效的新图书资料")


def _parts(start, end, matches):
    relevant = [match for match in matches if start < match["span"]["end"] and end > match["span"]["start"]]
    endpoints = {start, end}
    for match in relevant:
        endpoints.update((max(start, match["span"]["start"]), min(end, match["span"]["end"])))
    points = sorted(endpoints)
    if start == end:
        yield start, end, []
    for left, right in zip(points, points[1:]):
        covering = [match for match in relevant if match["span"]["start"] <= left < right <= match["span"]["end"]]
        yield left, right, covering


class Organization:
    def __init__(self, wb):
        self.wb = wb
        self.project = wb.project

    @staticmethod
    def _generations(db):
        values = dict(db.execute("SELECT key,value FROM meta WHERE key IN ('assignment_generation','disposition_generation')"))
        return {key: int(values.get(key, 0)) for key in ("assignment_generation", "disposition_generation")}

    def _ensure_current(self, db, plan):
        if any(plan[key] != value for key, value in self._generations(db).items()):
            raise DomainError("assignment_plan_stale", "归属或来源处置已更新，请重新生成归属计划", 409)
        for source in plan["sources"]:
            if self.project.row("sources", source["source_id"], db)["blocks_sha"] != source["blocks_sha"]:
                raise DomainError("assignment_plan_stale", "来源提取结果已变化，请重新生成归属计划", 409)

    def create(self, source_ids, *, job=None):
        if (not isinstance(source_ids, list) or not 1 <= len(source_ids) <= MAX_SOURCES
                or any(not _string(item) for item in source_ids) or len(set(source_ids)) != len(source_ids)):
            raise DomainError("assignment_sources", f"请选择 1 至 {MAX_SOURCES} 份不同的来源")
        plan_id = identifier(["assignment_plan", job["id"]]) if job else uid()
        with self.project.connect() as db:
            self.wb._job_guard(db, job)
            if job and (existing := self._find_plan(db, plan_id)):
                return self._plan_view(existing, json.loads(existing["evidence"]))
            plan = {"id": plan_id, **self._generations(db), "sources": []}
            rows = [self.project.row("sources", source_id, db) for source_id in source_ids]
        chars = blocks = candidates = spans = loaded = 0
        for row in rows:
            entry = {"source_id": row["id"], "name": row["name"], "blocks_sha": row["blocks_sha"],
                     "incomplete": False, "limit_reasons": [], "candidates": []}
            plan["sources"].append(entry)
            sha = row["blocks_sha"]
            if not sha or not row["block_count"]:
                continue
            size = self.project.object_path(sha).stat().st_size
            if (size + loaded > MAX_EXTRACTION_BYTES or chars >= MAX_CHARS or blocks >= MAX_BLOCKS
                    or candidates >= MAX_CANDIDATES or spans >= MAX_PLAN_SPANS):
                entry.update(incomplete=True, limit_reasons=["plan_source_budget"])
                continue
            loaded += size
            source = self.wb.source(row["id"])
            limits = AnalysisLimits(max_blocks=MAX_BLOCKS - blocks, max_chars=MAX_CHARS - chars,
                                    max_pairs=max(100, 1000 // len(rows)),
                                    max_total_cells=max(4_000_000, 40_000_000 // len(rows)))
            report = SourceAnalysis(self.wb, limits=limits).inspect(row["id"], job=job)
            selected = source["blocks"][:report["analyzed_blocks"]]
            blocks += len(selected)
            chars += sum(len(block["text"]) for block in selected)
            items, truncated = self._candidates(source, selected, report,
                                                MAX_CANDIDATES - candidates, MAX_PLAN_SPANS - spans)
            reasons = report["limit_reasons"] + (["plan_candidate_budget"] if truncated else [])
            entry.update(candidates=items, incomplete=bool(reasons), limit_reasons=reasons)
            candidates += len(items)
            spans += sum(len(item["spans"]) for item in items)
        owner = f"{job['id']}:{job['token']}" if job else "assignment-plan:" + plan["id"]
        sha = self.project.put_json(plan, "assignment_plan", owner=owner)
        with self.project.transaction() as db:
            self.wb._job_guard(db, job)
            if job and (existing := self._find_plan(db, plan_id)):
                return self._plan_view(existing, json.loads(existing["evidence"]))
            self._ensure_current(db, plan)
            evidence = {"payload_sha": sha, "source_name": rows[0]["name"], "source_count": len(rows)}
            db.execute("INSERT INTO candidates VALUES(?,?,?,?,?,?,?)",
                       (plan["id"], source_ids[0], None, "assignment_plan", canonical(evidence), "ready", time.time()))
            self.project.ref(db, "assignment_plan", plan["id"], sha, "payload")
            self.project.event(db, "assignment.plan_created", plan["id"])
        return {**plan, "state": "ready"}

    def _candidates(self, source, blocks, report, max_candidates, max_spans):
        by_block = defaultdict(list)
        for match in report["matches"]:
            by_block[match["span"]["block_id"]].append(match)
        editions = sorted({match["edition_id"] for match in report["matches"]})
        incomplete = bool(EVIDENCE_LIMITS.intersection(report["limit_reasons"]))
        with self.project.connect() as db:
            assignments = defaultdict(list)
            for row in db.execute("SELECT block_id,start,end FROM assignments WHERE source_id=?", (source["id"],)):
                assignments[row["block_id"]].append(row)
            labels = {}
            if editions:
                placeholders = ",".join("?" for _ in editions)
                for row in db.execute("SELECT b.id AS book_id,e.id AS edition_id,b.title,e.label "
                                      "FROM editions e JOIN books b ON b.id=e.book_id "
                                      f"WHERE e.id IN ({placeholders})", editions):
                    labels[row["book_id"], row["edition_id"]] = (row["title"], row["label"])
        pending = defaultdict(list)
        for row in source["dispositions"]:
            if row["kind"] == "pending":
                pending[row["block_id"]].append(row)
        hints = {}
        for index, segment in enumerate(source["metadata"].get("suggested_segments", [])):
            for block_id in segment["block_ids"]:
                hints[block_id] = (index, segment["title"])
        boundary_starts = {item["position"]["block_id"] for item in report["boundaries"]
                           if item["position"]["start"] == 0}

        items, last_hint, count = [], None, 0
        for block in blocks:
            text = block["text"]
            block_id = block["id"]
            hint = hints.get(block_id, (-1, source["metadata"].get("title") or source["name"]))
            if block_id in boundary_starts:
                last_hint = None
            if not text and block["type"] not in STRUCTURED:
                continue
            if block["type"] in STRUCTURED and not any(row["start"] == 0 and row["end"] == len(text) for row in pending[block_id]):
                last_hint = None
                continue
            quotations = [match.span() for match in EXPLICIT_QUOTES.finditer(text)] if by_block[block_id] else []
            previous_end = None
            for window in pending[block_id]:
                for start, end in _subtract(window["start"], window["end"], assignments[block_id]):
                    if block["type"] in STRUCTURED and (start, end) != (0, len(text)):
                        last_hint = None
                        continue
                    if start != (previous_end if previous_end is not None else 0):
                        last_hint = None
                    previous_end = end
                    for left, right, matches in _parts(start, end, by_block[block_id]):
                        selected = {**span(source["id"], block_id, left, right), "preview": text[left:min(right, left + 240)]}
                        quoted = any(a <= left < right <= b for a, b in quotations)
                        item = self._candidate(selected, hint[1], matches, labels, incomplete, quoted)
                        merge = last_hint == hint and item["kind"] == "unassigned" and len(items[-1]["spans"]) < MAX_GROUP_SPANS
                        if count == max_spans or (not merge and len(items) == max_candidates):
                            return self._identify(items, source["blocks_sha"]), True
                        if merge:
                            items[-1]["spans"].append(selected)
                        else:
                            items.append(item)
                        count += 1
                        last_hint = hint if item["kind"] == "unassigned" else None
            if previous_end != len(text):
                last_hint = None
        return self._identify(items, source["blocks_sha"]), False

    @staticmethod
    def _candidate(selected, title, matches, labels, incomplete, quoted):
        grouped = defaultdict(list)
        for match in matches:
            book, edition = match["book_id"], match["edition_id"]
            offset = selected["start"] - match["span"]["start"]
            length = selected["end"] - selected["start"]
            for proof in match["evidence"]:
                origin = proof["span"]
                evidence = {"span": {**origin, "start": origin["start"] + offset, "end": origin["start"] + offset + length},
                            "preview": selected["preview"], "match_chars": length, "relation": proof["relation"]}
                if len(grouped[book, edition]) < 3 and evidence not in grouped[book, edition]:
                    grouped[book, edition].append(evidence)
        alternatives = []
        for (book, edition), evidence in sorted(grouped.items()):
            book_title, edition_label = labels.get((book, edition), (book, edition))
            alternatives.append({"book_id": book, "edition_id": edition, "book_title": book_title,
                                 "edition_label": edition_label, "evidence": evidence})
        suggested = None
        if not alternatives:
            kind, reasons = "unassigned", ["题名是来源线索，请核对范围并选择图书和版本"]
        else:
            reasons = []
            if len(alternatives) > 1:
                reasons.append("同一范围匹配多个图书或版本，须人工选择")
            if quoted or any("可能是引用或局部复用" in match["uncertainty"] for match in matches):
                reasons.append("可能是引用或局部复用，须人工核对")
            if selected["end"] - selected["start"] < 24:
                reasons.append("切分后的直接证据较短，须人工核对")
            if incomplete:
                reasons.append("分析预算未穷尽，须人工选择")
            kind = "ambiguous" if reasons else "matched"
            if kind == "matched":
                suggested = {key: alternatives[0][key] for key in TARGET_FIELDS}
                reasons = ["该范围直接匹配已归属原文，请核对作品与版本"]
        return {"kind": kind, "title_hint": title, "spans": [selected],
                "suggested_target": suggested, "alternatives": alternatives, "reasons": reasons}

    @staticmethod
    def _identify(items, blocks_sha):
        for item in items:
            coordinates = [[value[key] for key in SPAN_FIELDS] for value in item["spans"]]
            targets = [[value[key] for key in TARGET_FIELDS] for value in item["alternatives"]]
            item["id"] = identifier([blocks_sha, coordinates, targets])
        return items

    @staticmethod
    def _find_plan(db, plan_id):
        return db.execute("SELECT state,evidence FROM candidates WHERE id=? AND kind='assignment_plan'", (plan_id,)).fetchone()

    def _plan_row(self, db, plan_id):
        if not _string(plan_id):
            raise DomainError("assignment_plan", "归属计划标识无效")
        row = self._find_plan(db, plan_id)
        if not row:
            raise DomainError("assignment_plan", "归属计划不存在", 404)
        return row, json.loads(row["evidence"])

    def _plan_view(self, row, evidence):
        result = {**self.project.read_json(evidence["payload_sha"]), "state": row["state"]}
        if row["state"] == "applied":
            result["result"] = evidence["result"]
        return result

    def get(self, plan_id):
        with self.project.connect() as db:
            row, evidence = self._plan_row(db, plan_id)
        return self._plan_view(row, evidence)

    @staticmethod
    def _decisions(decisions):
        if not isinstance(decisions, list) or not 1 <= len(decisions) <= MAX_CANDIDATES:
            raise DomainError("assignment_decisions", "请选择本次计划中的候选并确认或拒绝")
        normalized, seen, new_books = [], set(), {}
        for decision in decisions:
            if (not isinstance(decision, dict) or not {"candidate_id", "action"} <= decision.keys() <= {"candidate_id", "action", "target"}
                    or not _string(decision["candidate_id"]) or decision["action"] not in ("confirm", "reject")
                    or decision["candidate_id"] in seen or decision["action"] == "reject" and "target" in decision):
                raise DomainError("assignment_decisions", "每个候选只能明确确认或拒绝一次")
            seen.add(decision["candidate_id"])
            normalized.append(dict(decision))
            if "target" in decision:
                target = _target(decision["target"])
                normalized[-1]["target"] = target
                if "new_book" in target:
                    book = target["new_book"]
                    if new_books.setdefault(book["key"], book) != book:
                        raise DomainError("assignment_target", "同一个新图书标识的资料必须一致")
        return sorted(normalized, key=lambda item: item["candidate_id"])

    @staticmethod
    def _receipt(evidence, decisions_sha):
        if evidence["decisions_sha"] != decisions_sha:
            raise DomainError("assignment_plan_applied", "该归属计划已提交，请重新生成计划", 409)
        return evidence["result"]

    def apply(self, plan_id, decisions, *, job=None):
        decisions = self._decisions(decisions)
        decisions_sha = digest(decisions)
        with self.project.connect() as db:
            self.wb._job_guard(db, job)
            row, evidence = self._plan_row(db, plan_id)
        if row["state"] == "applied":
            return self._receipt(evidence, decisions_sha)
        plan = self.project.read_json(evidence["payload_sha"])
        candidates = {item["id"]: item for source in plan["sources"] for item in source["candidates"]}
        confirmations, rejected = [], []
        for choice in decisions:
            candidate_id = choice["candidate_id"]
            if candidate_id not in candidates:
                raise DomainError("assignment_decisions", "候选不属于当前归属计划")
            item = candidates[candidate_id]
            if choice["action"] == "reject":
                rejected.append(candidate_id)
                continue
            target = choice.get("target", item["suggested_target"])
            if target is None:
                raise DomainError("assignment_target", "请为待核对范围明确选择图书和版本")
            confirmations.append((candidate_id, target, item["spans"]))
        with self.project.transaction() as db:
            self.wb._job_guard(db, job)
            row, evidence = self._plan_row(db, plan_id)
            if row["state"] == "applied":
                return self._receipt(evidence, decisions_sha)
            self._ensure_current(db, plan)
            created, assigned_ids = {}, []
            for _, target, ranges in confirmations:
                target = self._resolve_target(db, target, created)
                self.wb._assign(db, target["book_id"], target["edition_id"], ranges)
                for value in ranges:
                    assigned_ids.append(db.execute(
                        "SELECT id FROM assignments WHERE source_id=? AND block_id=? AND start=? AND end=? AND book_id=? AND edition_id=?",
                        (*(value[key] for key in SPAN_FIELDS), target["book_id"], target["edition_id"])).fetchone()[0])
            confirmed = [candidate_id for candidate_id, _, _ in confirmations]
            result = {"plan_id": plan_id, "state": "applied", "confirmed": len(confirmed), "rejected": len(rejected),
                      "deferred": len(candidates) - len(decisions), "confirmed_ids": confirmed, "rejected_ids": rejected,
                      "assignment_ids": assigned_ids, "created_books": list(created.values())}
            evidence.update(result=result, decisions_sha=decisions_sha)
            db.execute("UPDATE candidates SET state='applied',evidence=? WHERE id=?", (canonical(evidence), plan_id))
            self.project.event(db, "assignment.plan_applied", plan_id, {"confirmed": len(confirmed), "rejected": len(rejected)})
        return result

    def _resolve_target(self, db, target, created):
        if "new_book" not in target:
            edition = self.project.row("editions", target["edition_id"], db)
            if edition["book_id"] != target["book_id"]:
                raise DomainError("assignment_target", "所选版本不属于目标图书")
            return target
        book = target["new_book"]
        key = book["key"]
        if key not in created:
            book_id, edition_id = uid(), uid()
            db.execute("INSERT INTO books(id,title,author,language,created) VALUES(?,?,?,?,?)",
                       (book_id, book["title"], book["author"], book["language"], time.time()))
            db.execute("INSERT INTO editions(id,book_id,label,language) VALUES(?,?,?,?)",
                       (edition_id, book_id, book["edition"], book["language"]))
            self.project.event(db, "book.created", book_id)
            created[key] = {"key": key, "book_id": book_id, "edition_id": edition_id}
        return created[key]

    @staticmethod
    def _assignment_rows(db, clause, args):
        return db.execute(
            "SELECT a.*,b.title AS book_title,e.label AS edition_label FROM assignments a "
            "JOIN books b ON b.id=a.book_id JOIN editions e ON e.id=a.edition_id WHERE " + clause, args)

    @staticmethod
    def _assignment_preview(row, source, blocks):
        start, end = row["start"], row["end"]
        block = blocks.get(row["block_id"])
        if block is None or not 0 <= start <= end <= len(block["text"]):
            raise DomainError("assignment_provenance", "已归属范围与来源不一致，需先核查书库")
        return {**dict(row), "blocks_sha": source["blocks_sha"], "preview": block["text"][start:min(end, start + 240)]}

    def assignments(self, source_id, offset=0, limit=100):
        if (not _string(source_id) or type(offset) is not int or not 0 <= offset < 2**63
                or type(limit) is not int or not 1 <= limit <= 500):
            raise DomainError("assignment_page", "归属列表的来源或分页参数无效")
        source = self.wb.source(source_id)
        blocks = {item["id"]: item for item in source["blocks"]}
        with self.project.connect() as db:
            db.execute("BEGIN")
            total = db.execute("SELECT count(*) FROM assignments WHERE source_id=?", (source_id,)).fetchone()[0]
            rows = self._assignment_rows(db, "a.source_id=? ORDER BY a.rowid DESC LIMIT ? OFFSET ?", (source_id, limit, offset)).fetchall()
        return {"items": [self._assignment_preview(row, source, blocks) for row in rows],
                "total": total, "offset": offset, "next_offset": offset + limit if offset + limit < total else None,
                "blocks_sha": source["blocks_sha"]}

    def change_assignment(self, id, expected, target=None):
        fields = (*SPAN_FIELDS, *TARGET_FIELDS, "blocks_sha")
        if (not _string(id) or not isinstance(expected, dict) or set(expected) != set(fields)
                or any(type(expected[key]) is not int if key in {"start", "end"} else not _string(expected[key]) for key in fields)):
            raise DomainError("assignment_expected", "归属快照无效，请刷新当前来源")
        if target is not None:
            target = _target(target)
            if "new_book" in target:
                raise DomainError("assignment_target", "更正时请选择已有图书和版本")
        source = self.wb.source(expected["source_id"])
        blocks = {item["id"]: item for item in source["blocks"]}
        with self.project.transaction() as db:
            current = self._assignment_rows(db, "a.id=?", (id,)).fetchone()
            if (not current or any(current[key] != expected[key] for key in fields[:-1])
                    or self.project.row("sources", expected["source_id"], db)["blocks_sha"] != expected["blocks_sha"]):
                raise DomainError("assignment_changed", "该归属已更新，请刷新后再操作", 409)
            if target is None:
                db.execute("DELETE FROM assignments WHERE id=?", (id,))
                self._restore_pending(db, current)
                self.project.event(db, "assignment.removed", id, {key: current[key] for key in fields[:-1]})
                return {"assignment": None, "removed_id": id}
            self._resolve_target(db, target, {})
            if all(current[key] == target[key] for key in TARGET_FIELDS):
                return {"assignment": self._assignment_preview(current, source, blocks), "removed_id": None}
            existing = self._assignment_rows(
                db, "a.source_id=? AND a.block_id=? AND a.start=? AND a.end=? AND a.book_id=? AND a.edition_id=?",
                (*(current[key] for key in SPAN_FIELDS), *(target[key] for key in TARGET_FIELDS))).fetchone()
            if existing:
                db.execute("DELETE FROM assignments WHERE id=?", (id,))
                updated = existing
            else:
                new_id = uid()
                db.execute("UPDATE assignments SET id=?,book_id=?,edition_id=?,created=? WHERE id=?",
                           (new_id, target["book_id"], target["edition_id"], time.time(), id))
                updated = self._assignment_rows(db, "a.id=?", (new_id,)).fetchone()
            self.project.event(db, "assignment.corrected", id, {"previous": {key: current[key] for key in fields[:-1]},
                                                                 "replacement_id": updated["id"], **target})
            return {"assignment": self._assignment_preview(updated, source, blocks), "removed_id": id}

    def _restore_pending(self, db, removed):
        source_id, block_id, start, end = (removed[key] for key in SPAN_FIELDS)
        remaining = db.execute("SELECT start,end FROM assignments WHERE source_id=? AND block_id=?", (source_id, block_id)).fetchall()
        auto_used = db.execute("SELECT start,end FROM dispositions WHERE source_id=? AND block_id=? AND kind='used' AND reason=?",
                               (source_id, block_id, AUTO_USED)).fetchall()
        for left, right in _subtract(start, end, remaining):
            for row in auto_used:
                restore_left, restore_right = max(left, row["start"]), min(right, row["end"])
                if restore_left < restore_right or left == right == row["start"] == row["end"]:
                    self.wb._dispose(db, source_id, block_id, restore_left, restore_right, "pending", "")
