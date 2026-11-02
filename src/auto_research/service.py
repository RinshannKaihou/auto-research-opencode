"""Private stdio RPC for native DSH research state and migration operations.

The process owns transactional local state, but has no scheduler, model client,
or HTTP socket. The host supplies a fixed registration map, never a path from a
browser or model.
"""

from __future__ import annotations

from . import __version__, frozen_refs, workbench_read

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile

from .artifacts import ArtifactStore, _open_source
from .migration import preflight, recovery_preview
from .native_store import NativeStore, SCHEMA_VERSION
from .memory_store import KINDS, STATUSES
from .errors import ConflictError, ValidationError

MAX_REQUEST = 64 * 1024
MAX_RESPONSE = 4 * 1024 * 1024
MAX_PREVIEW = 64 * 1024
_SECRET_KEY = re.compile(
    r"(authorization|cookie|api[_-]?key|access[_-]?token|secret|password)", re.I
)
_SECRET_TEXT = re.compile(
    r"(?i)((?:authorization|cookie|api[_-]?key|access[_-]?token|password|secret)"
    r"\s*[=:]\s*[\"\']?)([^\s\"\',;}]+(?:\s+[^\s\"\',;}]+)?|[^\n]+)"
)


def redact(value):
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _SECRET_KEY.search(key) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _SECRET_TEXT.sub(lambda m: m[1] + "[REDACTED]", value)
    return value


class ReadOnlyService:
    def __init__(self, registrations: dict[str, str]):
        self.roots = {
            key: Path(value).expanduser().resolve() for key, value in registrations.items()
        }

    def handle(self, request: dict) -> dict:
        request_id = request.get("request_id")
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            raise ValueError("request_id must be a nonempty string of at most 128 characters")
        project_id = request.get("project_id")
        if project_id not in self.roots:
            raise ValueError("Unknown project registration")
        root = self.roots[project_id]
        method = request.get("method")
        if method == "status":
            value = preflight(root, files=False)
            # Paths/errors/commands are retained; credentials alone are redacted.
        elif method == "inventory":
            value = preflight(root)
        elif method == "recovery_preview":
            value = recovery_preview(root, request.get("attempt_id"))
        elif method == "preview":
            path = request.get("path")
            if not isinstance(path, str) or Path(path).is_absolute():
                raise ValueError("Preview requires a relative .research path")
            if path.split("/")[0] not in {"workspaces", "objects", "jobs"}:
                raise ValueError(
                    "Only research workspaces, objects, and legacy jobs are previewable"
                )
            with _open_source(path, root / ".research") as fd:
                import os

                content = os.read(fd, MAX_PREVIEW + 1)
            if b"\0" in content:
                raise ValueError("Binary file; use the inventory digest")
            value = {
                "path": path,
                "text": content[:MAX_PREVIEW].decode("utf-8", errors="replace"),
                "truncated": len(content) > MAX_PREVIEW,
                "limit_bytes": MAX_PREVIEW,
                "provenance": "legacy-file; not-native-transcript",
            }
        else:
            raise ValueError("Read-only service: method is not available")
        return {
            "request_id": request_id,
            "project_id": project_id,
            "ok": True,
            "value": redact(value),
        }


class ProjectRegistry:
    """Host-owned mapping from native sessions to research roots."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY, root TEXT NOT NULL UNIQUE,
                    registered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    host_id TEXT NOT NULL, session_id TEXT NOT NULL,
                    project_id TEXT NOT NULL REFERENCES projects(project_id),
                    associated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    detached_at TEXT,
                    PRIMARY KEY(host_id, session_id, associated_at)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_live_project_per_session
                    ON sessions(host_id, session_id) WHERE detached_at IS NULL;
                """
            )

    def _connection(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def register(self, project_id: str, root: Path, host_id: str, session_id: str) -> None:
        root_text = str(root.resolve())
        with self._connection() as db:
            db.execute(
                "INSERT INTO projects(project_id,root) VALUES (?,?) "
                "ON CONFLICT(project_id) DO UPDATE SET root=excluded.root",
                (project_id, root_text),
            )
            live = db.execute(
                "SELECT project_id FROM sessions WHERE host_id=? AND session_id=? "
                "AND detached_at IS NULL",
                (host_id, session_id),
            ).fetchone()
            if live and live["project_id"] != project_id:
                raise ValueError("Native session is already associated with another project")
            if not live:
                db.execute(
                    "INSERT INTO sessions(host_id,session_id,project_id) VALUES (?,?,?)",
                    (host_id, session_id, project_id),
                )

    def detach(self, host_id: str, session_id: str) -> None:
        with self._connection() as db:
            db.execute(
                "UPDATE sessions SET detached_at=CURRENT_TIMESTAMP "
                "WHERE host_id=? AND session_id=? AND detached_at IS NULL",
                (host_id, session_id),
            )

    def root_for(self, host_id: str, session_id: str) -> Path:
        with self._connection() as db:
            row = db.execute(
                "SELECT p.root FROM sessions s JOIN projects p USING(project_id) "
                "WHERE s.host_id=? AND s.session_id=? AND s.detached_at IS NULL",
                (host_id, session_id),
            ).fetchone()
        if not row:
            raise ValueError("Native session is not associated with a research project")
        return Path(row["root"])

    def live_sessions(self, host_id: str) -> list[dict]:
        """Registry-only view of one host's associated sessions; opens no project."""
        with self._connection() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT s.session_id,s.project_id,p.root FROM sessions s "
                    "JOIN projects p USING(project_id) "
                    "WHERE s.host_id=? AND s.detached_at IS NULL ORDER BY s.associated_at",
                    (host_id,),
                )
            ]

    def sessions_for(self, host_id: str, session_id: str) -> list[dict]:
        with self._connection() as db:
            project = db.execute(
                "SELECT project_id FROM sessions WHERE host_id=? AND session_id=? "
                "AND detached_at IS NULL",
                (host_id, session_id),
            ).fetchone()
            if not project:
                raise ValueError("Native session is not associated with a research project")
            return [
                dict(row)
                for row in db.execute(
                    "SELECT host_id,session_id,associated_at FROM sessions "
                    "WHERE project_id=? AND detached_at IS NULL ORDER BY associated_at",
                    (project["project_id"],),
                )
            ]


