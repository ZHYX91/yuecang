from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import time
from pathlib import Path

from yuecang.storage.project import Project, DomainError, canonical, digest, uid


JSON_FIELDS = {"metadata", "issues", "operations", "evidence", "payload", "checkpoint", "result", "report"}


def unpack(row):
    result = dict(row)
    for key in JSON_FIELDS & result.keys():
        if result[key] is not None and isinstance(result[key], str):
            result[key] = json.loads(result[key])
    return result


class Workbench:
    def __init__(self, project: Project | str | Path):
        self.project = project if isinstance(project, Project) else Project(project)

    def state(self):
        out = {}
        with self.project.connect() as db:
            db.execute("BEGIN")
            for name in ("sources", "books", "editions", "branches", "revisions", "proposals", "jobs", "exports", "candidates", "assignments", "review_requests"):
                rows = db.execute(f"SELECT * FROM {name} ORDER BY rowid DESC LIMIT 1000").fetchall()
                out[name] = [unpack(row) for row in rows]
                out.setdefault("totals", {})[name] = db.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
            for job in out["jobs"]:
                job.pop("token", None)
                job.pop("payload", None)
                job.pop("checkpoint", None)
            out["state_counts"] = {
                table: dict(db.execute(f"SELECT state,count(*) FROM {table} GROUP BY state"))
                for table in ("jobs", "proposals")
            }
            out["event_id"] = db.execute("SELECT coalesce(max(id),0) FROM events").fetchone()[0]
            for generation in ("assignment_generation", "disposition_generation"):
                out[generation] = int(db.execute("SELECT value FROM meta WHERE key=?", (generation,)).fetchone()[0])
        out["settings"], out["storage"] = self.project.settings(), self.project.storage_status()
        return out

    def list_page(self, table, *, offset=0, limit=100, search="", source_id=None, book_id=None,
                  edition_id=None, kind=None, state=None):
        if table not in {"sources", "books", "jobs", "revisions", "exports", "proposals", "review_requests", "editions", "branches", "candidates", "assignments"}:
            raise DomainError("collection", "未知列表")
        limit, offset = min(max(int(limit), 1), 500), max(0, int(offset))
        if offset >= 2**63:
            raise DomainError("collection_page", "分页偏移量超出支持范围")
        conditions, args = [], []
        searchable = {
            "sources": ["name", "json_extract(metadata,'$.title')"], "books": ["title", "author"],
            "editions": ["label", "translator"], "branches": ["name"],
            "revisions": ["id", "json_extract(metadata,'$.title')"], "jobs": ["id", "kind"],
            "exports": ["id", "revision_id", "(SELECT json_extract(metadata,'$.title') FROM revisions WHERE id=exports.revision_id)"],
            "proposals": ["id", "revision_id"], "review_requests": ["id", "revision_id"],
        }.get(table, [])
        if search and searchable:
            conditions.append("(" + " OR ".join(f"{field} LIKE ? ESCAPE '\\'" for field in searchable) + ")")
            escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            args.extend([f"%{escaped}%"] * len(searchable))
        available = {
            "sources": set(), "books": set(), "jobs": {"kind", "state"}, "revisions": {"book_id", "edition_id"},
            "exports": set(), "proposals": {"state"}, "review_requests": set(), "editions": {"book_id"},
            "branches": {"book_id"}, "candidates": {"source_id", "kind", "state"},
            "assignments": {"source_id", "book_id", "edition_id"},
        }
        requested = {"source_id": source_id, "book_id": book_id, "edition_id": edition_id, "kind": kind, "state": state}
        if table == "sources" and (book_id or edition_id):
            scope = ["a.source_id=sources.id"]
            for field in ("book_id", "edition_id"):
                if requested[field]:
                    scope.append(f"a.{field}=?")
                    args.append(requested[field])
                    requested[field] = None
            conditions.append("EXISTS(SELECT 1 FROM assignments a WHERE " + " AND ".join(scope) + ")")
        for field, value in requested.items():
            if value is None:
                continue
            if field not in available[table]:
                raise DomainError("collection_filter", "该列表不支持此筛选条件")
            if table == "jobs" and field == "state":
                from yuecang.jobs import ACTIVE, TERMINAL
                valid_states = ACTIVE | TERMINAL
                states = value.split(",", maxsplit=len(valid_states))
                if len(states) > len(valid_states) or not set(states) <= valid_states:
                    raise DomainError("collection_filter", "任务状态筛选包含无效值")
                conditions.append("state IN (" + ",".join("?" for _ in states) + ")")
                args.extend(states)
            else:
                conditions.append(f"{field}=?")
                args.append(value)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self.project.connect() as db:
            db.execute("BEGIN")
            total = db.execute(f"SELECT count(*) FROM {table}{where}", args).fetchone()[0]
            rows = db.execute(f"SELECT * FROM {table}{where} ORDER BY rowid DESC LIMIT ? OFFSET ?", [*args, limit, offset]).fetchall()
        items = [unpack(r) for r in rows]
        for r in items:
            r.pop("token", None)
            if table == "jobs":
                r.pop("payload", None)
                r.pop("checkpoint", None)
        return {"items": items, "total": total, "offset": offset, "next_offset": offset + limit if offset + limit < total else None}

    def source(self, source_id):
        row = unpack(self.project.row("sources", source_id))
        row["blocks"] = self.project.read_json(row["blocks_sha"]) if row["blocks_sha"] else []
        for block in row["blocks"]:
            block["source_id"] = source_id
        with self.project.connect() as db:
            row["dispositions"] = [dict(x) for x in db.execute("SELECT * FROM dispositions WHERE source_id=? ORDER BY block_id,start", (source_id,))]
        return row

    def revision(self, revision_id, *, summary=False):
        row = unpack(self.project.row("revisions", revision_id))
        if not summary:
            row["blocks"] = self.project.read_json(row["document_sha"])
        with self.project.connect() as db:
            branch = db.execute("SELECT id FROM branches WHERE head_revision_id=?", (revision_id,)).fetchone()
            row["branch_id"] = branch[0] if branch else None
            if not summary:
                row["changes"] = [unpack(r) for r in db.execute("SELECT * FROM operations WHERE output_revision=?", (revision_id,))]
        return row

    def _job_guard(self, db, job):
        if job is None:
            return
        row = db.execute("SELECT state,token,lease_until FROM jobs WHERE id=?", (job["id"],)).fetchone()
        if not row or row["state"] != "running" or row["token"] != job["token"] or (row["lease_until"] or 0) < time.time():
            raise DomainError("stale_job", "任务已暂停、取消或失去执行租约", 409)

    def import_file(self, path, *, name=None, ocr=False, encoding="auto", repair="none", doc_converter="auto", derived_from=None, expected_blocks_sha=None, job=None):
        from yuecang.core.encoding import validate_options, MAX_RECOVERY_BYTES
        try:
            validate_options(encoding, repair)
        except ValueError as exc:
            raise DomainError("text_options", str(exc)) from exc
        if doc_converter not in {"auto", "libreoffice", "word", "wps"}:
            raise DomainError("doc_converter", "未知 DOC 转换器")
        path = Path(path)
        if path.is_symlink() or not path.is_file():
            raise DomainError("input_path", "导入源必须为普通文件")
        name = Path(name or path.name).name
        suffix = Path(name).suffix.lower()
        if (encoding != "auto" or repair != "none") and suffix not in {".txt", ".md", ".markdown"}:
            raise DomainError("encoding_format", "编码选择和文字修复仅适用于 TXT / Markdown")
        if (repair != "none" or derived_from) and path.stat().st_size > MAX_RECOVERY_BYTES:
            raise DomainError("text_recovery_limit", "文字恢复单次上限为 16 MiB")
        if derived_from:
            parent = self.source(derived_from)
            if parent["blocks_sha"] != expected_blocks_sha or self.project.hash_file(path) != parent["asset_sha"] or name != parent["name"]:
                raise DomainError("source_changed", "来源与预览不一致，请重新预览", 409)
        owner = f"{job['id']}:{job['token']}" if job else "import:" + uid()
        with path.open("rb") as stream:
            asset = self.project.put_stream(stream, "source", owner=owner)
        if derived_from and asset != parent["asset_sha"]:
            raise DomainError("source_changed", "来源与预览不一致，请重新预览", 409)
        parser_key = "formats-v2:" + suffix + ":" + digest([ocr, encoding, repair, doc_converter, derived_from])
        with self.project.connect() as db:
            old = db.execute("SELECT id,block_count FROM sources WHERE asset_sha=? AND (parser=? OR parser LIKE ?) ORDER BY (block_count>0) DESC,created DESC LIMIT 1", (asset, parser_key, parser_key + ":retry:%")).fetchone()
        if old and old["block_count"]:
            with self.project.transaction() as db:
                self._job_guard(db, job)
                db.execute("INSERT INTO imports VALUES(?,?,?,?)", (uid(), old[0], name, time.time()))
                self.project.event(db, "import.duplicate", old[0])
            return {"source_id": old[0], "duplicate": True}
        if old:
            # A previously unavailable converter or failed decoder is not a
            # permanent cached success. Keep that failed extraction immutable.
            parser_key += ":retry:" + uid()
        from yuecang.formats import extract
        from yuecang.formats.common import resource_budget
        from yuecang.storage.maintenance import register_temporary, forget_temporary
        scratch = self.project.root / "tmp" / ("extract-" + uid())
        reservation = uid()
        registration = None
        self.project.reserve(reservation, self.project.object_path(asset).stat().st_size, owner)
        try:
            scratch.mkdir()
            if job:
                registration = register_temporary(self.project, scratch, job["id"])
            local_input = scratch / name
            # Preserve extension for format detection while keeping the immutable source authoritative.
            shutil.copyfile(self.project.object_path(asset), local_input)
            def reserve_extraction(size):
                with self.project.connect() as db:
                    self._job_guard(db, job)
                self.project.reserve(reservation, size, owner)
            def cancel_check():
                with self.project.connect() as db:
                    self._job_guard(db, job)
                return False
            with resource_budget(scratch / "resources", reserve=reserve_extraction,
                                 initial_bytes=local_input.stat().st_size):
                result = extract(local_input, ocr=ocr, resource_dir=scratch / "resources", encoding=encoding, repair=repair,
                                 doc_converter=doc_converter, cancel_check=cancel_check if job else None)
            blocks = copy.deepcopy(result.blocks)
            resources = []
            for block in blocks:
                if not isinstance(block.get("id"), str) or not isinstance(block.get("text"), str):
                    raise DomainError("extract_contract", "解析器返回非法内容块")
                for resource in block.get("resources", []):
                    if resource.get("path"):
                        rp = Path(resource["path"])
                        if rp.is_symlink() or not rp.resolve().is_relative_to(scratch.resolve()):
                            raise DomainError("resource_path", "解析资源越出任务范围")
                        with rp.open("rb") as f:
                            resource["sha256"] = self.project.put_stream(f, "resource", owner=owner)
                        resource.pop("path", None)
                        resources.append(resource["sha256"])
            if len({b["id"] for b in blocks}) != len(blocks):
                raise DomainError("extract_contract", "内容块标识重复")
            blocks_sha = self.project.put_json(blocks, "extraction", owner=owner)
            source_id = uid()
            metadata = dict(result.metadata)
            metadata["title"] = result.title or Path(name).stem
            metadata["extractor"] = result.parser
            if derived_from:
                metadata["derived_from"] = derived_from
                metadata["recovery_options"] = {"encoding": encoding, "repair": repair, "parent_blocks_sha": expected_blocks_sha}
            metadata["suggested_segments"] = self._suggest_segments(blocks, result.title or Path(name).stem)
            with self.project.transaction() as db:
                self._job_guard(db, job)
                db.execute("INSERT INTO sources(id,asset_sha,name,status,blocks_sha,block_count,metadata,issues,parser,created) VALUES(?,?,?,?,?,?,?,?,?,?)", (source_id, asset, name, "ready" if blocks else "needs_review", blocks_sha, len(blocks), canonical(metadata), canonical(result.issues), parser_key, time.time()))
                db.execute("INSERT INTO imports VALUES(?,?,?,?)", (uid(), source_id, name, time.time()))
                self.project.ref(db, "source", source_id, asset, "original")
                self.project.ref(db, "source", source_id, blocks_sha, "extraction")
                for sha in resources:
                    self.project.ref(db, "source", source_id, sha, "resource")
                for block in blocks:
                    db.execute("INSERT INTO dispositions VALUES(?,?,?,?,?,?,?)", (uid(), source_id, block["id"], 0, len(block["text"]), "pending", ""))
                for token in self._tokens(blocks):
                    db.execute("INSERT OR IGNORE INTO fingerprints VALUES(?,?)", (source_id, token))
                self.project.event(db, "source.imported", source_id)
            return {"source_id": source_id, "duplicate": False, "blocks": len(blocks), "issues": result.issues}
        finally:
            # Exact directory generated above, no user data or links traversed.
            try:
                if scratch.exists():
                    shutil.rmtree(scratch)
                if registration:
                    forget_temporary(self.project, registration)
            finally:
                self.project.release_reservation(reservation, owner)

    def text_preview(self, source_id, *, encoding="auto", repair="none"):
        from yuecang.core.encoding import preview, MAX_RECOVERY_BYTES
        source = self.source(source_id)
        if Path(source["name"]).suffix.lower() not in {".txt", ".md", ".markdown"}:
            raise DomainError("encoding_format", "编码恢复仅适用于 TXT / Markdown")
        path = self.project.object_path(source["asset_sha"])
        if path.stat().st_size > MAX_RECOVERY_BYTES:
            raise DomainError("text_recovery_limit", "文字恢复单次上限为 16 MiB")
        raw = path.read_bytes()
        if digest(raw) != source["asset_sha"]:
            raise DomainError("corrupt_object", "原件校验失败")
        try:
            result = preview(raw, encoding, repair)
        except ValueError as exc:
            raise DomainError("text_decode", str(exc)) from exc
        current_text = "".join(b["text"] for b in source["blocks"])
        result["original_preview"] = current_text[:8000]
        result["changed"] = result["text_sha256"] != hashlib.sha256(current_text.encode()).hexdigest()
        return {**result, "source_id": source_id, "expected_blocks_sha": source["blocks_sha"]}

    @staticmethod
    def _tokens(blocks):
        tokens = set()
        for block in blocks:
            text = block["text"]
            if len(text) >= 12:
                for i in range(0, len(text) - 11, max(1, len(text) // 128)):
                    tokens.add(hashlib.sha256(text[i:i + 12].encode()).hexdigest()[:24])
            elif text.strip():
                tokens.add(hashlib.sha256(text.encode()).hexdigest()[:24])
        return sorted(tokens)[:512]

    @staticmethod
    def _suggest_segments(blocks, fallback):
        groups, current = [], {"title": fallback, "block_ids": [], "reason": "文件题名候选，需确认"}
        for block in blocks:
            match = re.match(r"^\s*《([^》]+)》", block["text"])
            if match and (block["type"] == "heading" or len(block["text"]) < 120):
                if current["block_ids"]:
                    groups.append(current)
                current = {"title": match[1], "block_ids": [], "reason": "显式书名边界候选，需确认"}
            current["block_ids"].append(block["id"])
        if current["block_ids"]:
            groups.append(current)
        return groups

    def create_book(self, title, author="", language="zh", edition="未确定版本"):
        if not isinstance(title, str) or not title.strip():
            raise DomainError("book_title", "请填写图书名称")
        book_id, edition_id = uid(), uid()
        with self.project.transaction() as db:
            db.execute("INSERT INTO books(id,title,author,language,created) VALUES(?,?,?,?,?)", (book_id, title.strip(), author, language, time.time()))
            db.execute("INSERT INTO editions(id,book_id,label,language) VALUES(?,?,?,?)", (edition_id, book_id, edition or "未确定版本", language))
            self.project.event(db, "book.created", book_id)
        return {"book": self.project.row("books", book_id), "edition": self.project.row("editions", edition_id), "id": book_id, "edition_id": edition_id}

    def create_edition(self, book_id, label, language="zh", translator=""):
        self.project.row("books", book_id)
        if not label.strip():
            raise DomainError("edition_label", "版本名称不能为空")
        edition_id = uid()
        with self.project.transaction() as db:
            db.execute("INSERT INTO editions(id,book_id,label,language,translator) VALUES(?,?,?,?,?)", (edition_id, book_id, label, language, translator))
            self.project.event(db, "edition.created", edition_id, {"book_id": book_id})
        return self.project.row("editions", edition_id)

    def _edition(self, book_id, edition_id=None):
        if not isinstance(book_id, str) or edition_id is not None and not isinstance(edition_id, str):
            raise DomainError("edition", "图书及版本标识必须为文字")
        if edition_id:
            ed = self.project.row("editions", edition_id)
        else:
            with self.project.connect() as db:
                row = db.execute("SELECT * FROM editions WHERE book_id=? ORDER BY rowid LIMIT 1", (book_id,)).fetchone()
                if not row:
                    raise DomainError("edition", "图书尚无版本")
                ed = dict(row)
        if ed["book_id"] != book_id:
            raise DomainError("edition", "版本不属于目标图书")
        return ed

    def _selections(self, selections):
        if not isinstance(selections, list) or not selections:
            raise DomainError("selection", "请至少选择一个来源片段")
        sources, blocks_by_source, normalized = {}, {}, []
        for item in selections:
            if not isinstance(item, dict) or any(not isinstance(item.get(key), str) or not item[key] for key in ("source_id", "block_id")):
                raise DomainError("selection", "来源片段须包含有效来源和内容块标识")
            sid = item.get("source_id")
            if sid not in sources:
                sources[sid] = self.source(sid)
                blocks_by_source[sid] = {b["id"]: b for b in sources[sid]["blocks"]}
            block = blocks_by_source[sid].get(item["block_id"])
            if block is None:
                raise DomainError("source_block", "来源内容块不存在")
            start, end = item.get("start", 0), item.get("end", len(block["text"]))
            if start is None: start = 0
            if end is None: end = len(block["text"])
            if any(type(v) is not int for v in (start, end)) or not 0 <= start <= end <= len(block["text"]):
                raise DomainError("span", "来源范围越界")
            if start == end and block["type"] not in {"figure", "unknown", "formula", "table"}:
                raise DomainError("span", "文本片段不能为空")
            if block["type"] in {"figure", "unknown", "formula", "table"} and (start != 0 or end != len(block["text"])):
                raise DomainError("structured_span", "图像、公式和表格须整块选入；不能用文字范围截断其结构")
            normalized.append({"source_id": sid, "block_id": block["id"], "start": start, "end": end, "block": block, "source": sources[sid]})
        return normalized

    def _assign(self, db, book_id, edition_id, selections):
        for s in selections:
            db.execute("INSERT OR IGNORE INTO assignments VALUES(?,?,?,?,?,?,?,?)", (uid(), s["source_id"], s["block_id"], s["start"], s["end"], book_id, edition_id, time.time()))
            self._dispose(db, s["source_id"], s["block_id"], s["start"], s["end"], "used", "已关联图书")

    def assign(self, book_id, edition_id=None, spans=None):
        edition = self._edition(book_id, edition_id)
        normalized = self._selections(spans)
        with self.project.transaction() as db:
            self._assign(db, book_id, edition["id"], normalized)
            self.project.event(db, "source.assigned", book_id)
        return {"assigned": len(normalized)}

    @staticmethod
    def _dispose(db, source_id, block_id, start, end, kind, reason):
        rows = db.execute("SELECT * FROM dispositions WHERE source_id=? AND block_id=? ORDER BY start", (source_id, block_id)).fetchall()
        if not rows:
            raise DomainError("disposition", "来源处置范围不存在")
        if start < rows[0]["start"] or end > rows[-1]["end"]:
            raise DomainError("disposition", "处置范围越界")
        rebuilt = []
        for r in rows:
            a, b = r["start"], r["end"]
            if b <= start or a >= end:
                if a == b == start == end:
                    rebuilt.append((a, b, kind, reason))
                else:
                    rebuilt.append((a, b, r["kind"], r["reason"]))
                continue
            if a < start: rebuilt.append((a, start, r["kind"], r["reason"]))
            rebuilt.append((max(a, start), min(b, end), kind, reason))
            if b > end: rebuilt.append((end, b, r["kind"], r["reason"]))
        db.execute("DELETE FROM dispositions WHERE source_id=? AND block_id=?", (source_id, block_id))
        for a, b, k, why in rebuilt:
            db.execute("INSERT INTO dispositions VALUES(?,?,?,?,?,?,?)", (uid(), source_id, block_id, a, b, k, why))

    def dispose(self, source_id, block_id, start, end, kind, reason=""):
        if kind not in {"used", "attachment", "duplicate", "ignored", "pending"}:
            raise DomainError("disposition", "未知处置类型")
        self._selections([{ "source_id": source_id, "block_id": block_id, "start": start, "end": end}])
        with self.project.transaction() as db:
            self._dispose(db, source_id, block_id, start, end, kind, reason)
        return {"ok": True}

    def assemble(self, book_id, selections, edition_id=None, title=None, job=None, allow_composite=False,
                 *, precondition=None, record_extra=None, assign_selected=True, commit_hook=None, selection_editions=None):
        if type(allow_composite) is not bool:
            raise DomainError("assembly_policy", "综合整理版选项必须为布尔值")
        if title is not None:
            if not isinstance(title, str):
                raise DomainError("assembly_title", "整理稿标题必须为文字")
            try:
                title.encode("utf-8")
            except UnicodeError as exc:
                raise DomainError("assembly_title", "整理稿标题包含无效字符") from exc
        book = self.project.row("books", book_id)
        edition = self._edition(book_id, edition_id)
        selected = self._selections(selections)
        if selection_editions is not None and (not allow_composite or assign_selected or precondition is None
                                              or len(selection_editions) != len(selected)):
            raise DomainError("assembly_provenance", "选稿来源版本与组稿方案不一致")
        frozen_editions = {}
        plan_editions = (record_extra or {}).get("source_editions", {})
        blocks, issues, seen = [], [], set()
        for i, s in enumerate(selected):
            key = (s["source_id"], s["block_id"], s["start"], s["end"])
            if key in seen:
                raise DomainError("duplicate_selection", "同一来源范围被重复选入，请去除重复")
            seen.add(key)
            block = copy.deepcopy(s["block"])
            block.update(id=uid(), text=block["text"][s["start"]:s["end"]], source_id=s["source_id"], source_span={k: s[k] for k in ("source_id", "block_id", "start", "end")})
            block["provenance"] = [{"kind": "copy", "output_start": 0, "output_end": len(block["text"]), "source": block["source_span"], "text_sha256": hashlib.sha256(block["text"].encode()).hexdigest()}]
            if selection_editions is not None:
                ranges = selection_editions[i]
                if not ranges or any(r["edition_id"] not in plan_editions or not s["start"] <= r["start"] <= r["end"] <= s["end"] for r in ranges):
                    raise DomainError("assembly_provenance", "选稿来源版本与组稿方案不一致")
                # These coordinates describe the source at assembly time. They
                # remain useful even after later edits change the output block.
                block["source_edition_ranges"] = [{"source_id": s["source_id"], "block_id": s["block_id"],
                                                   "start": r["start"], "end": r["end"], "edition_id": r["edition_id"]}
                                                  for r in ranges]
                for r in ranges:
                    edition_id_at_assembly = r["edition_id"]
                    frozen_editions[edition_id_at_assembly] = plan_editions[edition_id_at_assembly]
            blocks.append(block)
            for issue in s["source"]["issues"]:
                marked = copy.deepcopy(issue)
                marked.setdefault("severity", "warning")
                marked["source_id"] = s["source_id"]
                if marked not in issues:
                    issues.append(marked)
        rev_id, branch_id = uid(), uid()
        metadata = {"title": title or book["title"], "author": book["author"], "language": edition["language"], "edition": edition["label"], "translator": edition["translator"], "source_ids": sorted({s["source_id"] for s in selected}), "completeness": "known_sources_only", "assembly_policy": "composite-confirmed" if allow_composite else "single-edition"}
        if frozen_editions:
            metadata["source_editions"] = frozen_editions
        sha = self.project.put_json(blocks, "revision")
        with self.project.transaction() as db:
            self._job_guard(db, job)
            if precondition:
                precondition(db)
            for selected_span in selected:
                other = db.execute("SELECT edition_id FROM assignments WHERE source_id=? AND block_id=? AND ((start<? AND end>?) OR (start=end AND start=? AND ?=?)) AND edition_id<>?",
                                   (selected_span["source_id"], selected_span["block_id"], selected_span["end"], selected_span["start"],
                                    selected_span["start"], selected_span["start"], selected_span["end"], edition["id"])).fetchall()
                if other:
                    issues.append({"code": "edition_conflict", "severity": "warning" if allow_composite else "blocking",
                                   "message": "已明确选择综合整理版；保留其他版本归属和原始出处" if allow_composite else "所选片段已关联其他版本，请明确是否制作综合整理版",
                                   "source_id": selected_span["source_id"], "other_edition_ids": sorted({r["edition_id"] for r in other})})
            db.execute("INSERT INTO revisions VALUES(?,?,?,?,?,?,?,?,?)", (rev_id, book_id, edition["id"], None, sha, canonical(metadata), "draft", canonical(issues), time.time()))
            db.execute("INSERT INTO branches VALUES(?,?,?,?,?)", (branch_id, book_id, "整理稿 " + time.strftime("%m-%d %H:%M"), rev_id, 0))
            if assign_selected:
                self._assign(db, book_id, edition["id"], selected)
            self._revision_refs(db, rev_id, sha, blocks)
            operation = {"type": "assemble", "selections": [{k: s[k] for k in ("source_id", "block_id", "start", "end")} for s in selected]}
            if record_extra is not None:
                operation["guided_plan"] = record_extra
            db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?)", (uid(), None, rev_id, "user", None, canonical(operation), time.time()))
            self.project.event(db, "revision.created", rev_id)
            if commit_hook:
                commit_hook(db, {"revision_id": rev_id, "branch_id": branch_id})
        return {"revision_id": rev_id, "branch_id": branch_id, "revision": self.revision(rev_id), "branch": self.project.row("branches", branch_id)}

    def _revision_refs(self, db, revision_id, sha, blocks):
        self.project.ref(db, "revision", revision_id, sha, "document")
        for block in blocks:
            for resource in block.get("resources", []):
                if resource.get("sha256"):
                    self.project.ref(db, "revision", revision_id, resource["sha256"], "resource")

    def edit(self, branch_id, expected_revision_id, operations, *, actor="user", request_id=None, proposal_id=None):
        branch = self.project.row("branches", branch_id)
        if branch["head_revision_id"] != expected_revision_id:
            raise DomainError("revision_conflict", "整理稿已变化，请刷新后比较再提交", 409)
        revision = self.revision(expected_revision_id)
        if not isinstance(operations, list) or not operations:
            raise DomainError("operations", "没有待保存的修改")
        blocks, checked = copy.deepcopy(revision["blocks"]), []
        # Each operation is anchored to this revision, so a block may be edited only once per transaction.
        touched = set()
        for op in operations:
            block = next((b for b in blocks if b["id"] == op.get("block_id")), None)
            if block is None or block["id"] in touched:
                raise DomainError("edit_block", "内容块不存在或一次提交中重复编辑同一块")
            touched.add(block["id"])
            start, end, text = op.get("start", 0), op.get("end", len(block["text"])), op.get("text", op.get("new_text"))
            if any(type(x) is not int for x in (start, end)) or not isinstance(text, str) or not 0 <= start <= end <= len(block["text"]):
                raise DomainError("edit_span", "编辑范围或替换内容非法")
            old_text = block["text"][start:end]
            old_sha = hashlib.sha256(old_text.encode()).hexdigest()
            if op.get("expected_sha256") and old_sha != op["expected_sha256"]:
                raise DomainError("revision_conflict", "修改前的文字与建议不一致", 409)
            if "old_text" in op and op["old_text"] != old_text:
                raise DomainError("revision_conflict", "原文已变化", 409)
            before = block["text"]
            block["text"] = before[:start] + text + before[end:]
            if block["type"] in {"table", "formula", "figure", "unknown"} or block.get("resources"):
                block.setdefault("attributes", {})["structure_stale"] = True
                for resource in block.get("resources", []):
                    resource["role"] = "pre_edit_source"
            prev_id = block["id"]
            block["id"] = uid()
            provenance = []
            for a, b, c, d in ((0, start, 0, start), (start + len(text), len(block["text"]), end, len(before))):
                if b > a:
                    provenance.append({"kind": "copy", "output_start": a, "output_end": b, "revision_id": expected_revision_id, "block_id": prev_id, "start": c, "end": d})
            operation_id = uid()
            if text:
                provenance.append({"kind": "edit", "output_start": start, "output_end": start + len(text), "operation_id": operation_id, "actor": actor, "evidence": op.get("evidence", [])})
            block["provenance"] = sorted(provenance, key=lambda p: p["output_start"])
            block.pop("source_span", None)
            checked.append({"id": operation_id, "type": "delete" if not text else "replace", "input_block": prev_id, "output_block": block["id"], "start": start, "end": end, "old_text": old_text, "text": text, "expected_sha256": old_sha, "evidence": op.get("evidence", []), "proposal_id": proposal_id})
        new_id, sha = uid(), self.project.put_json(blocks, "revision")
        with self.project.transaction() as db:
            current = self.project.row("branches", branch_id, db)
            if current["head_revision_id"] != expected_revision_id or current["version"] != branch["version"]:
                raise DomainError("revision_conflict", "整理稿已变化，修改未应用", 409)
            if proposal_id:
                proposal = self.project.row("proposals", proposal_id, db)
                if proposal["state"] != "pending":
                    raise DomainError("proposal_state", "提案已经处理", 409)
            db.execute("INSERT INTO revisions VALUES(?,?,?,?,?,?,?,?,?)", (new_id, revision["book_id"], revision["edition_id"], expected_revision_id, sha, canonical(revision["metadata"]), "draft", canonical(revision["issues"]), time.time()))
            db.execute("UPDATE branches SET head_revision_id=?,version=version+1 WHERE id=? AND version=?", (new_id, branch_id, branch["version"]))
            for operation in checked:
                db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?)", (operation["id"], expected_revision_id, new_id, actor, request_id, canonical(operation), time.time()))
            self._revision_refs(db, new_id, sha, blocks)
            if proposal_id:
                db.execute("UPDATE proposals SET state='accepted' WHERE id=?", (proposal_id,))
            self.project.event(db, "revision.edited", new_id)
        return {"revision_id": new_id, "branch_id": branch_id, "revision": self.revision(new_id)}

    def select_revision(self, branch_id, expected_revision_id, revision_id):
        revision = self.project.row("revisions", revision_id)
        with self.project.transaction() as db:
            branch = self.project.row("branches", branch_id, db)
            if branch["book_id"] != revision["book_id"]:
                raise DomainError("revision_book", "不能选择另一图书的修订")
            if branch["head_revision_id"] != expected_revision_id:
                raise DomainError("revision_conflict", "分支已变化", 409)
            db.execute("UPDATE branches SET head_revision_id=?,version=version+1 WHERE id=?", (revision_id, branch_id))
            self.project.event(db, "branch.selected", branch_id)
        return {"revision_id": revision_id, "branch_id": branch_id}

    def finalize(self, revision_id):
        revision = self.revision(revision_id)
        self.validate_revision(revision)
        with self.project.transaction() as db:
            pending = db.execute("""WITH RECURSIVE ancestors(id,parent_id) AS (
                SELECT id,parent_id FROM revisions WHERE id=?
                UNION ALL SELECT r.id,r.parent_id FROM revisions r JOIN ancestors a ON r.id=a.parent_id
                ) SELECT count(*) FROM proposals p JOIN branches b ON b.id=p.branch_id
                WHERE p.state='pending' AND p.risk='high' AND p.revision_id IN (SELECT id FROM ancestors)
                AND (p.revision_id=? OR b.head_revision_id=?)""", (revision_id, revision_id, revision_id)).fetchone()[0]
            if pending or any(i.get("severity") == "blocking" for i in revision["issues"]):
                raise DomainError("unresolved", "仍有高风险提案或版本冲突未处理")
            db.execute("UPDATE revisions SET status='final' WHERE id=?", (revision_id,))
            db.execute("UPDATE books SET default_revision_id=?,version=version+1 WHERE id=?", (revision_id, revision["book_id"]))
            self.project.event(db, "revision.finalized", revision_id)
        return {"revision_id": revision_id, "status": "final"}

    def validate_revision(self, revision):
        if not revision["blocks"]:
            raise DomainError("empty_revision", "整理稿没有内容")
        ids = [b.get("id") for b in revision["blocks"]]
        if any(not isinstance(i, str) or not i for i in ids) or len(ids) != len(set(ids)):
            raise DomainError("provenance", "整理稿块标识缺失或重复")
        ancestry = {revision["id"]: revision}
        parent_id = revision.get("parent_id")
        while parent_id:
            if parent_id in ancestry:
                raise DomainError("provenance", "整理稿父修订关系存在循环")
            parent = self.revision(parent_id)
            if parent["book_id"] != revision["book_id"]:
                raise DomainError("provenance", "父修订属于另一图书")
            ancestry[parent_id] = parent
            parent_id = parent.get("parent_id")
        for block in revision["blocks"]:
            if not isinstance(block.get("text"), str) or not isinstance(block.get("provenance"), list):
                raise DomainError("provenance", "整理稿正文或来源结构非法")
            for p in block["provenance"]:
                if not isinstance(p, dict) or any(type(p.get(k)) is not int for k in ("output_start", "output_end")):
                    raise DomainError("provenance", "来源区间必须使用Unicode码点整数")
            intervals = sorted((p["output_start"], p["output_end"]) for p in block.get("provenance", []))
            cursor = 0
            for start, end in intervals:
                if start != cursor or end < start or end > len(block["text"]):
                    raise DomainError("provenance", "整理稿存在缺失或重叠的来源区间")
                cursor = end
            if cursor != len(block["text"]):
                raise DomainError("provenance", "整理稿来源没有覆盖全文")
            for p in block.get("provenance", []):
                output = block["text"][p["output_start"]:p["output_end"]]
                if p.get("text_sha256") and p["text_sha256"] != hashlib.sha256(output.encode()).hexdigest():
                    raise DomainError("source_drift", "来源片段哈希不匹配")
                origins = sum(bool(p.get(key)) for key in ("source", "revision_id", "operation_id"))
                if origins != 1:
                    raise DomainError("provenance", "每个来源区间必须且只能有一个证据对象")
                if p.get("source"):
                    if p.get("kind") != "copy":
                        raise DomainError("provenance", "来源复制类型非法")
                    s = self._selections([p["source"]])[0]
                    original = s["block"]["text"][s["start"]:s["end"]]
                    if original != output:
                        raise DomainError("source_drift", "复制内容与来源不一致")
                elif p.get("revision_id"):
                    if p.get("kind") != "copy" or p["revision_id"] == revision["id"] or p["revision_id"] not in ancestry:
                        raise DomainError("provenance", "复制片段必须引用本书的祖先修订")
                    parent = ancestry[p["revision_id"]]
                    original = next((b for b in parent["blocks"] if b["id"] == p["block_id"]), None)
                    a, b = p.get("start"), p.get("end")
                    if original is None or type(a) is not int or type(b) is not int or not 0 <= a <= b <= len(original["text"]) or original["text"][a:b] != output:
                        raise DomainError("source_drift", "父修订引用不一致")
                elif p.get("kind") == "edit":
                    with self.project.connect() as db:
                        operation_row = db.execute("SELECT * FROM operations WHERE id=?", (p.get("operation_id"),)).fetchone()
                        if not operation_row:
                            raise DomainError("provenance", "编辑操作记录缺失")
                    operation = unpack(operation_row)
                    change = json.loads(operation["payload"]) if isinstance(operation["payload"], str) else operation["payload"]
                    if (operation["output_revision"] not in ancestry or operation["input_revision"] not in ancestry
                            or change.get("output_block") != block["id"] or change.get("text") != output
                            or change.get("start") != p["output_start"] or operation["actor"] != p.get("actor")
                            or change.get("evidence", []) != p.get("evidence", [])):
                        raise DomainError("provenance", "编辑来源与操作记录不一致")
                    input_revision = ancestry[operation["input_revision"]]
                    previous = next((b for b in input_revision["blocks"] if b["id"] == change.get("input_block")), None)
                    a, b = change.get("start"), change.get("end")
                    if previous is None or type(a) is not int or type(b) is not int or not 0 <= a <= b <= len(previous["text"]):
                        raise DomainError("provenance", "编辑操作的原文范围不存在")
                    if (previous["text"][a:b] != change.get("old_text")
                            or hashlib.sha256(previous["text"][a:b].encode()).hexdigest() != change.get("expected_sha256")
                            or previous["text"][:a] + change["text"] + previous["text"][b:] != block["text"]):
                        raise DomainError("source_drift", "编辑内容不能由记录中的原文和替换重建")
                else:
                    raise DomainError("provenance", "未知来源类型")
            for resource in block.get("resources", []):
                if resource.get("sha256") and self.project.hash_file(self.project.object_path(resource["sha256"])) != resource["sha256"]:
                    raise DomainError("resource_hash", "资源哈希不一致")

    def analyze(self, source_id=None, book_id=None, job=None):
        source_ids = [source_id] if source_id else []
        if book_id:
            with self.project.connect() as db:
                source_ids = [r[0] for r in db.execute("SELECT DISTINCT source_id FROM assignments WHERE book_id=?", (book_id,))]
        if not source_ids:
            with self.project.connect() as db:
                source_ids = [r[0] for r in db.execute("SELECT id FROM sources ORDER BY created DESC LIMIT 100")]
        suggestions = []
        for sid in source_ids:
            source = self.source(sid)
            with self.project.connect() as db:
                rows = db.execute("SELECT f.source_id,count(*) AS overlap FROM fingerprints f JOIN fingerprints mine ON mine.token=f.token WHERE mine.source_id=? AND f.source_id<>? GROUP BY f.source_id ORDER BY overlap DESC LIMIT 51", (sid, sid)).fetchall()
            if len(rows) > 50:
                suggestions.append({"kind": "search_incomplete", "source_id": sid, "message": "候选超过本次预算，结果未覆盖全部来源"})
            for row in rows[:50]:
                other = self.source(row["source_id"])
                left = "\n".join(b["text"] for b in source["blocks"])
                right = "\n".join(b["text"] for b in other["blocks"])
                kind = "exact_text" if left == right else "related_content"
                evidence = {"overlap": row["overlap"], "left_title": source["metadata"].get("title"), "right_title": other["metadata"].get("title"), "message": "内容相似仅是候选，须确认图书与版本；不会自动合并"}
                candidate = {"id": digest([sid, row["source_id"], kind])[:32], "source_id": sid, "other_source_id": row["source_id"], "kind": kind, "evidence": evidence}
                with self.project.transaction() as db:
                    self._job_guard(db, job)
                    db.execute("INSERT OR IGNORE INTO candidates VALUES(?,?,?,?,?,?,?)", (candidate["id"], sid, row["source_id"], kind, canonical(evidence), "pending", time.time()))
                suggestions.append(candidate)
            suggestions.extend({"kind": "boundary", "source_id": sid, **s} for s in source["metadata"].get("suggested_segments", []))
        return {"suggestions": suggestions, "automatic_merge": False}

    def compare(self, left_source_id, right_source_id):
        from yuecang.core.text_diff import compare_texts
        left, right = self.source(left_source_id), self.source(right_source_id)
        a = "\n".join(b["text"] for b in left["blocks"])
        b = "\n".join(b["text"] for b in right["blocks"])
        return compare_texts(a, b)

    def export(self, revision_id, format="markdown", allow_draft=False, job=None):
        from yuecang.export import export_revision, verify_export
        format = {"md": "markdown", "text": "txt"}.get(format.lower(), format.lower())
        if format not in {"markdown", "txt", "epub"}:
            raise DomainError("export_format", "不支持的导出格式")
        revision = self.revision(revision_id)
        if revision["status"] != "final" and not allow_draft:
            raise DomainError("draft_export", "整理稿尚未定版；请明确选择草稿导出")
        self.validate_revision(revision)
        for block in revision["blocks"]:
            for resource in block.get("resources", []):
                if resource.get("sha256"):
                    resource["path"] = str(self.project.object_path(resource["sha256"]))
        export_id = job["id"] if job else uid()
        destination = self.project.root / "exports" / export_id
        with self.project.transaction() as db:
            self._job_guard(db, job)
            saved = db.execute("SELECT * FROM exports WHERE id=?", (export_id,)).fetchone()
        if saved:
            if saved["revision_id"] != revision_id or saved["format"] != format:
                raise DomainError("export_conflict", "此任务已经导出了另一整理稿或格式", 409)
            verify_export(destination)
            return {"export_id": export_id, "report": json.loads(saved["report"]), "duplicate": True}
        if destination.exists():
            if destination.is_symlink() or not destination.resolve().is_relative_to((self.project.root / "exports").resolve()):
                raise DomainError("export_path", "导出目录不安全")
            if (destination / "manifest.json").is_file():
                manifest = verify_export(destination)
                snapshot = json.loads((destination / "revision.json").read_text(encoding="utf-8"))
                if (manifest["revision_id"] != revision_id or manifest["format"] != format
                        or snapshot.get("document_sha") != revision["document_sha"] or snapshot.get("metadata") != revision["metadata"]):
                    raise DomainError("export_conflict", "已有导出件与冻结整理稿不匹配", 409)
                report = {**manifest, "destination": str(destination), "manifest_path": str(destination / "manifest.json"),
                          "output_path": str(destination / {"markdown": "book.md", "txt": "book.txt", "epub": "book.epub"}[format])}
            else:
                # The exact job-owned directory has no completion manifest.
                # Never follow an unexpected link while reclaiming partial output.
                if not job or any(p.is_symlink() or getattr(p.lstat(), "st_file_attributes", 0) & 0x400 for p in destination.rglob("*")):
                    raise DomainError("export_incomplete", "未完成导出目录需先处理")
                shutil.rmtree(destination)
                report = export_revision(revision, destination, format)
        else:
            report = export_revision(revision, destination, format)
        report["revision_status"] = revision["status"]
        for issue in revision["issues"]:
            if issue not in report["issues"]:
                report["issues"].append(issue)
        with self.project.transaction() as db:
            self._job_guard(db, job)
            db.execute("INSERT INTO exports VALUES(?,?,?,?,?,?)", (export_id, revision_id, format, destination.relative_to(self.project.root).as_posix(), canonical(report), time.time()))
            self.project.event(db, "export.completed", export_id)
        return {"export_id": export_id, "report": report}
