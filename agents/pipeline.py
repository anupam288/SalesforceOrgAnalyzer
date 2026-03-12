"""
agents/pipeline.py

LangGraph state machine — domain-agnostic Salesforce Org Intelligence pipeline.

Graph shape:

  connect → harvest ──┬── parse_apex    ──┐
                      ├── parse_flows    ──┤
                      ├── parse_ui       ──┤  (LWC + Aura + VF in parallel)
                      ├── parse_rules    ──┤  (Validation Rules + Workflow Rules)
                      └── parse_processes──┘  (Approval Processes + Process Builder)
                                          │
                                     reason (LLM analysis of all component types)
                                          │
                                    map_org (assemble OrgProfile)
                                          │
                                   write_docs (generate Markdown)

All five parse_* nodes run in parallel after harvest.
Annotated reducers on phase_timings and errors prevent concurrent write conflicts.
"""
import logging
import time
from typing import TypedDict, Annotated, Any

from langgraph.graph import StateGraph, END

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# REDUCERS
# ─────────────────────────────────────────────────────────────────────

def _merge_dicts(a: dict, b: dict) -> dict:
    return {**a, **b}

def _append_lists(a: list, b: list) -> list:
    return a + b


# ─────────────────────────────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────────────────────────────

class AgentState(TypedDict):
    config: dict
    output_dir: str

    # ── Raw harvested components ──────────────────────────────────────
    apex_classes:        list[dict]
    apex_triggers:       list[dict]
    flows:               list[dict]
    validation_rules:    list[dict]
    workflow_rules:      list[dict]
    approval_processes:  list[dict]
    lwc_components:      list[dict]
    aura_components:     list[dict]
    vf_pages:            list[dict]
    named_credentials:   list[dict]
    remote_site_settings: list[dict]
    connected_apps:      list[dict]
    custom_labels:       list[dict]
    call_graph:          dict

    # ── LLM annotations ───────────────────────────────────────────────
    annotations: dict[str, dict]   # api_name → ComponentAnnotation dict

    # ── Final outputs ─────────────────────────────────────────────────
    org_profile: Any               # OrgProfile object
    generated_files: list[str]

    # ── Execution tracking (Annotated = safe for concurrent node writes) ─
    phase_timings: Annotated[dict, _merge_dicts]
    errors:        Annotated[list, _append_lists]
    status: str


# ─────────────────────────────────────────────────────────────────────
# NODE 1 — CONNECT
# ─────────────────────────────────────────────────────────────────────

def node_connect(state: AgentState) -> dict:
    from tools.salesforce_client import SalesforceClient
    from config.settings import SalesforceConfig

    logger.info("Node: connect")
    t0 = time.time()
    cfg = state["config"]
    sf_cfg = SalesforceConfig(**cfg["salesforce"])
    client = SalesforceClient(
        config=sf_cfg,
        cache_dir=cfg.get("crawl", {}).get("cache_dir", ".cache/metadata"),
    )
    client.connect()
    _set_client("sf", client)
    return {
        "status": "Connected to Salesforce",
        "phase_timings": {"connect": round(time.time() - t0, 1)},
    }


# ─────────────────────────────────────────────────────────────────────
# NODE 2 — HARVEST  (all metadata types)
# ─────────────────────────────────────────────────────────────────────

