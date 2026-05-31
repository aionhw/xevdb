"""OpenSearch backend — connection, index lifecycle, and bulk write path.

Importing this module requires the optional ``opensearch-py`` package; the
top-level import is what makes the registry surface the
``bash install.sh --with-opensearch`` hint when the dependency is missing.

Phases delivered here:
* Phase 2 — resolve a dataset to a cluster + index set (pointer file or env
  knobs), open a client session, create/drop the index set.
* Phase 3 — ``build`` / ``ingest_rtl`` / ``ingest_sim`` stream documents (built
  by the dependency-free ``opensearch_docs`` module, with Strategy-B
  denormalization) into the cluster via ``helpers.bulk``; ``--reset`` is a
  per-group index wipe.

The query/prompt read path (``stats`` / ``run_prompt``) is Phase 4.
"""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from opensearchpy import OpenSearch, helpers
from opensearchpy.exceptions import NotFoundError

from .base import Backend
from . import opensearch_schema as schema
from . import opensearch_docs as docs
from .. import sv as _sv
from .. import bugs as _bugs
from .. import db as _db
from .. import prompts as _prompts
from .. import cache as _cache

# Environment knobs used when building a pointer for a path that doesn't exist
# yet (i.e. `xevdb --backend opensearch build ...`).
_ENV_HOSTS = "XEVDB_OPENSEARCH_HOSTS"      # comma-separated, e.g. "localhost:9200"
_ENV_DUMP_ID = "XEVDB_OPENSEARCH_DUMP_ID"  # override the slug derived from the path
_ENV_PREFIX = "XEVDB_OPENSEARCH_PREFIX"
_DEFAULT_HOST = "localhost:9200"


def _hosts_from_env() -> list[str]:
    raw = os.environ.get(_ENV_HOSTS, _DEFAULT_HOST)
    return [h.strip() for h in raw.split(",") if h.strip()]


