"""
ui_server.py  —  Flask backend for the SF Org Intelligence UI
Run:  python ui_server.py [--port 8080]
"""
import json, os, sys, threading, time, traceback, uuid, mimetypes
from pathlib import Path
from queue import Queue, Empty

sys.path.insert(0, str(Path(__file__).parent))

from flask import Flask, Response, jsonify, request, send_from_directory, send_file, abort
from flask_cors import CORS

app = Flask(__name__, static_folder="ui", static_url_path="")
CORS(app)

_jobs: dict[str, dict] = {}
_queues: dict[str, Queue] = {}

# ── STATIC ─────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("ui", "index.html")

# ── SERVE GENERATED MD FILES ────────────────────────────────────────────
# Markdown files are on disk; the UI fetches them via /api/file?path=...
@app.route("/api/file")
def serve_file():
    rel = request.args.get("path", "")
    # Security: only allow reading inside output directories
    base = Path(".").resolve()
    target = (base / rel).resolve()
    if not str(target).startswith(str(base)):
        abort(403)
    if not target.exists() or not target.is_file():
        abort(404)
    return send_file(target, mimetype="text/plain; charset=utf-8")

# ── TEST CONNECTIONS ────────────────────────────────────────────────────
@app.route("/api/test-salesforce", methods=["POST"])
def test_sf():
    d = request.json or {}
    try:
        from tools.salesforce_client import SalesforceClient
        from config.settings import SalesforceConfig
        cfg = SalesforceConfig(**{k:v for k,v in d.items() if v})
        sf = SalesforceClient(config=cfg, cache_dir=".cache/metadata")
        sf.connect()
        return jsonify({"ok": True, "instance_url": sf._instance_url})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

@app.route("/api/test-llm", methods=["POST"])
def test_llm():
    d = request.json or {}
    try:
        from tools.llm_client import build_llm_client
        client = build_llm_client(d)
        client.ask("Reply with just: OK", max_retries=1)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

# ── RUN PIPELINE ────────────────────────────────────────────────────────
@app.route("/api/run", methods=["POST"])
def run():
    d = request.json or {}
    jid = str(uuid.uuid4())[:8]
    _jobs[jid] = {"status":"queued","phase":"","log":[],"stats":{},"result":None,"error":None,"start":time.time()}
    _queues[jid] = Queue()
    threading.Thread(target=_run_job, args=(jid, d), daemon=True).start()
    return jsonify({"job_id": jid})

@app.route("/api/jobs/<jid>/stream")
def stream(jid):
    def gen():
        q = _queues.get(jid)
        if not q:
            yield f"data: {json.dumps({'type':'error','msg':'Job not found'})}\n\n"; return
        while True:
            try:
                ev = q.get(timeout=25)
                yield f"data: {json.dumps(ev)}\n\n"
                if ev.get("type") in ("done","error"): break
            except Empty:
                yield "data: {\"type\":\"ping\"}\n\n"
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

@app.route("/api/jobs/<jid>")
def job_status(jid):
    j = _jobs.get(jid)
    return jsonify(j) if j else (jsonify({"error":"not found"}), 404)

@app.route("/api/jobs/<jid>/result")
def job_result(jid):
    j = _jobs.get(jid)
    if not j: return jsonify({"error":"not found"}), 404
    if j["status"] != "done": return jsonify({"error":"not done"}), 400
    return jsonify(j.get("result", {}))

# ── PIPELINE PHASES (ordered) ───────────────────────────────────────────
PHASES = [
    ("connect",         "Connecting to Salesforce"),
    ("harvest",         "Harvesting metadata"),
    ("parse_apex",      "Parsing Apex code"),
    ("parse_flows",     "Parsing Flows"),
    ("parse_ui",        "Parsing UI components (LWC + Aura + VF)"),
    ("parse_rules",     "Parsing validation & workflow rules"),
    ("parse_processes", "Parsing approval processes"),
    ("reason",          "AI deep analysis"),
    ("map_org",         "Building org intelligence map"),
    ("write_docs",      "Writing documentation"),
]
PHASE_ORDER = [p[0] for p in PHASES]

def emit(jid, **kw):
    _queues[jid].put(kw)
    if kw.get("type") == "log":
        _jobs[jid]["log"].append(kw.get("msg",""))