def node_harvest(state: AgentState) -> dict:
    logger.info("Node: harvest")
    t0 = time.time()
    sf = _get_client("sf")

    def _safe(fn, label):
        try:
            result = fn()
            logger.info(f"  {label}: {len(result)}")
            return result
        except Exception as e:
            logger.warning(f"  {label} failed: {e}")
            return []

    apex_classes   = _safe(lambda: sf.retrieve_apex_source("ApexClass"),   "Apex classes")
    apex_triggers  = _safe(lambda: sf.retrieve_apex_source("ApexTrigger"), "Apex triggers")
    flows          = _safe(lambda: sf.retrieve_flows(),                    "Flows")
    val_rules      = _safe(lambda: sf.get_validation_rules(),              "Validation rules")
    wf_rules       = _safe(lambda: sf.retrieve_metadata_type("WorkflowRule"), "Workflow rules")
    approvals      = _safe(lambda: sf.get_approval_processes(),            "Approval processes")
    lwc            = _safe(lambda: sf.retrieve_metadata_type("LightningComponentBundle"), "LWC")
    aura           = _safe(lambda: sf.retrieve_metadata_type("AuraDefinitionBundle"),     "Aura")
    vf             = _safe(lambda: sf.retrieve_metadata_type("ApexPage"),               "VF pages")
    nc             = _safe(lambda: sf.get_named_credentials(),             "Named credentials")
    rss            = _safe(lambda: sf.get_remote_site_settings(),          "Remote site settings")
    apps           = _safe(lambda: sf.list_metadata("ConnectedApp"),       "Connected apps")
    labels         = _safe(lambda: sf.get_custom_labels(),                 "Custom labels")

    elapsed = round(time.time() - t0, 1)
    total = sum(map(len, [apex_classes, apex_triggers, flows, val_rules, wf_rules,
                          approvals, lwc, aura, vf, nc, rss, apps]))
    logger.info(f"  Harvest complete: {total} components in {elapsed}s")

    return {
        "status": f"Harvested {total} components",
        "apex_classes":         [c.to_dict() for c in apex_classes],
        "apex_triggers":        [c.to_dict() for c in apex_triggers],
        "flows":                [c.to_dict() for c in flows],
        "validation_rules":     [c.to_dict() for c in val_rules],
        "workflow_rules":       [c.to_dict() for c in wf_rules],
        "approval_processes":   [c.to_dict() for c in approvals],
        "lwc_components":       [c.to_dict() for c in lwc],
        "aura_components":      [c.to_dict() for c in aura],
        "vf_pages":             [c.to_dict() for c in vf],
        "named_credentials":    nc,
        "remote_site_settings": rss,
        "connected_apps":       apps,
        "custom_labels":        labels,
        "phase_timings": {"harvest": elapsed},
    }


# ─────────────────────────────────────────────────────────────────────
# NODE 3a — PARSE APEX
# ─────────────────────────────────────────────────────────────────────

def node_parse_apex(state: AgentState) -> dict:
    from parsers.apex_parser import ApexParser
    logger.info("Node: parse_apex")
    t0 = time.time()
    parser = ApexParser()

    class_objs   = [parser.parse(i["api_name"], i.get("raw_body", "")) for i in state.get("apex_classes", [])]
    trigger_objs = [parser.parse(i["api_name"], i.get("raw_body", "")) for i in state.get("apex_triggers", [])]
    call_graph   = parser.build_call_graph(class_objs + trigger_objs)

    return {
        "apex_classes":  [p.to_summary_dict() for p in class_objs],
        "apex_triggers": [p.to_summary_dict() for p in trigger_objs],
        "call_graph":    call_graph,
        "phase_timings": {"parse_apex": round(time.time() - t0, 1)},
    }


# ─────────────────────────────────────────────────────────────────────
# NODE 3b — PARSE FLOWS
# ─────────────────────────────────────────────────────────────────────

def node_parse_flows(state: AgentState) -> dict:
    from parsers.flow_parser import FlowParser
    logger.info("Node: parse_flows")
    t0 = time.time()
    parser = FlowParser()
    parsed = []
    for item in state.get("flows", []):
        try:
            p = parser.parse(item["api_name"], item.get("raw_body", ""))
            parsed.append(parser.to_summary_dict(p))
        except Exception as e:
            logger.warning(f"  Flow parse error {item['api_name']}: {e}")
            parsed.append({
                "api_name": item["api_name"], "label": item["api_name"],
                "trigger_type": "Unknown", "trigger_object": None,
                "entry_conditions": [], "pseudocode": f"[Parse error: {e}]",
                "element_counts": {}, "node_count": 0,
            })
    return {
        "flows": parsed,
        "phase_timings": {"parse_flows": round(time.time() - t0, 1)},
    }


# ─────────────────────────────────────────────────────────────────────
# NODE 3c — PARSE UI COMPONENTS  (LWC + Aura + Visualforce)
# ─────────────────────────────────────────────────────────────────────