class OpenSearchBackend(Backend):
    """A xevdb dataset stored as a set of OpenSearch indices.

    The ``db_path`` is a JSON *pointer file* (see ``opensearch_schema.Pointer``)
    rather than the bulk data. When it does not exist yet, a pointer is
    synthesized from environment knobs and written on first build.
    """

    name = "opensearch"
    supports_raw_sql = False

    def __init__(self, db_path: str | Path) -> None:
        super().__init__(db_path)
        self._ptr: schema.Pointer | None = (
            schema.read_pointer(db_path) if Path(db_path).is_file() else None
        )

    # -- pointer / client ---------------------------------------------------

    def _pointer(self, *, create: bool = False) -> schema.Pointer:
        if self._ptr is not None:
            return self._ptr
        if not create:
            raise FileNotFoundError(
                f"{self.db_path} is not an opensearch pointer file — create one, "
                f"or run `xevdb --backend opensearch build` to synthesize it "
                f"(cluster from ${_ENV_HOSTS}, default {_DEFAULT_HOST})."
            )
        dump_id = schema.slugify(
            os.environ.get(_ENV_DUMP_ID) or Path(self.db_path).stem
        )
        ptr = schema.Pointer(
            hosts=_hosts_from_env(),
            dump_id=dump_id,
            prefix=os.environ.get(_ENV_PREFIX, schema.DEFAULT_PREFIX),
        )
        schema.write_pointer(self.db_path, ptr)
        self._ptr = ptr
        return ptr

    def _client(self, ptr: schema.Pointer | None = None) -> OpenSearch:
        ptr = ptr or self._pointer()
        return OpenSearch(hosts=ptr.hosts, **ptr.extra)

    @contextmanager
    def open(self, *, read_only: bool = False) -> Iterator[OpenSearch]:
        client = self._client()
        try:
            yield client
        finally:
            client.close()

    # -- index lifecycle ----------------------------------------------------

    def _ensure_indices(self, client: OpenSearch, ptr: schema.Pointer,
                        tables: Iterable[str] | None = None) -> None:
        for table in (tables if tables is not None else schema.TABLES):
            idx = ptr.index(table)
            if not client.indices.exists(index=idx):
                client.indices.create(index=idx, body=schema.mapping_for(table))

    def _reset_indices(self, client: OpenSearch, ptr: schema.Pointer,
                      tables: Iterable[str]) -> None:
        for table in tables:
            idx = ptr.index(table)
            if client.indices.exists(index=idx):
                client.indices.delete(index=idx)
            client.indices.create(index=idx, body=schema.mapping_for(table))

    def _bulk(self, client: OpenSearch, ptr: schema.Pointer,
              actions: Iterable[docs.Action]) -> int:
        """Ship Action tuples to the cluster. Returns the number indexed."""
        def _ops() -> Iterator[dict]:
            for a in actions:
                op = {"_index": ptr.index(a.table), "_source": a.source}
                if a.id is not None:
                    op["_id"] = a.id
                yield op
        n, errors = helpers.bulk(client, _ops(), refresh=True)
        if errors:
            raise RuntimeError(f"opensearch bulk index reported {len(errors)} error(s)")
        return n

    def create_indices(self, *, reset: bool = False,
                       create_pointer: bool = False) -> dict[str, str]:
        """Create the dataset's index set with their mappings (idempotent)."""
        ptr = self._pointer(create=create_pointer)
        client = self._client(ptr)
        try:
            if reset:
                self._reset_indices(client, ptr, schema.TABLES)
            else:
                self._ensure_indices(client, ptr)
        finally:
            client.close()
        return ptr.indices()

    def drop_indices(self) -> list[str]:
        """Delete every index belonging to this dump. Returns those deleted."""
        ptr = self._pointer()
        client = self._client(ptr)
        dropped: list[str] = []
        try:
            for idx in ptr.indices().values():
                if client.indices.exists(index=idx):
                    client.indices.delete(index=idx)
                    dropped.append(idx)
        finally:
            client.close()
        return dropped

    # -- write path (Phase 3) ----------------------------------------------

    def build(self, vcd_path: str | Path, *, reset: bool = False,
              seed: bool = True) -> dict[str, int]:
        from ..parser import parse_file
        from .. import seed_prompts as _seed

        vcd = parse_file(vcd_path)
        ptr = self._pointer(create=True)
        client = self._client(ptr)
        try:
            # reset wipes the whole dump; otherwise re-load only the waveform +
            # meta indices so a re-build doesn't duplicate changes but leaves
            # any ingested RTL / sim intact. Prompt docs are keyed by name.
            self._reset_indices(
                client, ptr, schema.TABLES if reset else schema.VCD_TABLES + ("meta",))
            self._ensure_indices(client, ptr)
            actions = list(docs.vcd_actions(vcd, source=str(vcd_path)))
            if seed:
                actions += list(docs.prompt_actions(_seed.PROMPTS, now=time.time()))
            self._bulk(client, ptr, actions)
        finally:
            client.close()

        t_min = min((c.t for c in vcd.changes), default=0)
        t_max = max((c.t for c in vcd.changes), default=0)
        return {"signals": len(vcd.signals), "changes": len(vcd.changes),
                "t_min": t_min, "t_max": t_max}

    def ingest_rtl(self, rtl_path: str | Path, *,
                   reset: bool = False) -> dict[str, int]:
        ptr = self._pointer()
        client = self._client(ptr)
        files = list(_sv.walk_rtl(rtl_path))
        try:
            if reset:
                self._reset_indices(client, ptr, schema.RTL_TABLES)
            self._ensure_indices(client, ptr, schema.RTL_TABLES)
            self._bulk(client, ptr, docs.rtl_actions(files, now=time.time()))
        finally:
            client.close()

        n_modules = sum(len(mods) for _, mods, _ in files)
        n_ports = sum(len(m.ports) for _, mods, _ in files for m in mods)
        n_signals = sum(len(m.signals) for _, mods, _ in files for m in mods)
        n_instances = sum(len(m.instances) for _, mods, _ in files for m in mods)
        return {"files": len(files), "modules": n_modules, "ports": n_ports,
                "signals": n_signals, "instances": n_instances}

    def ingest_sim(self, log_path: str | Path, *, name: str | None = None,
                   keep_all: bool = False, reset: bool = False) -> dict[str, int]:
        from .. import sim as _sim

        events = _sim.parse_file(log_path, keep_all=keep_all)
        counts = _sim.severity_counts(events)
        raw_lines = Path(log_path).read_text(
            encoding="utf-8", errors="replace").splitlines()
        run_name = name or Path(log_path).name
        now = time.time()

        ptr = self._pointer()
        client = self._client(ptr)
        try:
            if reset:
                self._reset_indices(client, ptr, schema.SIM_TABLES)
            self._ensure_indices(client, ptr, schema.SIM_TABLES)
            self._bulk(client, ptr, docs.sim_actions(
                events, run_name=run_name, source=str(log_path), now=now,
                line_count=len(raw_lines), counts=counts))
        finally:
            client.close()

        return {
            "name": run_name, "line_count": len(raw_lines), "events": len(events),
            "fatal": sum(counts.get(s, 0) for s in ("FATAL", "UVM_FATAL")),
            "error": sum(counts.get(s, 0) for s in ("ERROR", "UVM_ERROR", "ASSERTION")),
            "warning": sum(counts.get(s, 0) for s in ("WARNING", "UVM_WARNING")),
        }

    # -- read path: stats (Phase 4) ----------------------------------------

    def stats(self, session: OpenSearch) -> dict[str, Any]:
        ptr = self._pointer()
        self._ensure_indices(session, ptr)
        out: dict[str, Any] = {}
        meta = session.search(index=ptr.index("meta"),
                              body={"size": 1000, "query": {"match_all": {}}})
        for h in meta["hits"]["hits"]:
            out[h["_source"]["key"]] = h["_source"]["value"]
        out["row_counts"] = {
            t: session.count(index=ptr.index(t))["count"] for t in schema.TABLES
        }
        return out

    # -- read path: waveform queries (Phase 4) -----------------------------

    def _sig_hits(self, session: OpenSearch, idx: str, body: dict) -> list[dict]:
        return [h["_source"] for h in session.search(index=idx, body=body)["hits"]["hits"]]

    @staticmethod
    def _to_resolved(doc: dict) -> "_db.ResolvedSignal":
        return _db.ResolvedSignal(
            sig_id=doc["id"], hier=doc["hier"], name=doc["name"],
            fullname=doc["fullname"], width=doc["width"], kind=doc["kind"])

    def resolve_signal(self, session: OpenSearch, query: str) -> Any | None:
        ptr = self._pointer()
        idx = ptr.index("signals")
        for field in ("id", "fullname"):
            hits = self._sig_hits(session, idx,
                                  {"size": 1, "query": {"term": {field: query}}})
            if hits:
                return self._to_resolved(hits[0])
        bare = query.rsplit(".", 1)[-1]
        hits = self._sig_hits(session, idx, {"size": 2, "query": {"bool": {"should": [
            {"term": {"name": bare}}, {"wildcard": {"name": f"{bare}[*"}},
        ], "minimum_should_match": 1}}})
        if len(hits) == 1:
            return self._to_resolved(hits[0])
        hits = self._sig_hits(session, idx,
                              {"size": 2, "query": {"wildcard": {"fullname": f"*.{query}"}}})
        if len(hits) == 1:
            return self._to_resolved(hits[0])
        return None

    def value_at(self, session: OpenSearch, sig_id: str,
                 t: int) -> tuple[int, str] | None:
        ptr = self._pointer()
        body = {"size": 1, "sort": [{"t": "desc"}], "query": {"bool": {"filter": [
            {"term": {"sig_id": sig_id}}, {"range": {"t": {"lte": t}}}]}}}
        hits = session.search(index=ptr.index("changes"), body=body)["hits"]["hits"]
        if not hits:
            return None
        s = hits[0]["_source"]
        return (s["t"], s["value"])

    def window(self, session: OpenSearch, sig_id: str, t0: int | None,
               t1: int | None, limit: int = 200) -> list[tuple[int, str]]:
        ptr = self._pointer()
        filt: list[dict] = [{"term": {"sig_id": sig_id}}]
        if t0 is not None or t1 is not None:
            rng: dict[str, Any] = {}
            rng["gte"] = t0 if t0 is not None else 0
            rng["lte"] = t1 if t1 is not None else 2**62
            filt.append({"range": {"t": rng}})
        body = {"size": limit, "sort": [{"t": "asc"}],
                "query": {"bool": {"filter": filt}}}
        hits = session.search(index=ptr.index("changes"), body=body)["hits"]["hits"]
        return [(h["_source"]["t"], h["_source"]["value"]) for h in hits]

    def find_signals(self, session: OpenSearch, pattern: str,
                     limit: int = 50) -> list[Any]:
        ptr = self._pointer()
        if any(ch in pattern for ch in "*?[]"):
            pat = pattern.replace("[", "").replace("]", "")
        else:
            pat = f"*{pattern}*"
        body = {"size": limit, "sort": [{"fullname": "asc"}], "query": {"bool": {
            "should": [{"wildcard": {"fullname": pat}}, {"wildcard": {"name": pat}}],
            "minimum_should_match": 1}}}
        return [self._to_resolved(h["_source"])
                for h in session.search(index=ptr.index("signals"), body=body)["hits"]["hits"]]

    # -- prompt library CRUD (Phase 5) -------------------------------------

    @staticmethod
    def _doc_to_prompt(doc: dict) -> "_prompts.Prompt":
        return _prompts.Prompt(
            name=doc["name"], description=doc.get("description", ""),
            sql=doc.get("sql", ""), params=json.loads(doc.get("params_json", "[]")),
            created_at=doc.get("created_at", 0.0), updated_at=doc.get("updated_at", 0.0),
            dsl_json=doc.get("dsl_json", ""))

    def list_prompts(self, session: OpenSearch) -> list[Any]:
        ptr = self._pointer()
        self._ensure_indices(session, ptr, ["prompts"])
        body = {"size": 1000, "query": {"match_all": {}}, "sort": [{"name": "asc"}]}
        hits = session.search(index=ptr.index("prompts"), body=body)["hits"]["hits"]
        return [self._doc_to_prompt(h["_source"]) for h in hits]

    def show_prompt(self, session: OpenSearch, name: str) -> Any:
        ptr = self._pointer()
        doc = self._get_doc(session, ptr.index("prompts"), name)
        if doc is None:
            raise KeyError(f"no prompt named {name!r}")
        return self._doc_to_prompt(doc)

    def add_prompt(self, session: OpenSearch, name: str, sql: str, *,
                   description: str = "", params: list[dict] | None = None,
                   overwrite: bool = False, dsl_json: str = "") -> None:
        ptr = self._pointer()
        idx = ptr.index("prompts")
        self._ensure_indices(session, ptr, ["prompts"])
        existing = self._get_doc(session, idx, name)
        if existing and not overwrite:
            raise ValueError(f"prompt {name!r} already exists")
        now = time.time()
        created = existing["created_at"] if existing else now
        session.index(index=idx, id=name, refresh=True, body={
            "name": name, "description": description, "sql": sql, "dsl_json": dsl_json,
            "params_json": json.dumps(params or []),
            "created_at": created, "updated_at": now})

    def remove_prompt(self, session: OpenSearch, name: str) -> bool:
        ptr = self._pointer()
        try:
            session.delete(index=ptr.index("prompts"), id=name, refresh=True)
            return True
        except NotFoundError:
            return False

    # -- result cache (Phase 5) --------------------------------------------

    def _cache_get(self, session: OpenSearch, name: str,
                   args: dict) -> list[dict] | None:
        ptr = self._pointer()
        key, _ = _cache.make_key(name, args)
        doc = self._get_doc(session, ptr.index("cache"), key)
        if doc is None:
            return None
        if doc["ttl_seconds"] and (time.time() - doc["created_at"]) > doc["ttl_seconds"]:
            self._cache_delete(session, key)
            return None
        session.update(index=ptr.index("cache"), id=key, refresh=True,
                       body={"doc": {"hits": doc.get("hits", 0) + 1,
                                     "last_hit_at": time.time()}})
        return json.loads(doc["result_json"])

    def _cache_put(self, session: OpenSearch, name: str, args: dict,
                   result: list[dict], ttl_seconds: int) -> None:
        ptr = self._pointer()
        key, args_json = _cache.make_key(name, args)
        session.index(index=ptr.index("cache"), id=key, refresh=True, body={
            "key": key, "prompt_name": name, "args_json": args_json,
            "result_json": json.dumps(result), "created_at": time.time(),
            "hits": 0, "last_hit_at": None, "ttl_seconds": ttl_seconds})

    def _cache_delete(self, session: OpenSearch, key: str) -> None:
        try:
            session.delete(index=self._pointer().index("cache"), id=key, refresh=True)
        except NotFoundError:
            pass

    def cache_stats(self, session: OpenSearch) -> dict[str, Any]:
        ptr = self._pointer()
        self._ensure_indices(session, ptr, ["cache"])
        idx = ptr.index("cache")
        body = {"size": 0, "aggs": {
            "total_hits": {"sum": {"field": "hits"}},
            "by_prompt": {"terms": {"field": "prompt_name", "size": 1000}}}}
        res = session.search(index=idx, body=body)
        n = res["hits"]["total"]["value"] if isinstance(res["hits"]["total"], dict) \
            else res["hits"]["total"]
        aggs = res.get("aggregations", {})
        return {
            "enabled": _cache.enabled(),
            "entries": n,
            "total_hits": int(aggs.get("total_hits", {}).get("value", 0) or 0),
            "result_bytes": 0,
            "by_prompt": {b["key"]: b["doc_count"]
                          for b in aggs.get("by_prompt", {}).get("buckets", [])},
        }

    def cache_list(self, session: OpenSearch, prompt: str | None = None,
                   limit: int = 50) -> list[dict]:
        ptr = self._pointer()
        self._ensure_indices(session, ptr, ["cache"])
        query: dict = {"term": {"prompt_name": prompt}} if prompt else {"match_all": {}}
        body = {"size": limit, "query": query, "sort": [{"created_at": "desc"}]}
        hits = session.search(index=ptr.index("cache"), body=body)["hits"]["hits"]
        out = []
        for h in hits:
            s = h["_source"]
            out.append({
                "key": s["key"][:16], "prompt": s["prompt_name"],
                "args": json.loads(s["args_json"]), "bytes": len(s["result_json"]),
                "created_at": s["created_at"], "hits": s.get("hits", 0),
                "last_hit_at": s.get("last_hit_at"), "ttl_seconds": s["ttl_seconds"],
            })
        return out

    def cache_clear(self, session: OpenSearch, prompt: str | None = None) -> int:
        ptr = self._pointer()
        self._ensure_indices(session, ptr, ["cache"])
        query: dict = {"term": {"prompt_name": prompt}} if prompt else {"match_all": {}}
        res = session.delete_by_query(index=ptr.index("cache"),
                                      body={"query": query}, refresh=True)
        return res.get("deleted", 0)

    # -- run_prompt: dsl_json execution engine (Phase 4) -------------------

    @staticmethod
    def _render_dsl(dsl_json: str, args: dict[str, Any]) -> str:
        """Substitute `:param` tokens (as JSON string literals) with arg values."""
        out = dsl_json
        for k, v in args.items():
            out = out.replace(f'":{k}"', json.dumps(v))
        return out

    def _exec_dsl(self, session: OpenSearch, spec: dict) -> list[dict]:
        ptr = self._pointer()
        idx = ptr.index(spec["index"])
        res = session.search(index=idx, body=spec["body"])
        mode = spec.get("rows", "hits")
        if mode == "hits":
            return [h["_source"] for h in res["hits"]["hits"]]
        if mode.startswith("aggs:"):
            agg = mode.split(":", 1)[1]
            return [{"key": b["key"], "count": b["doc_count"]}
                    for b in res["aggregations"][agg]["buckets"]]
        raise ValueError(f"unknown dsl rows mode {mode!r}")

    def run_prompt(self, session: OpenSearch, name: str,
                   args: dict[str, Any] | None = None, *, use_cache: bool = True,
                   ttl_seconds: int = 0) -> tuple[list[dict], bool]:
        prompt = self.show_prompt(session, name)
        args_resolved = _prompts._coerce_args(prompt, args or {})

        if use_cache and _cache.enabled():
            cached = self._cache_get(session, name, args_resolved)
            if cached is not None:
                return cached, True

        if not prompt.dsl_json:
            raise NotImplementedError(
                f"prompt {name!r} is SQL-only (no dsl_json) and cannot run on the "
                "opensearch backend; run it on the sqlite backend, or add a dsl_json.")
        spec = json.loads(self._render_dsl(prompt.dsl_json, args_resolved))
        rows = self._exec_dsl(session, spec)

        if use_cache and _cache.enabled():
            self._cache_put(session, name, args_resolved, rows, ttl_seconds)
        return rows, False

    # -- bug knowledge base (Phase B4) -------------------------------------
    #
    # Same logical surface as the SQLite backend, but links are denormalized
    # into arrays on the bug document (per schema.BUG_LINK_FIELD) instead of a
    # side table, and search is a `multi_match` rather than FTS5/LIKE.

    @staticmethod
    def _bug_to_doc(bug: _bugs.Bug) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "name": bug.name, "title": bug.title, "status": bug.status,
            "severity": bug.severity, "symptom": bug.symptom,
            "root_cause": bug.root_cause, "fix": bug.fix, "fix_ref": bug.fix_ref,
            "keywords": list(bug.keywords), "tags": list(bug.tags),
            "created_at": bug.created_at, "updated_at": bug.updated_at,
        }
        for field in set(schema.BUG_LINK_FIELD.values()):
            doc[field] = []
        for lk in bug.links:
            field = schema.BUG_LINK_FIELD.get(lk.kind)
            if field and lk.value not in doc[field]:
                doc[field].append(lk.value)
        return doc

    @staticmethod
    def _doc_to_bug(doc: dict[str, Any]) -> _bugs.Bug:
        links: list[_bugs.BugLink] = []
        for kind, field in schema.BUG_LINK_FIELD.items():
            for val in doc.get(field, []) or []:
                links.append(_bugs.BugLink(kind=kind, value=val))
        return _bugs.Bug(
            name=doc["name"], title=doc.get("title", ""), status=doc.get("status", "open"),
            severity=doc.get("severity", ""), symptom=doc.get("symptom", ""),
            root_cause=doc.get("root_cause", ""), fix=doc.get("fix", ""),
            fix_ref=doc.get("fix_ref", ""), keywords=list(doc.get("keywords", []) or []),
            tags=list(doc.get("tags", []) or []), created_at=doc.get("created_at", 0.0),
            updated_at=doc.get("updated_at", 0.0), links=links,
        )

    def _bugs_idx(self, client: OpenSearch) -> str:
        """Ensure the bugs index exists on `client`; return its name."""
        ptr = self._pointer()
        self._ensure_indices(client, ptr, ["bugs"])
        return ptr.index("bugs")

    @staticmethod
    def _get_doc(client: OpenSearch, idx: str, slug: str) -> dict | None:
        try:
            return client.get(index=idx, id=slug)["_source"]
        except NotFoundError:
            return None

    def add_bug(self, session: OpenSearch, name: str, *, title: str = "",
                status: str = "open", severity: str = "", symptom: str = "",
                root_cause: str = "", fix: str = "", fix_ref: str = "",
                keywords: list[str] | None = None, tags: list[str] | None = None,
                links: list[Any] | None = None, overwrite: bool = False) -> _bugs.Bug:
        idx = self._bugs_idx(session)
        slug = _bugs.normalize_name(name)
        if not slug or not _bugs._VALID_NAME.match(slug):
            raise ValueError(f"invalid bug name {name!r}")
        existing = self._get_doc(session, idx, slug)
        if existing and not overwrite:
            raise ValueError(f"bug {slug!r} already exists (use overwrite to update)")
        now = time.time()
        created = existing["created_at"] if existing else now
        bug = _bugs.Bug(
            name=slug, title=title, status=status, severity=severity, symptom=symptom,
            root_cause=root_cause, fix=fix, fix_ref=fix_ref,
            keywords=list(keywords or []), tags=list(tags or []),
            created_at=created, updated_at=now, links=list(links or []))
        session.index(index=idx, id=slug, body=self._bug_to_doc(bug), refresh=True)
        return bug

    def get_bug(self, session: OpenSearch, name: str) -> _bugs.Bug | None:
        idx = self._bugs_idx(session)
        doc = self._get_doc(session, idx, _bugs.normalize_name(name))
        return self._doc_to_bug(doc) if doc else None

    def list_bugs(self, session: OpenSearch, *, status: str | None = None,
                  severity: str | None = None, tag: str | None = None,
                  limit: int = 50) -> list[_bugs.Bug]:
        idx = self._bugs_idx(session)
        filt = self._facet_filters(status, severity, tag)
        body = {
            "size": limit,
            "query": {"bool": {"filter": filt}} if filt else {"match_all": {}},
            "sort": [{"updated_at": "desc"}],
        }
        hits = session.search(index=idx, body=body)["hits"]["hits"]
        return [self._doc_to_bug(h["_source"]) for h in hits]

    def search_bugs(self, session: OpenSearch, query: str, *, status: str | None = None,
                    keyword: str | None = None, limit: int = 50) -> list[_bugs.Bug]:
        idx = self._bugs_idx(session)
        filt = self._facet_filters(status, None, None)
        if keyword:
            filt.append({"term": {"keywords": keyword}})
        query = (query or "").strip()
        if query:
            must: Any = {"multi_match": {
                "query": query,
                "fields": ["title", "symptom", "root_cause", "fix", "keywords", "tags"],
            }}
            body = {"size": limit, "query": {"bool": {"must": must, "filter": filt}}}
        else:
            body = {"size": limit,
                    "query": {"bool": {"filter": filt}} if filt else {"match_all": {}},
                    "sort": [{"updated_at": "desc"}]}
        hits = session.search(index=idx, body=body)["hits"]["hits"]
        return [self._doc_to_bug(h["_source"]) for h in hits]

    @staticmethod
    def _facet_filters(status: str | None, severity: str | None,
                       tag: str | None) -> list[dict]:
        filt: list[dict] = []
        if status:
            filt.append({"term": {"status": status}})
        if severity:
            filt.append({"term": {"severity": severity}})
        if tag:
            filt.append({"term": {"tags": tag}})
        return filt

    def link_bug(self, session: OpenSearch, name: str, kind: str, value: str,
                 extra: str = "") -> _bugs.Bug:
        if kind not in _bugs.LINK_KINDS:
            raise ValueError(
                f"invalid link kind {kind!r}; one of {', '.join(_bugs.LINK_KINDS)}")
        idx = self._bugs_idx(session)
        doc = self._get_doc(session, idx, _bugs.normalize_name(name))
        if doc is None:
            raise ValueError(f"no bug named {_bugs.normalize_name(name)!r}")
        bug = self._doc_to_bug(doc)
        if not any(l.kind == kind and l.value == value for l in bug.links):
            bug.links.append(_bugs.BugLink(kind=kind, value=value, extra=extra))
        bug.updated_at = time.time()
        session.index(index=idx, id=bug.name, body=self._bug_to_doc(bug), refresh=True)
        return bug

    def close_bug(self, session: OpenSearch, name: str, *, status: str = "fixed",
                  fix: str | None = None, fix_ref: str | None = None) -> _bugs.Bug:
        idx = self._bugs_idx(session)
        doc = self._get_doc(session, idx, _bugs.normalize_name(name))
        if doc is None:
            raise ValueError(f"no bug named {_bugs.normalize_name(name)!r}")
        bug = self._doc_to_bug(doc)
        bug.status = status
        if fix is not None:
            bug.fix = fix
        if fix_ref is not None:
            bug.fix_ref = fix_ref
        bug.updated_at = time.time()
        session.index(index=idx, id=bug.name, body=self._bug_to_doc(bug), refresh=True)
        return bug

    def remove_bug(self, session: OpenSearch, name: str) -> bool:
        idx = self._bugs_idx(session)
        try:
            session.delete(index=idx, id=_bugs.normalize_name(name), refresh=True)
            return True
        except NotFoundError:
            return False