def _run_job(jid, inp):
    job = _jobs[jid]
    # Track which phases have completed so we can send explicit done signals
    completed_phases = []

    try:
        job["status"] = "running"
        emit(jid, type="status", status="running")

        sf      = inp.get("salesforce", {})
        llm_cfg = inp.get("llm", {})
        out_dir = inp.get("output_dir", "output/docs")

        config = {
            "salesforce": {k:v for k,v in sf.items() if v},
            "llm": llm_cfg,
            "crawl": {
                "metadata_types":    "all",
                "enable_cache":      inp.get("cache", True),
                "cache_dir":         ".cache/metadata",
                "parallel_requests": 3,
                "requests_per_second": 5,
            },
            "output": {"output_dir": out_dir, "format": "markdown"},
        }

        emit(jid, type="log", msg="Configuration validated ✓")

        from agents.pipeline import build_pipeline, make_initial_state
        pipeline = build_pipeline()
        state    = make_initial_state(config, out_dir)
        stats    = {}

        for event in pipeline.stream(state, stream_mode="updates"):
            node = list(event.keys())[0]
            data = event[node]
            timings = data.get("phase_timings", {})
            stats.update(timings)

            # ── Mark this node done, signal any that were live before it ──
            # The pipeline emits an event AFTER a node completes, so we:
            #   1. Mark all previously "live" phases as done
            #   2. Mark this node as done immediately (it just finished)
            completed_phases.append(node)
            emit(jid, type="phase_done", name=node,
                 label=dict(PHASES).get(node, node))

            # Find the next phase in the pipeline order that hasn't completed
            next_phase = None
            for ph in PHASE_ORDER:
                if ph not in completed_phases:
                    next_phase = ph
                    break
            if next_phase:
                emit(jid, type="phase_live", name=next_phase,
                     label=dict(PHASES).get(next_phase, next_phase))

            # ── Node-specific data events ──────────────────────────────
            if node == "harvest":
                counts = {
                    "Apex Classes":       len(data.get("apex_classes",[])),
                    "Apex Triggers":      len(data.get("apex_triggers",[])),
                    "Flows":              len(data.get("flows",[])),
                    "LWC Components":     len(data.get("lwc_components",[])),
                    "Aura Components":    len(data.get("aura_components",[])),
                    "Validation Rules":   len(data.get("validation_rules",[])),
                    "Workflow Rules":     len(data.get("workflow_rules",[])),
                    "Approval Processes": len(data.get("approval_processes",[])),
                    "VF Pages":           len(data.get("vf_pages",[])),
                    "Named Credentials":  len(data.get("named_credentials",[])),
                    "Connected Apps":     len(data.get("connected_apps",[])),
                }
                # Drop zero-count entries so the grid isn't cluttered
                counts = {k:v for k,v in counts.items() if v > 0}
                stats["counts"] = counts
                total = sum(counts.values())
                emit(jid, type="harvest", counts=counts, total=total)
                emit(jid, type="log", msg=f"Harvested {total} components ✓")

            elif node == "reason":
                n = len(data.get("annotations", {}))
                stats["analyzed"] = n
                emit(jid, type="log", msg=f"AI analyzed {n} components ✓")

            elif node == "write_docs":
                files = data.get("generated_files", [])
                stats["files"] = len(files)
                emit(jid, type="log", msg=f"Generated {len(files)} documentation files ✓")

            t = list(timings.values())[0] if timings else None
            if t:
                emit(jid, type="log",
                     msg=f"{dict(PHASES).get(node, node)} completed in {round(t,1)}s ✓")

        result = _build_result(out_dir, stats)
        job["result"] = result
        job["status"] = "done"
        job["stats"]  = stats
        total_t = round(time.time() - job["start"], 1)
        emit(jid, type="log", msg=f"Complete in {total_t}s ✓")
        emit(jid, type="done", stats=stats, total=total_t)

    except Exception as e:
        job["status"] = "error"
        job["error"]  = str(e)
        emit(jid, type="log", msg=f"Error: {e}")
        emit(jid, type="error", msg=str(e), tb=traceback.format_exc())


def _build_result(out_dir, stats):
    p = Path(out_dir)
    result = {"output_dir": out_dir, "stats": stats, "sections": {}, "categories": []}

    # Top-level markdown files (read inline — no broken links)
    for key, fname in [
        ("overview",  "00-org-overview.md"),
        ("hidden",    "hidden-logic-discovered.md"),
        ("risks",     "risk-register.md"),
        ("outbound",  "outbound-integrations.md"),
        ("inbound",   "inbound-integrations.md"),
        ("objects",   "object-usage-map.md"),
        ("callouts",  "callout-map.md"),
    ]:
        fp = p / fname
        if fp.exists():
            result["sections"][key] = _rewrite_md_links(fp.read_text(encoding="utf-8"), fp.parent)

    # Category sub-folders  (any folder that has overview.md)
    if p.exists():
        for d in sorted(p.iterdir()):
            if not d.is_dir():
                continue
            ov_path   = d / "overview.md"
            comp_path = d / "components.md"
            if not ov_path.exists():
                continue
            ov   = _rewrite_md_links(ov_path.read_text(encoding="utf-8"),   d)
            comp = _rewrite_md_links(comp_path.read_text(encoding="utf-8"), d) if comp_path.exists() else ""
            result["categories"].append({
                "slug":       d.name,
                "name":       d.name.replace("-", " ").title(),
                "overview":   ov,
                "components": comp,
                "count":      ov.count("| [`"),
            })

    return result


def _rewrite_md_links(md: str, base_dir: Path) -> str:
    """
    Rewrite relative .md file links in markdown so they become API calls
    the SPA can handle, instead of broken file-system paths.

    e.g.  [components](components.md#anchor)
    →     [components](#cat:components.md:anchor)   (handled by UI router)

    Also rewrites  (../other-folder/overview.md)  etc.
    """
    import re

    def replace_link(m):
        text   = m.group(1)
        target = m.group(2)
        anchor = m.group(3) or ""

        # Skip http/https/mailto/anchor-only links
        if target.startswith(("http://","https://","mailto:","#")) or not target:
            return m.group(0)

        # Resolve the target relative to base_dir
        resolved = (base_dir / target).resolve()
        # Make it relative to cwd so the UI can request it
        try:
            rel = str(resolved.relative_to(Path(".").resolve()))
        except ValueError:
            rel = str(resolved)

        # Encode as a special hash route the SPA intercepts
        anchor_part = f":{anchor}" if anchor else ""
        return f"[{text}](#mdlink:{rel}{anchor_part})"

    # Match [text](path.md) or [text](path.md#anchor)
    pattern = r'\[([^\]]+)\]\(([^)#\s]+\.md)(#[^)]+)?\)'
    return re.sub(pattern, replace_link, md)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--host", type=str, default="0.0.0.0")
    args = parser.parse_args()
    print("\n  ◈  Salesforce Org Intelligence")
    print(f"  →  http://localhost:{args.port}\n")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