def node_parse_ui(state: AgentState) -> dict:
    """Light normalisation pass for UI components — they're mostly raw XML/JS."""
    logger.info("Node: parse_ui")
    t0 = time.time()

    # LWC: concatenate all JS files into one raw_body for the reasoner
    lwc_merged = []
    for item in state.get("lwc_components", []):
        lwc_merged.append({
            **item,
            "raw_body": item.get("raw_body", "")[:4000],
        })

    aura_merged = []
    for item in state.get("aura_components", []):
        aura_merged.append({
            **item,
            "raw_body": item.get("raw_body", "")[:4000],
        })

    vf_merged = []
    for item in state.get("vf_pages", []):
        vf_merged.append({
            **item,
            "raw_body": item.get("raw_body", "")[:4000],
        })

    return {
        "lwc_components":  lwc_merged,
        "aura_components": aura_merged,
        "vf_pages":        vf_merged,
        "phase_timings":   {"parse_ui": round(time.time() - t0, 1)},
    }


# ─────────────────────────────────────────────────────────────────────
# NODE 3d — PARSE RULES  (Validation Rules + Workflow Rules)
# ─────────────────────────────────────────────────────────────────────

def node_parse_rules(state: AgentState) -> dict:
    logger.info("Node: parse_rules")
    t0 = time.time()
    logger.info(f"  {len(state.get('validation_rules', []))} validation rules ready")
    logger.info(f"  {len(state.get('workflow_rules', []))} workflow rules ready")
    return {
        "phase_timings": {"parse_rules": round(time.time() - t0, 1)},
    }


# ─────────────────────────────────────────────────────────────────────
# NODE 3e — PARSE PROCESSES  (Approval Processes + Process Builder)
# ─────────────────────────────────────────────────────────────────────

def node_parse_processes(state: AgentState) -> dict:
    logger.info("Node: parse_processes")
    t0 = time.time()
    logger.info(f"  {len(state.get('approval_processes', []))} approval processes ready")
    logger.info(f"  {len(state.get('connected_apps', []))} connected apps ready")
    return {
        "phase_timings": {"parse_processes": round(time.time() - t0, 1)},
    }


# ─────────────────────────────────────────────────────────────────────
# NODE 4 — REASON  (LLM analysis of ALL component types)
# ─────────────────────────────────────────────────────────────────────

def node_reason(state: AgentState) -> dict:
    from agents.semantic_reasoner import SemanticReasoner
    from tools.llm_client import build_llm_client

    logger.info("Node: reason — LLM analysis of all components")
    t0 = time.time()

    cfg    = state["config"]
    claude = build_llm_client(cfg.get("llm", cfg.get("anthropic", {})))
    reasoner = SemanticReasoner(claude)

    # ── Proxy objects (bridge dicts → attribute access) ───────────────

    class _ApexObj:
        def __init__(self, d):
            self.api_name        = d.get("api_name", "")
            self.class_type      = d.get("class_type", "class")
            self.is_test         = d.get("is_test", False)
            self.annotations     = d.get("annotations", [])
            self.all_soql        = d.get("soql_queries", [])
            self.all_dml         = d.get("dml_operations", [])
            self.all_callouts    = d.get("external_callouts", [])
            self.all_comments    = d.get("key_comments", [])
            self.trigger_objects = d.get("trigger_objects", [])
            self.trigger_events  = d.get("trigger_events", [])
            self.raw_body        = d.get("source_excerpt", "")

    class _FlowObj:
        def __init__(self, d):
            self.api_name         = d.get("api_name", "")
            self.label            = d.get("label", "")
            self.trigger_type     = d.get("trigger_type", "")
            self.trigger_object   = d.get("trigger_object")
            self.entry_conditions = d.get("entry_conditions", [])
            self.pseudocode       = d.get("pseudocode", "")
            self.raw_stats        = d.get("element_counts", {})

    class _RawObj:
        def __init__(self, d):
            self.api_name = d.get("api_name", "")
            self.raw_body = d.get("raw_body", "")

    all_annotations: dict[str, dict] = {}
    call_graph = state.get("call_graph", {})

    def _store(anns):
        for a in anns:
            all_annotations[a.api_name] = _ann_to_dict(a)

    # Apex
    _store(reasoner.annotate_apex(
        [_ApexObj(d) for d in state.get("apex_classes", [])], call_graph
    ))
    _store(reasoner.annotate_apex(
        [_ApexObj(d) for d in state.get("apex_triggers", [])], call_graph
    ))

    # Flows (all types)
    _store(reasoner.annotate_flows(
        [_FlowObj(d) for d in state.get("flows", [])]
    ))

    # Validation Rules
    _store(reasoner.annotate_validation_rules(
        [_RawObj(d) for d in state.get("validation_rules", [])]
    ))

    # Workflow Rules
    _store(reasoner.annotate_workflow_rules(
        [_RawObj(d) for d in state.get("workflow_rules", [])]
    ))

    # Approval Processes
    _store(reasoner.annotate_approval_processes(
        [_RawObj(d) for d in state.get("approval_processes", [])]
    ))

    # LWC
    _store(reasoner.annotate_lwc(
        [_RawObj(d) for d in state.get("lwc_components", [])]
    ))

    # Aura
    _store(reasoner.annotate_aura(
        [_RawObj(d) for d in state.get("aura_components", [])]
    ))

    # Visualforce
    _store(reasoner.annotate_visualforce(
        [_RawObj(d) for d in state.get("vf_pages", [])]
    ))

    # Outbound integrations (Named Credentials + Remote Site Settings)
    _store(reasoner.annotate_outbound_integrations(
        state.get("named_credentials", []),
        state.get("remote_site_settings", []),
    ))

    # Inbound integrations (Connected Apps)
    _store(reasoner.annotate_inbound_integrations(
        state.get("connected_apps", [])
    ))

    elapsed = round(time.time() - t0, 1)
    logger.info(f"  Annotated {len(all_annotations)} components in {elapsed}s")
    logger.info(f"  Claude usage: {claude.usage_summary()}")

    return {
        "annotations":   all_annotations,
        "status":        f"Analyzed {len(all_annotations)} components",
        "phase_timings": {"reason": elapsed},
        "errors":        [],
    }