class NativeService:
    """Private schema-5 research service. It never invokes a model or schedules work."""

    def __init__(self, registry: str | Path):
        self.registry = ProjectRegistry(registry)
        self.stores = {}
        from .evidence_read import EvidenceCache
        self.evidence = EvidenceCache()

    @staticmethod
    def _identity(request: dict) -> tuple[str, str]:
        host_id = request.get("host_id")
        session_id = request.get("session_id")
        if not isinstance(host_id, str) or not host_id:
            raise ValueError("host_id is required")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id is required")
        return host_id, session_id

    @staticmethod
    def _operation(request: dict) -> str:
        value = request.get("operation_id")
        if not isinstance(value, str) or not value or len(value) > 256:
            raise ValueError("operation_id must be a nonempty string of at most 256 characters")
        return value

    def _store(self, request: dict) -> tuple[NativeStore, Path, str, str]:
        host_id, session_id = self._identity(request)
        root = self.registry.root_for(host_id, session_id)
        store = self.stores.setdefault(str(root), None)
        if store is None:
            store = self.stores[str(root)] = NativeStore(root)
        return store, root, host_id, session_id

    @staticmethod
    def _intent(root: Path, operation_id: str, kind: str) -> tuple[Path, dict | None]:
        digest = hashlib.sha256(operation_id.encode()).hexdigest()
        directory = root / ".research" / f"{kind}-intents" / digest
        manifest = directory / "manifest.json"
        if manifest.is_file():
            return manifest, json.loads(manifest.read_text())
        directory.mkdir(parents=True, exist_ok=True)
        return manifest, None

    @staticmethod
    def _write_intent(path: Path, value: dict) -> None:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
        descriptor, temporary = tempfile.mkstemp(prefix="manifest-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _safe_source(source: object) -> str:
        if not isinstance(source, str) or not source:
            raise ValueError("source_path must be nonempty text")
        if Path(source).is_absolute() or ".." in Path(source).parts:
            raise ValueError("Archive paths must be relative to the native session cwd")
        parts = Path(source).parts
        lowered = {part.lower() for part in parts}
        if ".research" in lowered or ".git" in lowered:
            raise ValueError("Runtime control directories and Git metadata cannot be archived")
        if any(
            part == ".env" or "credential" in part or "secret" in part or "token" in part
            for part in lowered
        ):
            raise ValueError("Potential credential files cannot be archived")
        return source

    def _publish_items(
        self, root: Path, operation_id: str, items: list[dict], cwd=None
    ) -> list[dict]:
        intent_path, saved = self._intent(root, operation_id, "publish")
        if saved is not None:
            return saved["items"]
        if not isinstance(items, list):
            raise ValueError("items must be a list")
        artifacts = ArtifactStore(root)
        fixed = []
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("publication items must be objects")
            value = dict(item)
            source = value.get("source_path")
            if source is not None:
                source = self._safe_source(source)
                product = artifacts.freeze(source, cwd or root)
                value["object_version"] = product["version"]
                value["object_kind"] = product["kind"]
            fixed.append(value)
        self._write_intent(intent_path, {"operation_id": operation_id, "items": fixed})
        return fixed

    def _snapshot_manifest(
        self, root: Path, operation_id: str, paths: object, cwd=None
    ) -> tuple[list[dict], bool]:
        intent_path, saved = self._intent(root, operation_id, "snapshot")
        if saved is not None:
            return saved["manifest"], bool(saved["complete"])
        if not isinstance(paths, list):
            raise ValueError("paths must be a list")
        artifacts = ArtifactStore(root)
        manifest = []
        complete = True
        for source in paths:
            try:
                source = self._safe_source(source)
                product = artifacts.freeze(source, cwd or root)
                manifest.append({"source_path": source, **product, "status": "fixed"})
            except Exception as exc:
                manifest.append({"source_path": source, "status": "incomplete", "error": str(exc)})
                complete = False
        self._write_intent(
            intent_path,
            {"operation_id": operation_id, "manifest": manifest, "complete": complete,},
        )
        return manifest, complete

    def _prepare_branch(
        self, store: NativeStore, root: Path, node_id: object, operation_id: str
    ) -> dict:
        if not isinstance(node_id, str) or not node_id:
            raise ValueError("node_id is required")
        intent_path, saved = self._intent(root, operation_id, "branch")
        if saved is not None:
            return saved
        try:
            selected_node = store.reference_query(node_id, full=True)
        except Exception:
            selected_node = None
        node = selected_node["value"] if selected_node and selected_node["kind"] == "node" else None
        if node is None or node["status"] == "closed":
            raise ValueError(f"Unknown or closed research node: {node_id}")
        refs = list(node["inputs"])
        if node.get("anchor_ref") and node["anchor_ref"] not in refs:
            refs.append(node["anchor_ref"])
        index, materialized = self._resolve_branch_inputs(store, root, refs)
        digest = hashlib.sha256(operation_id.encode()).hexdigest()[:12]
        workspace = ArtifactStore(root).prepare_workspace(f"branch-{digest}", materialized)
        index_path = workspace / "research-inputs.json"
        index_path.write_text(
            json.dumps(
                {
                    "node_id": node_id,
                    "strategy": node.get("strategy", "continue"),
                    "anchor_ref": node.get("anchor_ref"),
                    "declared_inputs": refs,
                    "inputs": index,
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        value = {
            "node_id": node_id,
            "workspace": str(workspace),
            "project_root": str(root),
            "strategy": node.get("strategy", "continue"),
            "anchor_ref": node.get("anchor_ref"),
            "inputs": index,
        }
        self._write_intent(intent_path, value)
        return value

    def _validate_branch_inputs(
        self, store: NativeStore, root: Path, node_id: object
    ) -> dict:
        if not isinstance(node_id, str) or not node_id:
            raise ValueError("node_id is required")
        try:
            selected = store.reference_query(node_id, full=True)
        except Exception as exc:
            raise ValueError(f"Unknown research node: {node_id}") from exc
        if selected["kind"] != "node" or selected["value"]["status"] == "closed":
            raise ValueError(f"Unknown or closed research node: {node_id}")
        node = selected["value"]
        refs = list(node["inputs"])
        if node.get("anchor_ref") and node["anchor_ref"] not in refs:
            refs.append(node["anchor_ref"])
        index, _ = self._resolve_branch_inputs(store, root, refs)
        return {"node_id": node_id, "declared_inputs": refs, "inputs": index}

    @staticmethod
    def _resolve_branch_inputs(
        store: NativeStore, root: Path, refs: list[str]
    ) -> tuple[list[dict], list[dict]]:
        """Resolve declared inputs for both dispatch validation and materialization."""
        materialized = []
        index = []
        seen: set[str] = set()

        def add_frozen(declared_ref, concrete_ref):
            if concrete_ref in seen:
                return
            seen.add(concrete_ref)
            with store._read() as db:
                resolution, source = frozen_refs.open(db, root, concrete_ref)
            content_only = (
                resolution["kind"] == "publication_item"
                and resolution["reason"] == "not_an_object"
            )
            if resolution["outcome"] != "resolved" and not content_only:
                raise ValueError(resolution["message"])
            selected = store.reference_query(concrete_ref, full=True)
            item = selected["value"]
            entry = {"ref": concrete_ref, "declared_ref": declared_ref, "materialized": False,
                     "kind": item.get("kind", selected["kind"]), "content": item.get("content"),
                     "resolution": resolution}
            if resolution.get("publication_id"):
                entry["publication_id"] = resolution["publication_id"]
            if source is not None:
                obj = resolution["object"]
                token = hashlib.sha256(concrete_ref.encode()).hexdigest()[:16]
                product = {**obj, "path": f".research/objects/{obj['version']}"}
                if resolution.get("subpath"):
                    product["subpath"] = resolution["subpath"]
                materialized.append({"node_id": "input", "product_id": f"ref-{token}", "product": product})
                entry.update(materialized=True, path=f"inputs/input/ref-{token}")
            index.append(entry)

        for ref in refs:
            product = None
            if frozen_refs.is_frozen(ref):
                with store._read() as db:
                    resolution = frozen_refs.resolve(db, root, ref)
                    if resolution["outcome"] != "resolved":
                        raise ValueError(resolution["message"])
                    if resolution["kind"] == "snapshot":
                        raise ValueError(
                            f"Research reference cannot be used as an input: {ref} (snapshot)"
                        )
                    items = list(db.execute("SELECT item_id FROM publication_items WHERE publication_id=? ORDER BY item_id",
                                            (resolution["publication_id"],))) if resolution["kind"] == "publication" else None
                if items is not None:
                    for item in items:
                        add_frozen(ref, f"pub/{resolution['publication_id']}#{item['item_id']}")
                else:
                    add_frozen(ref, ref)
                continue
            try:
                selected = store.reference_query(ref, full=True)
            except Exception as exc:
                raise ValueError(f"Research input does not exist: {ref}") from exc
            entry = {"ref": ref, "declared_ref": ref, "materialized": False}
            if selected["kind"] == "legacy-ref":
                old = selected["value"]
                entry.update({"kind": old["kind"], "legacy": True})
                if old["kind"] == "product" and isinstance(old["item"], dict):
                    product = {key: old["item"].get(key) for key in ("path", "version", "kind")}
            elif selected["kind"] in {"node", "knowledge"}:
                entry.update({"kind": selected["kind"]})
            else:
                raise ValueError(
                    f"Research reference cannot be used as an input: {ref} ({selected['kind']})"
                )
            if product and all(product.values()):
                token = hashlib.sha256(ref.encode()).hexdigest()[:16]
                materialized.append(
                    {"node_id": "input", "product_id": f"ref-{token}", "product": product,}
                )
                entry["materialized"] = True
                entry["path"] = f"inputs/input/ref-{token}"
            index.append(entry)
        return index, materialized

    def restore_preview(self, store, root, snapshot_id, session_id):
        selected = store.reference_query(snapshot_id, full=True)
        if selected["kind"] != "snapshot":
            raise ValueError("Unknown snapshot")
        snapshot = selected["value"]
        attempt = store.reference_query(snapshot["attempt_id"], full=True)["value"]
        manifest = snapshot["manifest"]
        with store._connection() as db:
            saved = db.execute(
                "SELECT context FROM snapshot_handoffs WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
        context = (
            json.loads(saved["context"])
            if saved
            else {
                "goal": store.control_state()["project"]["goal"],
                "source_attempt_id": attempt["attempt_id"],
                "source_node_id": attempt["node_id"],
                "files": manifest,
                "provenance": "legacy-snapshot; no contemporaneous handoff summary",
            }
        )
        context["snapshot_id"] = snapshot_id
        token = hashlib.sha256(
            json.dumps([snapshot, session_id], sort_keys=True).encode()
        ).hexdigest()
        return {
            "preview_id": token,
            "snapshot_id": snapshot_id,
            "manifest": manifest,
            "workspace": str(root / "workspaces" / ("restore-" + token[:16])),
            "complete": snapshot["complete"],
            "context": context,
            "eligible": attempt["ended_at"] is not None and attempt["state"] != "unknown",
            "missing": [m for m in manifest if m.get("status") != "fixed"],
        }

    def prepare_discussion(self, store, root, host_id, session_id, request):
        selected = store.reference_query(request["node_id"], full=True)
        node = selected["value"] if selected["kind"] == "node" else None
        if node is None:
            raise ValueError("Unknown discussion node")
        if not request.get("fresh"):
            with store._connection() as db:
                row = db.execute(
                    "SELECT * FROM workflow_sessions WHERE role='discussion' AND detached=0 "
                    "AND node_id=? ORDER BY rowid DESC LIMIT 1",
                    (node["node_id"],),
                ).fetchone()
                existing = dict(row) if row else None
                if existing:
                    existing["context"] = json.loads(existing["context"])
            if existing:
                return {
                    "session_id": existing["session_id"],
                    "workspace": existing["cwd"],
                    "context": existing["context"],
                    "existing": True,
                }
        intent, saved = self._intent(root, self._operation(request), "discussion")
        if saved:
            return saved
        digest = hashlib.sha256(self._operation(request).encode()).hexdigest()[:20]
        workspace = root / "workspaces" / ("discussion-" + digest)
        workspace.mkdir(parents=True, exist_ok=True)
        refs = set(node.get("inputs", []))
        referenced_publications = {
            parsed["publication_id"] for ref in refs
            if (parsed := frozen_refs.parse(ref)).get("publication_id")
        }
        with store._connection() as db:
            attempt_ids = {
                row["attempt_id"]
                for row in db.execute(
                    "SELECT attempt_id FROM attempts WHERE node_id=?", (node["node_id"],)
                )
            }
            publication_rows = list(
                db.execute(
                    "SELECT rowid AS _cursor,* FROM publications WHERE node_id=? "
                    "OR publication_id IN (SELECT value FROM json_each(?)) ORDER BY rowid",
                    (node["node_id"], json.dumps(sorted(referenced_publications))),
                )
            )
            publications = [
                store._decode_page_row(db, "publications", row, full=True)
                for row in publication_rows
            ]
            notes = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM notes WHERE attempt_id IN (SELECT value FROM json_each(?)) ORDER BY rowid",
                    (json.dumps(sorted(attempt_ids)),),
                )
            ]
            goal = store._project(db)["goal"]
        context = {
            "role": "discussion",
            "goal": goal,
            "node": node,
            "publications": publications,
            "notes": notes,
            "instruction": "Discuss these frozen references. Query for newer records explicitly. Do not mutate the research ledger or dispatch research.",
        }
        material_refs = [f"pub/{p['publication_id']}" for p in publications]
        # Discussion ignores context-only inputs. Dispatch validation remains
        # stricter; a whole snapshot is neither expanded nor materialized here.
        material_refs.extend(
            ref for ref in node.get("inputs", [])
            if ref not in material_refs
            and frozen_refs.parse(ref).get("form") in {
                "publication", "publication_item", "snapshot_entry"
            }
        )
        index, materialized = self._resolve_branch_inputs(store, root, material_refs)
        artifacts = ArtifactStore(root)
        for item in materialized:
            destination = workspace / "inputs" / item["node_id"] / item["product_id"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            artifacts.materialize(item["product"], destination, readonly=True)
        context["material_index"] = index
        # Keep the full frozen record on disk; IPC and system context carry a bounded view.
        self._write_intent(workspace / "RESEARCH_CONTEXT.json", context)
        if len(json.dumps(context, ensure_ascii=False).encode()) > 24000:
            context = {
                "role": "discussion",
                "goal": context["goal"][:2000],
                "node": {k: str(node[k])[:2000] for k in ("node_id", "question", "plan", "status")},
                "record_path": "RESEARCH_CONTEXT.json",
                "publication_ids": [p["publication_id"] for p in publications][-40:],
                "instruction": context["instruction"]
                + " Read RESEARCH_CONTEXT.json for the full frozen material index and background.",
            }
        value = {
            "session_id": "discussion-" + digest,
            "workspace": str(workspace),
            "context": context,
            "existing": False,
        }
        self._write_intent(intent, value)
        return value

    def _asserted_at(self, store, request, host_id, session_id):
        if "asserted_at" in request:
            raise ValidationError("asserted_at is server-managed")
        turn = request.get("turn")
        if turn is None:
            with store._read() as db:
                turn = db.execute("SELECT MAX(turn) FROM turn_bindings WHERE host_id=? AND session_id=?", (host_id, session_id)).fetchone()[0]
        return dict(host_id=host_id, session_id=session_id, turn=turn, operation_id=self._operation(request))

    @staticmethod
    def _host_root(request: dict, method: str) -> Path:
        root_value = request.get("root")
        if not isinstance(root_value, str) or not Path(root_value).is_absolute():
            raise ValueError(f"{method} requires an absolute host-supplied root")
        return Path(root_value).expanduser().resolve()

    def project_read(self, request: dict) -> dict:
        """Browse a project by root through a read-only store; needs no session."""
        root = self._host_root(request, "project_read")
        if not (root / ".research" / "state.sqlite3").is_file():
            return {"project": None, "project_root": str(root)}
        store = NativeStore(root, readonly=True)
        view = request.get("view")
        if view == 'evidence':
            return self.evidence.page(store, **{key: request[key] for key in ('refs', 'include_materials', 'draft_id', 'snapshot_id', 'cursor', 'limit', 'scope_id') if key in request})
        if view == 'page' and request.get('collection') in {'exploration_scopes', 'exploration_routes', 'exploration_decisions', 'exploration_results', 'exploration_intents'}:
            return store.exploration_page(request['collection'], **{key: request[key] for key in
                ('cursor', 'limit', 'ref', 'scope_id') if key in request})
        if view == 'page' and request.get('collection') in {'review_candidates','review_obligations','review_events'}:
            return store.review_page(request['collection'], **{key:request[key] for key in
                ('cursor','limit','node_id','trigger_kind','generation','scope_owner','state','target_ref','change_id','obligation_id') if key in request})
        if view == 'page' and request.get('collection')=='adoptions':
            return store.adoptions(**{key:request[key] for key in ('cursor','limit','node_id') if key in request})
        if view == 'capabilities':
            with store._read() as db:
                version = db.execute('PRAGMA user_version').fetchone()[0]
            return {'schema_version': version, 'memory_write_contract': 'r2-v1' if version>=11 else 'requires_migration',
                    'checkpoint_base': 'receipt-cas-v1' if version >= 11 else 'requires_migration',
                    'review_lifecycle': 'candidate-obligation-v1' if version>=11 else 'requires_migration',
                    'exploration_contract':'exploration-v1' if version >= 13 else 'requires_migration',
                    'multiroute_contract':'multiroute-v1' if version >= 16 else 'requires_migration',
                    'evidence_contract':'evidence-v1' if version >= 14 else 'requires_migration',
                    'backend_version':__version__,
                    'runtime_budget_scope':'plugin autonomous dispatch; token/cost observed subset; external processes unmetered'}
        if view == "context_preview":
            if request.get("model_call"):
                raise ValueError("Context preview requires the host controller")
            return store.context_preview(request.get("preview_session_id"))
        if view == "summary":
            value = workbench_read.summary(store, None, None)
            value.update(
                project_root=str(root),
                knowledge_kinds=sorted(KINDS),
                knowledge_statuses=sorted(STATUSES),
            )
            return value
        if view == "page" and request.get("collection") in {"critical_now", "findings", "completed_findings"}:
            return store.context_page(request["collection"], **{
                key: request[key] for key in ("cursor", "limit", "node_id", "kind", "query", "decision_refs", "final_ref") if key in request})
        if view == "page":
            return workbench_read.page(
                store, request.get("collection"), after=request.get("after"),
                upper_id=request.get("upper_id"), limit=request.get("limit", 20),
                node_id=request.get("node_id"), kind=request.get("kind"),
                state=request.get("state"), trigger_kind=request.get("trigger_kind"), cursor=request.get("cursor"),
            )
        if view == "knowledge":
            return workbench_read.knowledge_page(
                store, query=request.get("query", ""), kind=request.get("kind"),
                status=request.get("status"), node_id=request.get("node_id"),
                knowledge_id=request.get("knowledge_id"), history=request.get("history", False),
                after=request.get("after", 0), upper_id=request.get("upper_id"),
                limit=request.get("limit", 20),
            )
        if view == "presentation":
            return workbench_read.presentation(store, root)
        if view == "reference_entries":
            return workbench_read.reference_entries(
                store, root, request.get("ref"), after=request.get("after", 0),
                limit=request.get("limit", 50),
            )
        if view == "reference_content":
            return workbench_read.reference_content(
                store, root, request.get("ref"), offset=request.get("offset", 0),
                limit=request.get("limit", 32768),
            )
        if view == "reference_get":
            return store.reference_query(request.get("ref"))
        if view == "reference_chunk":
            return store.reference_chunk(
                request.get("ref"), offset=request.get("offset", 0),
                limit=request.get("limit", 8192), expected_digest=request.get("expected_digest"),
            )
        raise ValueError(f"Unknown project view: {view}")

    def handle(self, request: dict) -> dict:
        from .controller import CURRENT_CONTROLLER, CURRENT_MODEL_CALL
        token = CURRENT_CONTROLLER.set(request.get('control_owner') if isinstance(request, dict) else None)
        model_token = CURRENT_MODEL_CALL.set({'host': request.get('host_id'), 'session': request.get('session_id'),
                                              'execution_token': request.get('execution_token')} if isinstance(request, dict) and request.get('model_call') else None)
        try:
            return self._handle(request)
        finally:
            CURRENT_MODEL_CALL.reset(model_token)
            CURRENT_CONTROLLER.reset(token)

    def _handle(self, request: dict) -> dict:
        transport_id = request.get("transport_id", request.get("request_id"))
        store = None
        invocation = None
        if not isinstance(transport_id, str) or not transport_id or len(transport_id) > 128:
            raise ValueError("transport_id must be a nonempty string of at most 128 characters")
        method = request.get("method")
        if method == "capabilities":
            value = {
                "schema_version": SCHEMA_VERSION,
                "execution_owner": "opencode",
                "model_loop": "session-events",
                "manual_research": True,
                "autonomous_research": True,
                "usage_accounting": "observed-native-events-subset",
                "strict_cross_session_read_isolation": False,
                "knowledge_revisions": True,
                "checkpoint_base": "receipt-cas-v1",
                'memory_write_contract':'r2-v1',
                'runtime_control_contract':'controller-launch-v1',
                'review_lifecycle':'candidate-obligation-v1',
                'exploration_contract':'exploration-v1', 'evidence_contract':'evidence-v1',
                'multiroute_contract':'multiroute-v1',
                'evidence_read':'snapshot-delivery-v2', 'backend_version':__version__,
                'runtime_budget_scope':'plugin autonomous dispatch; token/cost observed subset; external processes unmetered',
                "native_specialists": "foreground-one-shot",
            }
        elif method == "project_sessions":
            host_id, session_id = self._identity(request)
            value = self.registry.sessions_for(host_id, session_id)
        elif method == "host_sessions":
            host_id = request.get("host_id")
            if not isinstance(host_id, str) or not host_id:
                raise ValueError("host_id is required")
            value = self.registry.live_sessions(host_id)
        elif method == "session_membership":
            host_id, session_id = self._identity(request)
            try:
                root = self.registry.root_for(host_id, session_id)
            except ValueError:
                value = {'associated': False}
            else:
                readonly = NativeStore(root, readonly=True)
                with readonly._read() as db:
                    row = db.execute('SELECT session_id,role,node_id,detached FROM workflow_sessions WHERE host_id=? AND session_id=?', (host_id, session_id)).fetchone()
                    main = db.execute("SELECT session_id,role,node_id,detached FROM workflow_sessions WHERE host_id=? AND role='main' AND detached=0", (host_id,)).fetchone()
                value = {'associated': True, 'root': str(root), 'session': dict(row) if row else None, 'main': dict(main) if main else None}
        elif method == "project_read":
            value = self.project_read(request)
        elif method == "takeover_main":
            host_id, session_id = self._identity(request)
            root = self._host_root(request, "takeover_main")
            if not (root / ".research" / "state.sqlite3").is_file():
                raise ValueError("No research project exists at this root")
            store = self.stores.get(str(root))
            if store is None:
                store = self.stores[str(root)] = NativeStore(root)
            value = store.takeover_main(
                host_id, session_id, request.get("cwd", str(root)), self._operation(request)
            )
            project_id = store.control_state()["project"]["project_id"]
            self.registry.register(project_id, root, host_id, session_id)
            value = store.control_state(host_id, session_id)
            value["project_root"] = str(root)
        elif method == "specialist_bind_child":
            host_id, parent_session_id = self._identity(request)
            root = self.registry.root_for(host_id, parent_session_id)
            store = self.stores.get(str(root))
            if store is None:
                store = self.stores[str(root)] = NativeStore(root)
            child_session_id = request.get("child_session_id")
            if not isinstance(child_session_id, str) or not child_session_id:
                raise ValueError("child_session_id is required")
            operation_id = self._operation(request)
            project_id = store.control_state()["project"]["project_id"]
            self.registry.register(project_id, root, host_id, child_session_id)
            store.associate(host_id, child_session_id, operation_id + ":associate")
            store.workflow(
                host_id,
                child_session_id,
                "register",
                {
                    "role": "specialist",
                    "cwd": request.get("cwd", str(root)),
                    "node_id": request.get("node_id"),
                    "context": {"specialist_task_id": request.get("task_id")},
                },
                operation_id + ":role",
            )
            value = store.specialist_bind(
                {
                    "task_id": request.get("task_id"),
                    "session_id": child_session_id,
                    "parent_session_id": parent_session_id,
                },
                operation_id,
            )
        elif method == "open":
            host_id, session_id = self._identity(request)
            root_value = request.get("root")
            if not isinstance(root_value, str) or not Path(root_value).is_absolute():
                raise ValueError("open requires an absolute host-supplied root")
            root = Path(root_value).expanduser().resolve()
            store = self.stores.get(str(root))
            if store is None:
                store = self.stores[str(root)] = NativeStore(root)
            try:
                state = store.control_state()
            except Exception as exc:
                if "not initialized" not in str(exc).lower():
                    raise
                state = {"project": store.initialize(request.get("goal"), self._operation(request))}
            project_id = state["project"]["project_id"]
            self.registry.register(project_id, root, host_id, session_id)
            store.associate(host_id, session_id, self._operation(request) + ":associate")
            store.workflow(
                host_id,
                session_id,
                "register",
                {
                    **({"role": request["session_role"]} if request.get("session_role") else {}),
                    "cwd": request.get("cwd", str(root)),
                    "node_id": request.get("node_id"),
                    "context": request.get("context", {}),
                },
                self._operation(request) + ":role",
            )
            value = store.control_state(host_id, session_id)
            value["project_root"] = str(root)
        else:
            store, root, host_id, session_id = self._store(request)
            store.last_replayed = None
            with store._connection() as db:
                row = db.execute(
                    "SELECT * FROM workflow_sessions WHERE session_id=?", (session_id,)
                ).fetchone()
                role = dict(row) if row else None
                association=db.execute('SELECT * FROM associations WHERE host_id=? AND session_id=? AND ended_at IS NULL',(host_id,session_id)).fetchone()
                if association:
                    invocation = store._association_context(db,host_id,session_id,association)
            with store._connection() as db:
                specialist_row = db.execute("SELECT t.* FROM specialist_tasks t JOIN specialist_bindings b USING(task_id) WHERE b.session_id=?", (session_id,)).fetchone()
            if specialist_row and specialist_row["context_mode"] == "blind" and method in {"query", "status", "history_page", "guidance_status", "workbench_summary", "workbench_page", "knowledge_page", "presentation_get", "reference_content", "reference_entries"}:
                raise ValueError("Blind reviewers may only read assigned inputs through research_read_input")
            writes = {
                "focus",
                "propose",
                "note",
                "publish",
                "relate",
                "snapshot",
                "finish",
                "close_node",
                "memory_write",
                "review_scope_set",
                "exploration_scope",
                "exploration_route",
                "exploration_decide",
                "conclusion_prepare",
                "specialist_create",
                "consume",
            }
            if (
                role
                and role["role"] in {"discussion", "handoff", "specialist"}
                and method in writes
            ):
                raise ValueError(
                    f"{role['role'].title()} sessions cannot modify the research ledger"
                )
            if method == "guidance_register" and (not role or role["role"] != "main"):
                raise ValueError("Only the research main session may configure method guidance")
            if request.get("model_call") and method not in writes | {"query", "status", "conclude", "specialist_read_input"}:
                raise ValueError("This operation requires the host controller")
            if request.get("model_call") and method in {"note", "publish", "snapshot"}:
                with store._connection() as db:
                    prior = db.execute(
                        "SELECT 1 FROM requests WHERE request_id=?", (self._operation(request),)
                    ).fetchone()
                    live = db.execute(
                        "SELECT a.* FROM attempts a JOIN associations s USING(association_id) WHERE s.session_id=? AND s.ended_at IS NULL AND a.ended_at IS NULL",
                        (session_id,),
                    ).fetchone()
                if not prior and not live:
                    node_id = role["node_id"] if role else None
                    with store._connection() as db:
                        node = db.execute(
                            "SELECT status FROM nodes WHERE node_id=?", (node_id,)
                        ).fetchone()
                    if node and node["status"] == "closed":
                        node_id = None
                    store.focus(
                        host_id,
                        session_id,
                        node_id,
                        "researcher",
                        "manual",
                        self._operation(request) + ":manual-attempt",
                    )
            if method == "workflow":
                value = store.workflow(
                    host_id,
                    session_id,
                    request["action"],
                    request.get("fields", {}),
                    self._operation(request),
                )
            elif method == "discussion_prepare":
                value = self.prepare_discussion(store, root, host_id, session_id, request)
            elif method == "restore_preview":
                value = self.restore_preview(store, root, request["snapshot_id"], session_id)
            elif method == "host_events":
                value = []
                for event in request.get("events", []):
                    facts = event.get("facts", {})
                    goal = facts.get("goal") if event.get("event_type") == "goal/change" else None
                    if not isinstance(goal, dict) or not goal.get("id"):
                        continue
                    with store._read() as db:
                        owner = db.execute(
                            "SELECT goal_id FROM workflow_sessions WHERE session_id=?",
                            (session_id,),
                        ).fetchone()
                    if owner and owner["goal_id"] == goal["id"]:
                        store.own_goal(
                            host_id,
                            session_id,
                            goal["id"],
                            goal.get("revision", 0),
                            goal.get("phase", "unknown"),
                            f"goal-projection:{session_id}:{event['sequence']}",
                            activation=goal.get("activation"),
                            change_reason=facts.get("reason"),
                            source_sequence=event["sequence"],
                        )
                for event in request.get("events", []):
                    if event["event_type"] != "assistant/message" or not event.get("facts", {}).get(
                        "usage"
                    ):
                        continue
                    facts = event["facts"]
                    with store._connection() as db:
                        observations = list(
                            db.execute(
                                "SELECT u.* FROM usage_observations u JOIN associations a USING(association_id) WHERE a.host_id=? AND a.session_id=? AND u.purpose='conversation' AND json_extract(u.details,'$.turn')=? AND json_extract(u.details,'$.step')=?",
                                (host_id, session_id, facts.get("turn"), facts.get("step")),
                            )
                        )
                    if len(observations) == 1 and observations[0]["amount"] is None:
                        usage = facts["usage"]
                        amount = usage.get("totalTokens")
                        if amount is None:
                            values = [
                                usage[k]
                                for k in (
                                    "inputTokens",
                                    "outputTokens",
                                    "cacheReadTokens",
                                    "cacheWriteTokens",
                                )
                                if isinstance(usage.get(k), (float, int))
                            ]
                            amount = sum(values) if values else None
                        if amount is not None:
                            store.finish_usage(
                                observations[0]["source_key"],
                                amount,
                                "actual",
                                {"phase": "reconciled", "native_sequence": event["sequence"]},
                                f"reconcile:{session_id}:{event['sequence']}",
                            )
                for event in request.get("events", []):
                    value.append(
                        store.record_host_event(
                            host_id,
                            session_id,
                            event["event_type"],
                            event["sequence"],
                            event.get("facts", {}),
                            f"{session_id}:event:{event['sequence']}",
                        )
                    )
                with store._connection() as db:
                    db.execute(
                        "INSERT INTO projection_cursors VALUES(?,?) ON CONFLICT(session_id) DO UPDATE SET sequence=MAX(sequence,excluded.sequence)",
                        (session_id, request.get("cursor", 0)),
                    )
            elif method == "recovery_receipt":
                with store._read() as db:
                    row = db.execute(
                        "SELECT details FROM workflow_intents WHERE intent_id=? AND session_id=? AND kind='recovery-receipt' AND state='complete'",
                        (request["key"], session_id),
                    ).fetchone()
                    value = json.loads(row["details"]) if row else None
            elif method == "task_context":
                from .memory_store import bounded_value

                selected = store.reference_query(request["task_id"], full=True)
                if selected["kind"] != "exploration-task":
                    raise ValueError("Unknown exploration task")
                value = bounded_value(selected["value"]["context"], 6000)
            elif method == "task_creation_clear":
                with store._read() as db:
                    task = db.execute(
                        "SELECT session_id FROM exploration_tasks WHERE task_id=?",
                        (request["task_id"],),
                    ).fetchone()
                    if not task:
                        raise ValueError("Unknown exploration task")
                    sid = task["session_id"]
                    rows = db.execute(
                        "SELECT 1 FROM associations WHERE session_id=? UNION ALL SELECT 1 FROM workflow_sessions WHERE session_id=? UNION ALL SELECT 1 FROM workflow_intents WHERE session_id=? LIMIT 1",
                        (sid, sid, sid),
                    ).fetchone()
                    value = {"clear": rows is None}
            elif method == "workflow_page":
                with store._read() as db:
                    value = store.workflow_control(db, session_id, request.get("cursors"))
            elif method == "status" or method == "control_state":
                value = store.control_state(host_id, session_id)
                value["project_root"] = str(root)
            elif method == "workbench_summary":
                value = workbench_read.summary(store, host_id, session_id)
            elif method == "workbench_page":
                value = workbench_read.page(
                    store, request.get("collection"), after=request.get("after"),
                    upper_id=request.get("upper_id"), limit=request.get("limit", 20),
                    node_id=request.get("node_id"), kind=request.get("kind"),
                    state=request.get("state"), trigger_kind=request.get("trigger_kind"), cursor=request.get("cursor"),
                )
            elif method == "knowledge_page":
                value = workbench_read.knowledge_page(
                    store, query=request.get("query", ""), kind=request.get("kind"),
                    status=request.get("status"), node_id=request.get("node_id"),
                    knowledge_id=request.get("knowledge_id"), history=request.get("history", False),
                    after=request.get("after", 0), upper_id=request.get("upper_id"),
                    limit=request.get("limit", 20),
                )
            elif method == "presentation_get":
                value = workbench_read.presentation(store, root)
            elif method == "reference_entries":
                value = workbench_read.reference_entries(
                    store, root, request.get("ref"), after=request.get("after", 0),
                    limit=request.get("limit", 50),
                )
            elif method == "reference_content":
                value = workbench_read.reference_content(
                    store, root, request.get("ref"), offset=request.get("offset", 0),
                    limit=request.get("limit", 32768),
                )
            elif method == "query":
                if request.get('collection') == 'exploration_budget':
                    budget = store.runtime_budget_status(host_id, session_id)['exploration']
                    items = ([{'kind':'allocation', **item} for item in budget.get('allocations', [])]
                             + [{'kind':'resource_lease', **item} for item in budget.get('resource_leases', [])])
                    digest = hashlib.sha256(json.dumps(budget, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                    cursor = request.get('cursor')
                    if cursor is not None and not isinstance(cursor, dict):
                        raise ValidationError('Exploration budget cursor must be an object')
                    cursor = cursor or {}
                    if cursor and (cursor.get('digest') != digest or cursor.get('project_root') != str(root)):
                        raise ConflictError('read_view_changed: refresh exploration budget')
                    offset = cursor.get('offset', 0)
                    if type(offset) is not int or offset < 0 or offset > len(items):
                        raise ValidationError('Invalid exploration budget cursor offset')
                    limit = max(1, min(int(request.get('limit') or 50), 100))
                    summary = {key:value for key,value in budget.items() if key not in {'allocations','resource_leases'}}
                    end = min(len(items), offset + limit)
                    value = {'summary':summary, 'items':items[offset:end], 'total':len(items),
                        'cursor': {'digest':digest, 'project_root':str(root), 'offset':end} if end < len(items) else None}
                elif request.get('collection') == 'evidence':
                    value = self.evidence.page(store, host=host_id, session=session_id, **{key: request[key] for key in
                        ('refs', 'include_materials', 'draft_id', 'snapshot_id', 'cursor', 'material_ref', 'limit', 'scope_id') if key in request})
                elif request.get('collection') in {'exploration_scopes', 'exploration_routes', 'exploration_decisions', 'exploration_results', 'exploration_intents'}:
                    value = store.exploration_page(request['collection'], **{key: request[key] for key in
                        ('cursor', 'limit', 'ref', 'scope_id') if key in request})
                elif isinstance(request.get('ref'), str) and request['ref'].startswith('explore/scope/'):
                    value = store.exploration_page('exploration_scopes', ref=request['ref'])
                elif isinstance(request.get('ref'), str) and request['ref'].startswith('explore/route/'):
                    value = store.exploration_page('exploration_routes', ref=request['ref'])
                elif isinstance(request.get('ref'), str) and request['ref'].startswith('explore/decision/'):
                    value = store.exploration_page('exploration_decisions', ref=request['ref'])
                elif request.get('collection')=='adoptions':
                    value = store.adoptions(**{key:request[key] for key in ('cursor','limit','node_id') if key in request})
                elif request.get('collection') in {'review_candidates','review_obligations','review_events'}:
                    value = store.review_page(request['collection'], **{key:request[key] for key in
                        ('cursor','limit','node_id','trigger_kind','generation','scope_owner','state','target_ref','change_id','obligation_id') if key in request})
                elif request.get('collection') == 'checkpoint':
                    value = store.checkpoint_read(host_id, session_id)
                elif request.get("ref") and request.get("offset") is not None:
                    value = store.reference_chunk(
                        request["ref"],
                        offset=request.get("offset", 0),
                        limit=request.get("limit", 8192), expected_digest=request.get("expected_digest"),
                    )
                elif request.get("ref"):
                    value = store.reference_query(request["ref"])
                elif request.get("collection") in {"critical_now", "findings", "completed_findings"}:
                    value = store.context_page(request["collection"], **{
                        key: request[key] for key in ("cursor", "limit", "node_id", "kind", "query", "decision_refs", "final_ref") if key in request})
                elif request.get("collection") == "hints":
                    value = store.hints_query(limit=request.get("limit", 8), offset=request.get("offset"),
                                              node_id=request.get("node_id"), klass=request.get("kind"), cursor=request.get("cursor"))
                elif request.get("collection") == "review_todos":
                    value = store.history_page("review_todos", **{
                        key: request[key] for key in ("after", "upper_id", "limit", "node_id", "state", "trigger_kind", "project_id", "order", "cursor") if key in request})
                elif any(
                    request.get(key) is not None
                    for key in ("query", "kind", "node_id", "status", "revision", "conditions")
                ):
                    value = store.knowledge_query(
                        query=request.get("query"),
                        node_id=request.get("node_id"),
                        kind=request.get("kind"),
                        status=request.get("status"),
                        revision=request.get("revision"),
                        conditions=request.get("conditions"),
                        after=request.get("after", 0),
                        limit=request.get("limit", 50),
                    )
                elif request.get("collection"):
                    if request["collection"] == "usage":
                        value = store.usage_page(
                            after=request.get("after", 0),
                            upper_id=request.get("upper_id"),
                            limit=request.get("limit", 50),
                        )
                    elif request["collection"] == "changes":
                        value = store.changes_page(
                            after=request.get("after", 0), limit=request.get("limit", 50)
                        )
                    else:
                        value = store.history_page(request["collection"], **{
                            key: request[key] for key in ("after", "upper_id", "limit", "project_id", "order", "cursor") if key in request})
                else:
                    value = store.control_state(host_id, session_id)
                    value["project_root"] = str(root)
            elif method == "history_page":
                value = store.history_page(request.get("collection"), **{
                    key: request[key] for key in ("offset", "after", "upper_id", "limit", "node_id", "state", "trigger_kind", "kind", "project_id", "order", "cursor") if key in request})
            elif method == "usage_page":
                value = store.usage_page(
                    after=request.get("after", 0),
                    upper_id=request.get("upper_id"),
                    limit=request.get("limit", 50),
                )
            elif method == "changes_page":
                value = store.changes_page(
                    after=request.get("after", 0), limit=request.get("limit", 50)
                )
            elif method in {"memory", "memory_context"}:
                value = store.context_view(
                    host_id, session_id, int(request.get("max_chars", 12000)),
                    profile=request.get("profile"), policy=request.get("policy"),
                    decision_refs=request.get("decision_refs"), final_ref=request.get("final_ref"),
                )
            elif method == "context_policy":
                if not role or role["role"] != "main":
                    raise ValueError("Only the research main session may configure context policy")
                if request.get("policy") is None:
                    with store._read() as db:
                        value = {"policy_version": store._project(db)["config"].get("context_policy", "memory-v2")}
                else:
                    value = store.set_context_policy(request["policy"], self._operation(request))
                with store._read() as db:
                    last = db.execute(
                        "SELECT p.policy_version,r.created_at FROM context_requests r "
                        "JOIN context_packs p USING(pack_id) WHERE r.host_id=? AND r.session_id=? "
                        "ORDER BY r.context_id DESC LIMIT 1", (host_id, session_id),
                    ).fetchone()
                value = {**value, "last_recorded": dict(last) if last else None}
            elif method == "exploration_policy":
                value = store.exploration_policy(host_id, session_id, request.get("mode"), self._operation(request))
            elif method == "conclusion_contract":
                value = store.conclusion_contract(host_id, session_id, request.get('contract'), self._operation(request))
            elif method == "conclusion_prepare":
                fields = request.get('fields', {})
                try:
                    snapshot = self.evidence.get(store, fields.get('snapshot_id'))
                except ConflictError:
                    with store._read() as db:
                        previous = db.execute('SELECT 1 FROM requests WHERE request_id=?', (self._operation(request),)).fetchone()
                    if not previous:
                        raise
                    snapshot = None
                value = store.prepare_conclusion(host_id, session_id, fields, snapshot, self._operation(request))
            elif method in {"exploration_scope", "exploration_route"}:
                value = getattr(store, method)(host_id, session_id, request.get("fields", {}), self._operation(request))
            elif method == "exploration_decide":
                fields = request.get("fields", {})
                try:
                    snapshot = self.evidence.get(store, fields.get("snapshot_id"))
                except ConflictError:
                    with store._read() as db:
                        previous = db.execute("SELECT 1 FROM requests WHERE request_id=?", (self._operation(request),)).fetchone()
                    if not previous:
                        raise
                    snapshot = None
                value = store.exploration_decide(host_id, session_id, fields, snapshot, self._operation(request))
            elif method == "exploration_pending":
                value = store.exploration_pending(host_id, session_id)
            elif method == "exploration_materialize":
                value = store.exploration_materialize(host_id, session_id, request.get("intent_id"), self._operation(request))
            elif method == "exploration_task_check":
                value = store.exploration_task_check(host_id, session_id, request.get("task_id"), self._operation(request))
            elif method == "evidence_observed":
                pages = request.get('pages')
                if not isinstance(pages, list):
                    raise ValidationError('Evidence pages must be a list')
                validated = []
                rejected = []
                with store._read() as db:
                    store._association(db, host_id, session_id)
                    generation = db.execute('SELECT generation FROM workflow_project WHERE id=1').fetchone()[0]
                    main = db.execute('SELECT 1 FROM workflow_project p JOIN workflow_sessions w ON w.session_id=p.main_session_id '
                                      "WHERE p.id=1 AND w.host_id=? AND w.session_id=? AND w.role='main' AND w.detached=0",
                                      (host_id, session_id)).fetchone()
                for index, page in enumerate(pages):
                    if not isinstance(page, dict):
                        rejected.append({'index': index, 'reason': 'page_mismatch'})
                        continue
                    if page.get('part') == 'material':
                        if not main:
                            rejected.append({'index': index, 'reason': 'wrong_session'})
                            continue
                        try:
                            material = self.evidence.materials.describe(store, page.get('material_ref'))
                            start, end = page.get('offset'), page.get('end')
                            if (type(start) is not int or type(end) is not int or not 0 <= start < end <= material['total_bytes']
                                    or page.get('snapshot_id') != material['snapshot_id']
                                    or not isinstance(page.get('model_message_id'), str) or not page['model_message_id']):
                                rejected.append({'index': index, 'reason': 'page_mismatch'})
                                continue
                            if (page.get('manifest_digest') != material['digest'] or page.get('chunk_digest') !=
                                    hashlib.sha256(self.evidence.materials.bytes(material, start, end)).hexdigest()):
                                rejected.append({'index': index, 'reason': 'digest_mismatch'})
                                continue
                            validated.append({'snapshot_id': material['snapshot_id'], 'draft_id': None, 'part': 'material',
                                              'model_message_id': page['model_message_id'], 'generation': generation,
                                              'offset': start, 'end': end, 'digest': material['digest']})
                        except (ConflictError, ValidationError, OSError):
                            rejected.append({'index': index, 'reason': 'material_unavailable'})
                        continue
                    try:
                        snapshot = self.evidence.get(store, page.get('snapshot_id'))
                    except ValidationError:
                        rejected.append({'index': index, 'reason': 'wrong_project'})
                        continue
                    except ConflictError:
                        if not page.get('draft_id'):
                            rejected.append({'index': index, 'reason': 'evidence_snapshot_expired'})
                            continue
                        try:
                            snapshot = self.evidence.draft_snapshot(store, page['draft_id'], page.get('snapshot_id'))
                        except (ConflictError, ValidationError):
                            rejected.append({'index': index, 'reason': 'draft_unavailable'})
                            continue
                    start, end = page.get('offset'), page.get('end')
                    if snapshot['generation'] != generation:
                        rejected.append({'index': index, 'reason': 'wrong_generation'})
                        continue
                    if snapshot.get('host_id') != host_id or snapshot.get('session_id') != session_id:
                        rejected.append({'index': index, 'reason': 'wrong_session'})
                        continue
                    part = 'decision' if snapshot.get('draft_id') and snapshot.get('format_version') == 2 else 'manifest'
                    body = snapshot['body'].encode('utf-8') if part == 'decision' else snapshot['body']
                    if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(body)
                            or page.get('part', part) != part
                            or page.get('draft_id', snapshot.get('draft_id')) != snapshot.get('draft_id')):
                        rejected.append({'index': index, 'reason': 'page_mismatch'})
                        continue
                    if (page.get('manifest_digest') != snapshot['manifest_digest'] or page.get('chunk_digest') != hashlib.sha256(
                            body[start:end] if part == 'decision' else body[start:end].encode('utf-8')).hexdigest()):
                        rejected.append({'index': index, 'reason': 'digest_mismatch'})
                        continue
                    validated.append({'snapshot_id': snapshot['snapshot_id'], 'draft_id': snapshot.get('draft_id'),
                                      'generation': snapshot['generation'], 'offset': start, 'end': end,
                                      'part': part, 'digest': snapshot['manifest_digest']})
                value = store.evidence_observed(host_id, session_id, request.get('plugin_instance_id'), validated, self._operation(request))
                value['rejected'] = rejected
            elif method == "context_shadow":
                value = store.context_shadow(host_id, session_id,
                    max_chars=request.get("max_chars", 12000), profile=request.get("profile"),
                    decision_refs=request.get("decision_refs"), final_ref=request.get("final_ref"))
            elif method == "conclude":
                value = store.conclude(host_id, session_id, request.get("fields", {}), self._operation(request),
                                       enforce_control=bool(request.get('model_call')),
                                       delivery_receipts=request.get('delivery_receipts'),
                                       plugin_instance_id=request.get('plugin_instance_id'))
            elif method == "context_record":
                value = store.record_context_request(
                    {**request.get("fields", {}), "host_id": host_id, "session_id": session_id,},
                    self._operation(request),
                )
            elif method == 'checkpoint_observed':
                value = store.checkpoint_observed(host_id, session_id, request.get('snapshot'),
                    request.get('plugin_instance_id'), request.get('origin'), self._operation(request))
            elif method in {'review_scope_set','review_promote','review_exception'}:
                if not role or role['role'] != 'main':
                    raise ValidationError('Review control requires the project main')
                if method == 'review_scope_set':
                    value = store.review_scope_set(host_id, session_id, request['refs'], request['reason'], self._operation(request))
                else:
                    value = getattr(store, method)(request['fields'], self._operation(request), {'host_id':host_id,'session_id':session_id})
            elif method == 'runtime_budget_set':
                if not role or role['role'] != 'main':
                    raise ValidationError('Runtime budget control requires the project main')
                value = store.runtime_budget_set(host_id,session_id,request['limits'],request['reason'],self._operation(request))
            elif method == 'runtime_budget_status':
                value = store.runtime_budget_status(host_id,session_id)
            elif method == 'runtime_unverified':
                value = store.runtime_unverified(host_id, session_id)
            elif method == 'controller_attach':
                value = store.controller_attach(host_id, session_id, request.get('epoch'), request.get('binding'), request.get('pid'), self._operation(request))
            elif method == 'controller_ready':
                value = store.controller_ready(host_id, session_id, self._operation(request))
            elif method == 'controller_check':
                value = store.controller_check(host_id, session_id)
            elif method == 'controller_adopt':
                value = store.controller_adopt(host_id, session_id, request.get('reason'), self._operation(request))
            elif method == 'recovery_confirm_exit':
                value = store.recovery_confirm_exit(host_id, session_id, request['task_id'], request.get('reason'), self._operation(request))
            elif method == 'runtime_resources_release':
                value = store.runtime_resources_release(host_id, session_id, request['task_id'], request.get('reason'), self._operation(request))
            elif method == 'recovery_retry_task':
                value = store.recovery_retry_task(host_id, session_id, request['task_id'], request.get('reason'), self._operation(request))
            elif method == 'recovery_confirm_specialist':
                value = store.recovery_confirm_specialist(host_id, session_id, request['task_id'], request.get('reason'), self._operation(request))
            elif method == 'recovery_resolve_delivery':
                value = store.recovery_resolve_delivery(host_id, session_id, request['notification_id'], request['kind'], request['disposition'], request.get('reason'), self._operation(request))
            elif method == 'runtime_source_owner':
                value = store.runtime_source_owner(host_id,session_id)
            elif method == 'runtime_deadline':
                value = store.runtime_deadline()
            elif method == 'runtime_shell_begin':
                value = store.runtime_shell_begin(host_id, session_id, request['call_id'], self._operation(request), request.get('execution_token'))
            elif method == 'runtime_shell_result':
                value = store.runtime_shell_result(host_id, session_id, request['call_id'], request['state'], request.get('shell_id'), self._operation(request))
            elif method == 'runtime_shell_notice':
                value = store.runtime_shell_notice(host_id, session_id, request['shell_id'], request['message_id'],
                                                   request['outcome'], request.get('exit_code'), self._operation(request))
            elif method == 'runtime_shell_status':
                value = store.runtime_shell_status(host_id, session_id)
            elif method == 'runtime_shell_unverified':
                value = store.runtime_shell_unverified(host_id, session_id, request['call_id'], self._operation(request))
            elif method == 'runtime_resume_session':
                value = store.runtime_resume_session(host_id, session_id, request['target_session'],
                                                     request['expected_revision'], self._operation(request))
            elif method == 'runtime_reserve':
                value = store.runtime_reserve(host_id,session_id,self._operation(request),request.get('source_snapshot'),
                                              request.get('wake_intent_id'), request.get('launch_intent_id'), request.get('specialist_launch_id'), request.get('message_id'))
            elif method == 'runtime_execution_start':
                value = store.runtime_execution_start(host_id, session_id, request['execution_token'], self._operation(request), request.get('event_seq'), request.get('ambiguous', False))
            elif method == 'runtime_execution_bind':
                value = store.runtime_execution_bind(host_id, session_id, request['execution_token'], request['message_id'], self._operation(request), request.get('event_seq'), request.get('transcript_witness', False))
            elif method == 'runtime_drain_check':
                value = store.runtime_drain_check(host_id, session_id, request.get('execution_token'))
            elif method == 'runtime_execution_view':
                value = store.runtime_execution_view(host_id, session_id, request.get('execution_token'))
            elif method == 'runtime_outbound_not_sent':
                value = store.runtime_outbound_not_sent(host_id, session_id, request['intent_id'], request['stage'], request['reason'], self._operation(request))
            elif method == 'runtime_turn_not_sent':
                value = store.runtime_turn_not_sent(host_id, session_id, request['message_id'], self._operation(request))
            elif method == 'runtime_budget_stop':
                value = store.runtime_budget_stop(host_id,session_id,self._operation(request))
            elif method == 'runtime_turn_finished':
                value = store.runtime_turn_finished(host_id,session_id,request['turn'],self._operation(request),request.get('source_snapshot'))
            elif method == 'runtime_execution_end':
                value = store.runtime_execution_end(host_id,session_id,request['turn'],request['outcome'],self._operation(request),request.get('execution_token'),request.get('event_seq'))
                value = {**value,'claimed':value.get('claimed',True) and not store.last_replayed}
            elif method == 'node_finish_request':
                value = store.node_finish_request(host_id, session_id, request['state'], request['summary'], request.get('turn_token'), self._operation(request))
            elif method == 'node_launch_claim':
                value = store.node_launch_claim(host_id, session_id, request['task_id'], request['concurrency'], self._operation(request))
            elif method == 'node_launch_get':
                value = store.node_launch_get(host_id, session_id, request['task_id'])
            elif method == 'specialist_launch_get':
                value = store.specialist_launch_get(host_id, session_id, request['task_id'])
            elif method == 'specialist_launch_phase':
                value = store.specialist_launch_phase(host_id, session_id, request['intent_id'], request['phase'], self._operation(request))
            elif method == 'specialist_cancel_unstarted':
                value = store.specialist_cancel_unstarted(host_id, session_id, request['task_id'], self._operation(request))
            elif method == 'node_launch_phase':
                value = store.node_launch_phase(host_id, session_id, request['intent_id'], request['phase'], self._operation(request))
            elif method == 'node_launch_submitted':
                value = store.node_launch_submitted(host_id, session_id, request['intent_id'], request['message_id'], request['cwd'], self._operation(request))
            elif method == 'notification_begin':
                value = store.notification_begin(host_id, session_id, request['notification_id'], self._operation(request), request.get('text'))
            elif method == 'notification_confirm':
                value = store.notification_confirm(host_id, session_id, request['intent_id'], request['message_id'], self._operation(request))
            elif method == 'notification_wake_begin':
                value = store.notification_wake_begin(host_id, session_id, request['notification_id'], self._operation(request))
            elif method == 'notification_wake_confirm':
                value = store.notification_wake_confirm(host_id, session_id, request['intent_id'], request['message_id'], self._operation(request))
            elif method == 'node_finish_pending':
                value = store.node_finish_pending(host_id, session_id)
            elif method == 'node_finish_commit':
                value = store.node_finish_commit(host_id, session_id, request['intent_id'], request['turn_token'], request['terminal'], self._operation(request))
            elif method == 'runtime_submitted':
                value = store.runtime_submitted(host_id,session_id,request['reservation_id'],self._operation(request))
            elif method == 'runtime_native_event':
                value = store.runtime_native_event(host_id,session_id,request['fields'],self._operation(request))
            elif method == 'runtime_tool_observed':
                value = store.runtime_tool_observed(host_id,session_id,request['tool'],request['call_id'],self._operation(request))
            elif method == 'invocation_context':
                value = store.invocation_context(host_id,session_id)
            elif method == "memory_write":
                action = request.get("action")
                contract = request.get('contract_version','legacy')
                if contract not in {'legacy','r2-v1'} or '_contract_version' in request.get('fields',{}):
                    raise ValidationError('Unsupported memory write contract')
                fields = {
                    **request.get("fields", {}),
                    "source_identity": request.get("fields", {}).get("source_identity", {
                        "host_id": host_id, "session_id": session_id,
                    }),
                }
                store._reject_asserted_at(fields)
                if contract == 'r2-v1':
                    fields['_contract_version'] = contract
                if action == "record":
                    value = store.record_knowledge(
                        fields, self._operation(request), execution_identity=(host_id, session_id),
                        asserted_at=self._asserted_at(store, request, host_id, session_id)
                    )
                elif action == "revise":
                    value = store.revise_knowledge(fields, self._operation(request),
                        asserted_at=self._asserted_at(store, request, host_id, session_id))
                elif action == "checkpoint":
                    value = store.checkpoint(fields, self._operation(request), execution_identity=(host_id, session_id),
                        receipt=request.get('checkpoint_base'), plugin_instance_id=request.get('plugin_instance_id'))
                elif action == "dispose":
                    if 'review_event' in fields:
                        value = store.dispose_review_challenge(fields,self._operation(request),
                            {'host_id':host_id,'session_id':session_id})
                    else:
                        value = store.dispose_impact(fields, self._operation(request))
                elif action == "narrow_scope":
                    value = store.narrow_impact_scope(fields, self._operation(request))
                else:
                    raise ValueError(
                        "memory_write action must be record, revise, checkpoint,"
                        " dispose, or narrow_scope"
                    )
            elif method == "specialist_create":
                if not role or role["role"] not in {"main", "node_core", "exploration"}:
                    raise ValueError("Only managed research agents may delegate specialists")
                with store._connection() as db:
                    association = store._association(db, host_id, session_id)
                    attempt = db.execute(
                        "SELECT * FROM attempts WHERE association_id=? AND ended_at IS NULL",
                        (association["association_id"],),
                    ).fetchone()
                fields = dict(request.get("fields", {}))
                node_id = role["node_id"]
                if role["role"] == "main":
                    if fields.get("purpose") != "review" or fields.get("node_id") is not None:
                        raise ValueError("Main coordinates nodes; use research_dispatch. Only project consolidation reviewers may be delegated by main.")
                elif not node_id or not attempt or attempt["node_id"] != node_id:
                    raise ValueError("A specialist requires a live node_core attempt")
                if fields.get("node_id") not in {None, node_id}:
                    raise ValueError("Specialist node_id must match the caller's node")
                fields["node_id"] = node_id
                if fields.get("context_mode", "research") == "blind":
                    if not fields.get("inputs"):
                        raise ValueError("Blind review requires frozen file publication-item inputs")
                    for ref in fields["inputs"]:
                        if not frozen_refs.is_frozen(ref):
                            raise ValueError("Blind inputs must be frozen file references")
                        with store._read() as db:
                            resolution = frozen_refs.require(db, root, ref)
                        obj = resolution["object"]
                        path = root / ".research" / "objects" / obj["version"] if obj else None
                        if path and resolution.get("subpath"):
                            path = path / resolution["subpath"]
                        if path is None or not path.is_file():
                            raise ValueError(f"{ref}: not_an_object")
                value = store.specialist_create(
                    {
                        **fields,
                        "parent_session_id": session_id,
                        "node_id": fields.get("node_id")
                        or (attempt["node_id"] if attempt else role["node_id"]),
                        "attempt_id": attempt["attempt_id"] if attempt else None,
                    },
                    self._operation(request),
                    control_actor=(host_id, session_id) if request.get('model_call') else None,
                )
            elif method == "specialist_result_import":
                task = store.specialist_get(request.get("task_id"))
                if task["parent_session_id"] != session_id:
                    raise ValueError("Only the parent may import a specialist result")
                directory = root / ".research" / "specialist-results"
                source = directory / (task["task_id"] + ".json")
                value = ArtifactStore(root).freeze(source, directory)
            elif method == "specialist_read_input":
                if not specialist_row:
                    raise ValueError("Only an assigned specialist may read inputs")
                refs = json.loads(specialist_row["inputs"])
                input_id = request.get("input_id", "")
                allowed = {f"input-{i + 1}": ref for i, ref in enumerate(refs)}
                if input_id not in allowed:
                    raise ValueError("Input is not assigned to this specialist")
                with store._read() as db:
                    resolution, source = frozen_refs.open(db, root, allowed[input_id])
                if resolution["outcome"] != "resolved":
                    raise ValueError(resolution["message"])
                if not source.is_file():
                    raise ValueError(f"{allowed[input_id]}: not_an_object")
                offset, limit = max(0, int(request.get("offset", 0))), max(1, min(8192, int(request.get("limit", 8192))))
                content = source.read_text(encoding="utf-8")
                end = min(len(content), offset + limit)
                value = {"input_id": input_id, "text": content[offset:end], "next_offset": end if end < len(content) else None}
            elif method == "specialist_finish":
                value = store.specialist_finish(
                    {**request.get("fields", {}), "parent_session_id": session_id,},
                    self._operation(request),
                )
            elif method == "specialist_get":
                value = store.specialist_get(request.get("task_id"))
            elif method == "impact_next":
                value = store.impact_next(request.get("node_id"))
            elif method == "impact_review_state":
                value = store.impact_review_state(
                    request.get("change_id"),
                    request.get("affected_version"),
                    request.get("state"),
                    self._operation(request),
                )
            elif method == "knowledge_impacts":
                value = store.knowledge_impacts(
                    version=request.get("version"),
                    change_id=request.get("change_id"),
                    limit=int(request.get("limit") or 50),
                    offset=int(request.get("offset") or 0),
                    bound=request.get("bound"),
                )
            elif method == "knowledge_risk":
                value = store.knowledge_risk(
                    request.get("versions") or [], request.get("bound")
                )
            elif method == "publication_check":
                value = (
                    store.stored_publication_check(request["publication_id"])
                    if request.get("stored")
                    else store.publication_check(
                        request["publication_id"], request.get("bound")
                    )
                )
            elif method == "review_todo_next":
                value = store.review_todo_next(request.get("node_id"))
            elif method == "review_todo_state":
                value = store.review_todo_state(
                    request.get("todo_id"), request.get("state"), self._operation(request)
                )
            elif method == "guidance_register":
                path_value = request.get("path")
                if not isinstance(path_value, str) or not Path(path_value).is_absolute():
                    raise ValueError("Guidance requires an absolute host-supplied path")
                path = Path(path_value).expanduser().resolve()
                if not path.is_file():
                    raise ValueError("Guidance document does not exist")
                content = path.read_text(encoding="utf-8")
                if len(content.encode("utf-8")) > 2 * 1024 * 1024:
                    raise ValueError("Guidance document exceeds 2 MiB")
                value = store.guidance_register(
                    {
                        "path": str(path),
                        "version": request.get("version")
                        or hashlib.sha256(content.encode()).hexdigest()[:12],
                        "content": content,
                    },
                    self._operation(request),
                )
            elif method == "guidance_status":
                value = store.guidance_status()
            elif method == "propose":
                value = store.propose(
                    request.get("question"),
                    request.get("why_now"),
                    request.get("plan"),
                    self._operation(request),
                    request.get("inputs"),
                    request.get("purpose", "explore"),
                    request.get("strategy", "continue"),
                    request.get("anchor_ref"),
                    request.get("question_ref"),
                    request.get("root_reason"),
                    request.get("predecessors"),
                    enforce_protocol=bool(request.get("model_call")),
                    asserted_at=self._asserted_at(store, request, host_id, session_id),
                    control_actor=(host_id, session_id) if request.get('model_call') else None,
                )
            elif method == "consume":
                value = store.consume(
                    host_id,
                    session_id,
                    request.get("source_ref"),
                    request.get("use"),
                    request.get("relation_type", "adopts"),
                    self._operation(request),
                )
            elif method == "focus":
                value = store.focus(
                    host_id,
                    session_id,
                    request.get("node_id"),
                    request.get("role", "researcher"),
                    request.get("mode", "manual"),
                    self._operation(request),
                    defer=bool(request.get("defer", False)),
                )
            elif method == "bind_turn":
                value = store.bind_turn(
                    host_id, session_id, request.get("turn"), self._operation(request)
                )
            elif method == "note":
                value = store.note(
                    host_id,
                    session_id,
                    request.get("body"),
                    request.get("kind", "progress"),
                    self._operation(request),
                )
            elif method == "publish":
                operation_id = self._operation(request)
                items = self._publish_items(
                    root,
                    operation_id,
                    request.get("items", []),
                    Path(role["cwd"]) if role and role["cwd"] else root,
                )
                value = store.publish_metadata(
                    host_id,
                    session_id,
                    request.get("status", "partial"),
                    request.get("summary"),
                    request.get("gaps", []),
                    items,
                    operation_id,
                    request.get("knowledge_refs", []),
                    asserted_at=self._asserted_at(store, request, host_id, session_id),
                    display=request.get("display"),
                    draft_id=request.get('draft_id'), report_item_id=request.get('report_item_id'),
                )
            elif method == "relate":
                if request.get('contract_version') == 'r2-v1':
                    fields = {key:request[key] for key in ('source_ref','target_ref','label','note','input_refs','revision',
                        'obligation_id','change_id','result','complete','evidence_refs','contract_version',
                        'review_type','specialist_task_id','limitations') if key in request}
                    value = store.relate_typed(fields,self._operation(request),self._asserted_at(store,request,host_id,session_id))
                else:
                    value = store.relate(request.get('source_ref'),request.get('target_ref'),request.get('label'),
                        request.get('note'),self._operation(request),asserted_at=self._asserted_at(store,request,host_id,session_id))
            elif method == "finish":
                value = store.finish(
                    host_id,
                    session_id,
                    request.get("state", "finished"),
                    request.get("details", {}),
                    self._operation(request),
                )
            elif method == "close_node":
                value = store.close_node(request.get("node_id"), self._operation(request))
            elif method == "control":
                value = store.set_control(request.get("control"), self._operation(request))
            elif method == "host_event":
                value = store.record_host_event(
                    host_id,
                    session_id,
                    request.get("event_type"),
                    request.get("sequence"),
                    request.get("facts", {}),
                    self._operation(request),
                )
            elif method == "usage_begin":
                value = store.begin_usage(
                    request.get("source_key"),
                    self._operation(request),
                    host_id=host_id,
                    session_id=session_id,
                    purpose=request.get("purpose", "conversation"),
                    provider=request.get("provider"),
                    model=request.get("model"),
                    turn=request.get("turn"),
                    step=request.get("step"),
                )
            elif method == "usage_finish":
                value = store.finish_usage(
                    request.get("source_key"),
                    request.get("amount"),
                    request.get("completeness", "unknown"),
                    request.get("details", {}),
                    self._operation(request),
                )
            elif method == "usage_gap":
                value = store.record_usage_gap(
                    request.get("source_key"),
                    session_id,
                    request.get("purpose", "conversation"),
                    request.get("reason"),
                    request.get("details", {}),
                    self._operation(request),
                )
            elif method == "own_goal":
                value = store.own_goal(
                    host_id,
                    session_id,
                    request.get("goal_id"),
                    request.get("revision"),
                    request.get("phase"),
                    self._operation(request),
                    activation=request.get("activation"),
                    change_reason=request.get("change_reason"),
                    source_sequence=request.get("source_sequence"),
                )
            elif method == "snapshot":
                operation_id = self._operation(request)
                manifest, complete = self._snapshot_manifest(
                    root,
                    operation_id,
                    request.get("paths", []),
                    Path(role["cwd"]) if role and role["cwd"] else root,
                )
                value = store.record_snapshot(host_id, session_id, manifest, complete, operation_id)
            elif method == "prepare_branch":
                value = self._prepare_branch(
                    store, root, request.get("node_id"), self._operation(request)
                )
            elif method == "validate_branch_inputs":
                value = self._validate_branch_inputs(store, root, request.get("node_id"))
            elif method == "prepare_restore":
                operation_id = self._operation(request)
                preview = self.restore_preview(store, root, request["snapshot_id"], session_id)
                if request.get("preview_id") != preview["preview_id"]:
                    raise ValueError("A current restore preview_id is required")
                intent_path, saved = self._intent(root, operation_id, "restore")
                if saved is not None:
                    value = saved
                else:
                    snapshot_id = request.get("snapshot_id")
                    selected_snapshot = store.reference_query(snapshot_id, full=True)
                    if selected_snapshot["kind"] != "snapshot":
                        raise ValueError(f"Unknown snapshot: {snapshot_id}")
                    snapshot = selected_snapshot["value"]
                    source_attempt = store.reference_query(snapshot["attempt_id"], full=True)[
                        "value"
                    ]
                    if source_attempt["state"] == "unknown" or source_attempt["ended_at"] is None:
                        raise ValueError(
                            "The source work segment is still active or unverified; "
                            "finish or stop it before takeover"
                        )
                    digest = hashlib.sha256(operation_id.encode()).hexdigest()[:12]
                    workspace = Path(preview["workspace"])
                    workspace.mkdir(parents=True, exist_ok=True)
                    artifacts = ArtifactStore(root)
                    restored = []
                    for index, item in enumerate(snapshot["manifest"]):
                        if item.get("status") != "fixed":
                            continue
                        source_name = self._safe_source(item["source_path"])
                        destination = workspace / source_name
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        artifacts.materialize(
                            {key: item[key] for key in ("path", "version", "kind")},
                            destination,
                            readonly=False,
                        )
                        restored.append(str(destination.relative_to(root)))
                    value = {
                        "snapshot_id": snapshot_id,
                        "source_attempt_id": snapshot["attempt_id"],
                        "node_id": source_attempt["node_id"],
                        "workspace": str(workspace),
                        "project_root": str(root),
                        "restored": restored,
                        "complete": snapshot["complete"],
                    }
                    value["context"] = preview["context"]
                    self._write_intent(workspace / "RESEARCH_HANDOFF.json", preview["context"])
                    self._write_intent(intent_path, value)
            elif method == "record_restore":
                value = store.record_restore(
                    host_id,
                    session_id,
                    request.get("snapshot_id"),
                    request.get("source_attempt_id"),
                    request.get("workspace"),
                    self._operation(request),
                )
            elif method == "detach":
                value = store.detach(
                    host_id, session_id, self._operation(request), request.get("ended_seq")
                )
                self.registry.detach(host_id, session_id)
            elif method == "inventory":
                value = preflight(root)
            elif method == "recovery_preview":
                value = recovery_preview(root, request.get("attempt_id"))
            elif method == "preview":
                path = request.get("path")
                if not isinstance(path, str) or Path(path).is_absolute():
                    raise ValueError("Preview requires a project-relative research path")
                if path.split("/")[0] not in {"workspaces", ".research"}:
                    raise ValueError("Only registered project research paths are previewable")
                with _open_source(path, root) as fd:
                    content = os.read(fd, MAX_PREVIEW + 1)
                if b"\0" in content:
                    raise ValueError("Binary file; use the inventory digest")
                value = {
                    "path": path,
                    "text": content[:MAX_PREVIEW].decode("utf-8", errors="replace"),
                    "truncated": len(content) > MAX_PREVIEW,
                    "limit_bytes": MAX_PREVIEW,
                }
            else:
                raise ValueError(f"Unknown native research method: {method}")
        return {"request_id": transport_id, "ok": True, "value": redact(value),
                'replayed': store.last_replayed if store else None,
                **({'invocation_context':invocation} if invocation else {})}


def serve(service, source, output):
    while True:
        raw = source.readline(MAX_REQUEST + 1)
        if not raw:
            return
        request_id = None
        try:
            if len(raw) > MAX_REQUEST:
                # Close an invalid stream rather than interpreting the tail as another request.
                raise OverflowError("Request exceeds byte limit; connection closed")
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise ValueError("Request must be an object")
            request_id = request.get("transport_id", request.get("request_id"))
            response = service.handle(request)
            encoded = json.dumps(response, ensure_ascii=False, allow_nan=False).encode()
            if len(encoded) > MAX_RESPONSE:
                raise ValueError("Response exceeds byte limit; use a narrower view")
        except Exception as exc:
            encoded = json.dumps(
                {
                    "request_id": request_id,
                    "ok": False,
                    "error": {"code": getattr(exc, 'code', type(exc).__name__), "message": str(exc),
                              **({'details': exc.details} if hasattr(exc, 'details') else {})},
                }
            ).encode()
        output.write(encoded + b"\n")
        output.flush()
        if len(raw) > MAX_REQUEST:
            return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--registry", help="Host-owned native project registry")
    mode.add_argument("--registrations", help="Legacy read-only registration JSON")
    args = parser.parse_args()
    if args.registry:
        serve(NativeService(args.registry), sys.stdin.buffer, sys.stdout.buffer)
        return
    registrations = json.loads(args.registrations)
    if not isinstance(registrations, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in registrations.items()
    ):
        parser.error("registrations must map strings to paths")
    serve(ReadOnlyService(registrations), sys.stdin.buffer, sys.stdout.buffer)


if __name__ == "__main__":
    main()