# ─────────────────────────────────────────────────────────────────────
# NODE 5 — MAP ORG
# ─────────────────────────────────────────────────────────────────────

def node_map_org(state: AgentState) -> dict:
    from agents.org_mapper import OrgMapper
    from agents.semantic_reasoner import ComponentAnnotation
    from tools.llm_client import build_llm_client

    logger.info("Node: map_org")
    t0 = time.time()

    cfg    = state["config"]
    claude = build_llm_client(cfg.get("llm", cfg.get("anthropic", {})))

    # Reconstruct ComponentAnnotation objects from stored dicts
    annotations = {
        name: ComponentAnnotation(**{
            k: v for k, v in d.items() if k != "raw_llm_response"
        }, raw_llm_response={})
        for name, d in state.get("annotations", {}).items()
    }

    raw_integrations = (
        state.get("named_credentials", []) +
        state.get("remote_site_settings", [])
    )

    mapper = OrgMapper(claude)
    org_profile = mapper.map_org(annotations, raw_integrations=raw_integrations)

    elapsed = round(time.time() - t0, 1)
    logger.info(f"  Org profile built in {elapsed}s")

    return {
        "org_profile":   org_profile,
        "status":        "Org profile mapped",
        "phase_timings": {"map_org": elapsed},
    }


# ─────────────────────────────────────────────────────────────────────
# NODE 6 — WRITE DOCS
# ─────────────────────────────────────────────────────────────────────

def node_write_docs(state: AgentState) -> dict:
    from agents.doc_writer import DocWriter
    logger.info("Node: write_docs")
    t0 = time.time()

    writer = DocWriter(output_dir=state.get("output_dir", "output/docs"))
    files  = writer.write_all(state["org_profile"])

    elapsed = round(time.time() - t0, 1)
    logger.info(f"  Wrote {len(files)} files in {elapsed}s")

    return {
        "generated_files": [str(f) for f in files],
        "status":          f"Documentation complete: {len(files)} files",
        "phase_timings":   {"write_docs": elapsed},
    }


# ─────────────────────────────────────────────────────────────────────
# GRAPH CONSTRUCTION
# ─────────────────────────────────────────────────────────────────────

PARALLEL_PARSE_NODES = [
    "parse_apex", "parse_flows", "parse_ui", "parse_rules", "parse_processes"
]

def build_pipeline():
    graph = StateGraph(AgentState)

    graph.add_node("connect",         node_connect)
    graph.add_node("harvest",         node_harvest)
    graph.add_node("parse_apex",      node_parse_apex)
    graph.add_node("parse_flows",     node_parse_flows)
    graph.add_node("parse_ui",        node_parse_ui)
    graph.add_node("parse_rules",     node_parse_rules)
    graph.add_node("parse_processes", node_parse_processes)
    graph.add_node("reason",          node_reason)
    graph.add_node("map_org",         node_map_org)
    graph.add_node("write_docs",      node_write_docs)

    graph.set_entry_point("connect")
    graph.add_edge("connect", "harvest")

    # Fan-out: harvest → 5 parallel parse nodes
    for pn in PARALLEL_PARSE_NODES:
        graph.add_edge("harvest", pn)
        graph.add_edge(pn, "reason")   # Fan-in: all must complete before reason

    graph.add_edge("reason",    "map_org")
    graph.add_edge("map_org",   "write_docs")
    graph.add_edge("write_docs", END)

    return graph.compile()


def build_partial_pipeline(start_from: str):
    """Resume pipeline from a specific node."""
    SEQUENCE = ["connect", "harvest", "reason", "map_org", "write_docs"]
    NODE_FNS = {
        "connect":         node_connect,
        "harvest":         node_harvest,
        "parse_apex":      node_parse_apex,
        "parse_flows":     node_parse_flows,
        "parse_ui":        node_parse_ui,
        "parse_rules":     node_parse_rules,
        "parse_processes": node_parse_processes,
        "reason":          node_reason,
        "map_org":         node_map_org,
        "write_docs":      node_write_docs,
    }

    try:
        idx = SEQUENCE.index(start_from)
    except ValueError:
        idx = 2  # default to reason

    to_run = SEQUENCE[idx:]
    graph = StateGraph(AgentState)

    if "harvest" in to_run:
        # Include parallel parse nodes when starting from harvest
        for name in to_run:
            graph.add_node(name, NODE_FNS[name])
        for pn in PARALLEL_PARSE_NODES:
            graph.add_node(pn, NODE_FNS[pn])
        graph.set_entry_point("harvest")
        for pn in PARALLEL_PARSE_NODES:
            graph.add_edge("harvest", pn)
            graph.add_edge(pn, "reason")
        remaining = [n for n in to_run if n not in ("harvest", "reason")]
        prev = "reason"
        for n in remaining:
            graph.add_edge(prev, n)
            prev = n
    else:
        for name in to_run:
            graph.add_node(name, NODE_FNS[name])
        graph.set_entry_point(to_run[0])
        for i in range(len(to_run) - 1):
            graph.add_edge(to_run[i], to_run[i + 1])

    graph.add_edge(to_run[-1], END)
    return graph.compile()


# ─────────────────────────────────────────────────────────────────────
# CLIENT REGISTRY
# ─────────────────────────────────────────────────────────────────────

_clients: dict = {}

def _set_client(key, client):   _clients[key] = client
def _get_client(key):
    if key not in _clients:
        raise RuntimeError(f"Client '{key}' not initialized. Run node_connect first.")
    return _clients[key]


# ─────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────

def _ann_to_dict(ann) -> dict:
    return {
        "api_name":            ann.api_name,
        "metadata_type":       ann.metadata_type,
        "component_category":  ann.component_category,
        "purpose":             ann.purpose,
        "business_process":    ann.business_process,
        "trigger_condition":   ann.trigger_condition,
        "objects_read":        ann.objects_read,
        "objects_written":     ann.objects_written,
        "calls_apex":          ann.calls_apex,
        "calls_flows":         ann.calls_flows,
        "callouts":            ann.callouts,
        "integration_direction": ann.integration_direction,
        "endpoint_url":        ann.endpoint_url,
        "auth_mechanism":      ann.auth_mechanism,
        "data_exchanged":      ann.data_exchanged,
        "ui_context":          ann.ui_context,
        "user_facing_actions": ann.user_facing_actions,
        "hidden_logic":        ann.hidden_logic,
        "risk_flags":          ann.risk_flags,
        "dependencies":        ann.dependencies,
        "dependents":          ann.dependents,
        "confidence":          ann.confidence,
    }


def make_initial_state(config: dict, output_dir: str) -> AgentState:
    return AgentState(
        config=config,
        output_dir=output_dir,
        apex_classes=[], apex_triggers=[], flows=[],
        validation_rules=[], workflow_rules=[], approval_processes=[],
        lwc_components=[], aura_components=[], vf_pages=[],
        named_credentials=[], remote_site_settings=[], connected_apps=[],
        custom_labels=[], call_graph={},
        annotations={}, org_profile=None, generated_files=[],
        phase_timings={}, errors=[], status="initialized",
    )
