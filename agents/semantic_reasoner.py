"""
agents/semantic_reasoner.py

Universal Salesforce Org Intelligence layer.
Analyzes every metadata component type and produces structured documentation
of what each component does — completely independent of any business domain.

Supported component types:
  - ApexClass / ApexTrigger
  - Flow (Screen, Record-Triggered, Scheduled, AutoLaunched)
  - ProcessBuilder (legacy)
  - WorkflowRule + WorkflowFieldUpdate / WorkflowAlert / WorkflowTask
  - ValidationRule
  - LightningComponentBundle (LWC)
  - AuraDefinitionBundle
  - ApexPage (Visualforce)
  - ApprovalProcess
  - NamedCredential / RemoteSiteSetting (Outbound Integration)
  - ConnectedApp / ExternalService (Inbound Integration / REST API)
  - CustomObject / CustomField
"""
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# COMPONENT CATEGORIES — domain-agnostic taxonomy
# ─────────────────────────────────────────────────────────────────────
COMPONENT_CATEGORIES = [
    "Apex Class",
    "Apex Trigger",
    "Flow — Record Triggered",
    "Flow — Screen Flow",
    "Flow — Scheduled",
    "Flow — AutoLaunched",
    "Process Builder",
    "Workflow Rule",
    "Validation Rule",
    "LWC Component",
    "Aura Component",
    "Visualforce Page",
    "Approval Process",
    "Outbound Integration",
    "Inbound Integration / REST API",
    "Custom Object",
    "Custom Field",
    "Other",
]


@dataclass
class ComponentAnnotation:
    """
    Complete AI-generated documentation for one Salesforce metadata component.
    Fully domain-agnostic — works for any Salesforce org.
    """
    api_name: str
    metadata_type: str          # Raw SF metadata type (ApexClass, Flow, etc.)
    component_category: str     # Human-friendly category from COMPONENT_CATEGORIES

    # ── What it does ─────────────────────────────────────────────────
    purpose: str                # 2-3 sentence plain English description
    business_process: str       # Which business process / module this belongs to
    trigger_condition: str      # WHEN does this fire? (for triggers, flows, rules)

    # ── What it touches ──────────────────────────────────────────────
    objects_read: list[str]     # SF objects this reads from
    objects_written: list[str]  # SF objects this creates/updates/deletes
    calls_apex: list[str]       # Apex classes/methods called
    calls_flows: list[str]      # Flows/processes called
    callouts: list[str]         # External HTTP endpoints / Named Credentials called

    # ── Integration specifics (for integration components) ───────────
    integration_direction: str  # "outbound", "inbound", "both", or ""
    endpoint_url: str           # URL / Named Credential reference
    auth_mechanism: str         # OAuth, Basic, JWT, API Key, etc.
    data_exchanged: str         # What data goes in/out

    # ── UI specifics (for LWC / Aura / VF) ──────────────────────────
    ui_context: str             # Where is this rendered? (Record page, App page, etc.)
    user_facing_actions: list[str]  # What can a user do in this component?

    # ── Quality signals ───────────────────────────────────────────────
    hidden_logic: list[str]     # Non-obvious business rules embedded in code
    risk_flags: list[str]       # Tech debt, bugs, governor limit risks
    dependencies: list[str]     # Other components this depends on
    dependents: list[str]       # Other components that call/use this

    confidence: float           # 0.0–1.0
    raw_llm_response: dict


SYSTEM_PROMPT = """\
You are an expert Salesforce architect performing a technical audit of a Salesforce org.
Your job is to read metadata and write precise, accurate documentation of what each
component does — independent of any specific business domain.

Rules:
- Be specific and factual. Never guess vaguely.
- Extract actual field names, object names, endpoint URLs from the code.
- For hidden logic: surface thresholds, conditions, routing rules embedded in code.
- For risk flags: identify SOQL in loops, missing null checks, hardcoded values,
  missing error handling, deprecated APIs, no test coverage.
- If source is empty or too short to analyze, say so honestly.
- Always respond with valid JSON only. No markdown fences, no preamble.
"""


class SemanticReasoner:
    """
    Runs Claude analysis on every Salesforce metadata component.
    Returns ComponentAnnotation objects — fully domain-agnostic.
    """

    def __init__(self, claude_client):
        self.claude = claude_client
        self.annotations: dict[str, ComponentAnnotation] = {}

    # ─────────────────────────────────────────────────────────────────
    # APEX CLASSES & TRIGGERS
    # ─────────────────────────────────────────────────────────────────

    def annotate_apex(self, apex_list: list, call_graph: dict) -> list[ComponentAnnotation]:
        non_test = [a for a in apex_list if not a.is_test]
        logger.info(f"Annotating {len(non_test)} Apex components (skipping {len(apex_list)-len(non_test)} test classes)...")
        results = []

        for i, apex in enumerate(non_test):
            logger.info(f"  [{i+1}/{len(non_test)}] {apex.api_name}")
            deps = call_graph.get(apex.api_name, {})

            is_trigger = apex.class_type == "trigger"
            trigger_info = (
                f"Fires on {apex.trigger_objects} for events: {', '.join(apex.trigger_events)}"
                if is_trigger else "N/A — this is a class, not a trigger"
            )
            category = "Apex Trigger" if is_trigger else "Apex Class"

            annotations_str = ", ".join(apex.annotations) if apex.annotations else "None"
            # Detect special class types from annotations
            if "RestResource" in annotations_str:
                category = "Inbound Integration / REST API"
            elif "AuraEnabled" in annotations_str:
                category = "Apex Class"  # Controller for LWC/Aura

            prompt = f"""Analyze this Salesforce Apex {apex.class_type}.

API NAME: {apex.api_name}
CATEGORY: {category}
ANNOTATIONS: {annotations_str}
TRIGGER INFO: {trigger_info}

SOQL QUERIES (objects read):
{chr(10).join(f'  {q}' for q in apex.all_soql[:8]) or '  None'}

DML OPERATIONS (objects written):
{chr(10).join(f'  {d}' for d in apex.all_dml[:10]) or '  None'}

EXTERNAL HTTP CALLOUTS:
{chr(10).join(f'  {c}' for c in apex.all_callouts) or '  None'}

CALLS THESE CLASSES (from call graph):
{chr(10).join(f'  {c}' for c in deps.get('calls', [])[:8]) or '  None detected'}

CALLED BY THESE CLASSES:
{chr(10).join(f'  {c}' for c in deps.get('called_by', [])[:8]) or '  None detected'}

INLINE COMMENTS (developer notes):
{chr(10).join(f'  {c}' for c in apex.all_comments[:6]) or '  None'}

SOURCE CODE:
{apex.raw_body[:3000] if apex.raw_body else '[No source available]'}

Return JSON with exactly these keys:
{{
  "purpose": "2-3 sentences: what does this class/trigger do in plain English?",
  "business_process": "Which module or process does this belong to? (e.g. 'Account Management', 'Order Processing', 'User Onboarding', 'Data Sync with ERP')",
  "trigger_condition": "For triggers: WHEN does it fire and on what conditions? For classes: how is it invoked?",
  "objects_read": ["list of SF object API names this SELECTs from"],
  "objects_written": ["list of SF object API names this inserts/updates/deletes"],
  "calls_apex": ["ApexClassName.methodName patterns found"],
  "calls_flows": ["any Flow API names invoked"],
  "callouts": ["endpoint URLs or NamedCredential references"],
  "integration_direction": "outbound if makes HTTP calls, inbound if @RestResource, both, or empty string",
  "endpoint_url": "specific URL or Named Credential if this is an integration class, else empty string",
  "auth_mechanism": "how does it authenticate to external systems? empty string if N/A",
  "data_exchanged": "what data is sent/received in integrations? empty string if N/A",
  "ui_context": "if @AuraEnabled controller or RemoteAction: which pages/components use this? else empty string",
  "user_facing_actions": [],
  "hidden_logic": [
    "Any non-obvious business rules, thresholds, conditions embedded in the code",
    "Be specific: include actual field names and values"
  ],
  "risk_flags": [
    "SOQL in loop — <MethodName> at line ~N",
    "Hardcoded value — <field> = <value>, should be in Custom Setting",
    "No null check before — <variable>.fieldName",
    "Missing try/catch around HTTP callout",
    "No test class found (inferred)"
  ],
  "dependencies": ["other Apex classes, objects, flows this needs to work"],
  "confidence": 0.85
}}"""

            ann = self._run_and_build(apex.api_name, category, prompt, deps)
            results.append(ann)

        return results

    # ─────────────────────────────────────────────────────────────────
    # FLOWS (all types)
    # ─────────────────────────────────────────────────────────────────

    def annotate_flows(self, flow_list: list) -> list[ComponentAnnotation]:
        logger.info(f"Annotating {len(flow_list)} Flows...")
        results = []

        for i, flow in enumerate(flow_list):
            logger.info(f"  [{i+1}/{len(flow_list)}] {flow.api_name}")

            # Determine Flow category from trigger type
            tt = (flow.trigger_type or "").lower()
            if "recordaftersave" in tt or "recordbeforesave" in tt:
                category = "Flow — Record Triggered"
            elif "scheduled" in tt:
                category = "Flow — Scheduled"
            elif "screen" in tt or flow.raw_stats.get("screens", 0) > 0:
                category = "Flow — Screen Flow"
            else:
                category = "Flow — AutoLaunched"

            # Detect Process Builder
            if tt in ("workflow", "customevent") or "process" in (flow.api_name or "").lower():
                category = "Process Builder"

            prompt = f"""Analyze this Salesforce Flow / Process Builder automation.

API NAME: {flow.api_name}
LABEL: {flow.label}
CATEGORY: {category}
TRIGGER TYPE: {flow.trigger_type}
TRIGGER OBJECT: {flow.trigger_object or 'N/A'}
ENTRY CONDITIONS: {'; '.join(flow.entry_conditions) if flow.entry_conditions else 'None'}

ELEMENT COUNTS: {flow.raw_stats}

EXECUTION PSEUDOCODE (full logic):
{flow.pseudocode[:4000] if flow.pseudocode else '[No pseudocode available]'}

Return JSON with exactly these keys:
{{
  "purpose": "2-3 sentences: what does this flow do in plain English?",
  "business_process": "Which business module or process does this automate?",
  "trigger_condition": "Exactly when does this run? On which object, which field change, which condition?",
  "objects_read": ["SF objects looked up in this flow"],
  "objects_written": ["SF objects created or updated by this flow"],
  "calls_apex": ["Apex class names called via Action elements"],
  "calls_flows": ["sub-flow API names called"],
  "callouts": ["any external action names that suggest HTTP calls"],
  "integration_direction": "outbound if calls external actions, else empty string",
  "endpoint_url": "",
  "auth_mechanism": "",
  "data_exchanged": "",
  "ui_context": "For screen flows: where is this launched from? (button, quick action, app page?)",
  "user_facing_actions": ["for screen flows: what inputs does the user provide? what decisions do they make?"],
  "hidden_logic": [
    "Every non-obvious routing rule, threshold, condition, or assignment in the flow",
    "Include actual field names and values from the pseudocode"
  ],
  "risk_flags": [
    "Flows with no fault connector on Apex actions",
    "Hardcoded IDs (record IDs, user IDs) in assignments",
    "No null checks before field references",
    "Unhandled fault paths"
  ],
  "dependencies": ["objects, Apex classes, other flows this depends on"],
  "confidence": 0.85
}}"""

            ann = self._run_and_build(flow.api_name, category, prompt)
            results.append(ann)

        return results

    # ─────────────────────────────────────────────────────────────────
    # VALIDATION RULES
    # ─────────────────────────────────────────────────────────────────

    def annotate_validation_rules(self, vr_list: list) -> list[ComponentAnnotation]:
        logger.info(f"Annotating {len(vr_list)} Validation Rules...")
        results = []

        for rule in vr_list:
            full_name = rule.api_name
            parts = full_name.split(".")
            object_name = parts[0] if len(parts) > 1 else "Unknown"
            rule_name = parts[-1]

            formula   = self._xml_val(rule.raw_body, "errorConditionFormula") or "Not available"
            error_msg = self._xml_val(rule.raw_body, "errorMessage") or "Not available"
            error_fld = self._xml_val(rule.raw_body, "errorDisplayField") or "Page"
            active    = self._xml_val(rule.raw_body, "active") or "true"

            prompt = f"""Analyze this Salesforce Validation Rule.

OBJECT: {object_name}
RULE NAME: {rule_name}
ACTIVE: {active}

VALIDATION FORMULA (fires when TRUE = show error):
{formula[:1500]}

ERROR MESSAGE SHOWN TO USER:
{error_msg[:400]}

ERROR DISPLAYED ON FIELD: {error_fld}

Return JSON with exactly these keys:
{{
  "purpose": "Plain English: what does this rule enforce or prevent? Who does it affect?",
  "business_process": "Which process or data quality concern does this support?",
  "trigger_condition": "When exactly does this fire? On insert only, update only, both? Any record type restrictions?",
  "objects_read": ["{object_name}"],
  "objects_written": [],
  "calls_apex": [],
  "calls_flows": [],
  "callouts": [],
  "integration_direction": "",
  "endpoint_url": "",
  "auth_mechanism": "",
  "data_exchanged": "",
  "ui_context": "Field '{error_fld}' on {object_name} layout",
  "user_facing_actions": [],
  "hidden_logic": [
    "Any non-obvious conditions in the formula — thresholds, cross-object checks, date logic",
    "Be specific about field names and values from the formula"
  ],
  "risk_flags": [
    "Fires on all record types including internal/system ones — may block automation",
    "Complex formula with no comments — hard to maintain",
    "Cross-object formula reference — may cause SOQL limits at scale"
  ],
  "dependencies": ["fields and objects referenced in the formula"],
  "confidence": 0.9
}}"""

            ann = self._run_and_build(full_name, "Validation Rule", prompt)
            results.append(ann)

        return results

    # ─────────────────────────────────────────────────────────────────
    # LWC COMPONENTS
    # ─────────────────────────────────────────────────────────────────

    def annotate_lwc(self, lwc_list: list) -> list[ComponentAnnotation]:
        logger.info(f"Annotating {len(lwc_list)} LWC components...")
        results = []

        for comp in lwc_list:
            prompt = f"""Analyze this Salesforce Lightning Web Component (LWC).

COMPONENT NAME: {comp.api_name}

JS CONTROLLER SOURCE:
{comp.raw_body[:3000] if comp.raw_body else '[No source available]'}

Return JSON:
{{
  "purpose": "What does this UI component do? What does the user see and interact with?",
  "business_process": "Which screen, page, or workflow is this component part of?",
  "trigger_condition": "Where is this component placed? (Record page, App page, Utility bar, Flow screen, etc.)",
  "objects_read": ["SF objects queried via @wire or imperative apex calls"],
  "objects_written": ["SF objects modified via apex calls"],
  "calls_apex": ["Apex methods imported and called"],
  "calls_flows": ["any Flow invocations"],
  "callouts": [],
  "integration_direction": "",
  "endpoint_url": "",
  "auth_mechanism": "",
  "data_exchanged": "",
  "ui_context": "Describe the layout context: record page, list view, community page, etc.",
  "user_facing_actions": [
    "List every button, form, action the user can take in this component"
  ],
  "hidden_logic": [
    "Client-side business rules embedded in JS",
    "Field visibility or conditional rendering logic",
    "Validation performed before server call"
  ],
  "risk_flags": [
    "No error handling on wire or apex calls",
    "Hardcoded record IDs or org-specific values",
    "Missing accessibility attributes",
    "Large data fetching without pagination"
  ],
  "dependencies": ["apex controllers, child components, custom events used"],
  "confidence": 0.80
}}"""

            ann = self._run_and_build(comp.api_name, "LWC Component", prompt)
            results.append(ann)

        return results

    # ─────────────────────────────────────────────────────────────────
    # AURA COMPONENTS
    # ─────────────────────────────────────────────────────────────────

    def annotate_aura(self, aura_list: list) -> list[ComponentAnnotation]:
        logger.info(f"Annotating {len(aura_list)} Aura components...")
        results = []

        for comp in aura_list:
            prompt = f"""Analyze this Salesforce Aura (Lightning) Component.

COMPONENT NAME: {comp.api_name}

SOURCE:
{comp.raw_body[:3000] if comp.raw_body else '[No source available]'}

Return JSON:
{{
  "purpose": "What does this Aura component do? What does the user see?",
  "business_process": "Which page or process is this used in?",
  "trigger_condition": "Where is this component deployed? (App Builder, Community, Utility bar, etc.)",
  "objects_read": ["SF objects accessed"],
  "objects_written": ["SF objects modified"],
  "calls_apex": ["Apex controllers used"],
  "calls_flows": [],
  "callouts": [],
  "integration_direction": "",
  "endpoint_url": "",
  "auth_mechanism": "",
  "data_exchanged": "",
  "ui_context": "Layout context description",
  "user_facing_actions": ["actions and interactions available to user"],
  "hidden_logic": ["client-side business rules in the controller JS"],
  "risk_flags": [
    "Legacy Aura component — consider migrating to LWC",
    "No error handling on server calls",
    "Hardcoded values in component"
  ],
  "dependencies": ["apex controllers, child components, events"],
  "confidence": 0.75
}}"""

            ann = self._run_and_build(comp.api_name, "Aura Component", prompt)
            results.append(ann)

        return results

    # ─────────────────────────────────────────────────────────────────
    # VISUALFORCE PAGES
    # ─────────────────────────────────────────────────────────────────

    def annotate_visualforce(self, vf_list: list) -> list[ComponentAnnotation]:
        logger.info(f"Annotating {len(vf_list)} Visualforce Pages...")
        results = []

        for page in vf_list:
            prompt = f"""Analyze this Salesforce Visualforce Page.

PAGE NAME: {page.api_name}

SOURCE MARKUP:
{page.raw_body[:3000] if page.raw_body else '[No source available]'}

Return JSON:
{{
  "purpose": "What does this page display or allow the user to do?",
  "business_process": "Which business function does this page serve?",
  "trigger_condition": "How is this page accessed? (Button, direct URL, override, email link, etc.)",
  "objects_read": ["SF objects queried by this page's controller"],
  "objects_written": ["SF objects saved by this page"],
  "calls_apex": ["Apex controller class names"],
  "calls_flows": [],
  "callouts": [],
  "integration_direction": "",
  "endpoint_url": "",
  "auth_mechanism": "",
  "data_exchanged": "",
  "ui_context": "Visualforce page — typically rendered in Classic or as override in Lightning",
  "user_facing_actions": ["forms, buttons, and actions on this page"],
  "hidden_logic": ["any conditional rendering or business rules in the markup/controller"],
  "risk_flags": [
    "Legacy Visualforce — consider migrating to LWC",
    "No CSRF protection (missing PageReference patterns)",
    "renderAs='pdf' pages may have encoding issues",
    "Inline SOQL in component binding"
  ],
  "dependencies": ["apex controller, extensions, static resources"],
  "confidence": 0.75
}}"""

            ann = self._run_and_build(page.api_name, "Visualforce Page", prompt)
            results.append(ann)

        return results

    # ─────────────────────────────────────────────────────────────────
    # WORKFLOW RULES
    # ─────────────────────────────────────────────────────────────────

    def annotate_workflow_rules(self, wf_list: list) -> list[ComponentAnnotation]:
        logger.info(f"Annotating {len(wf_list)} Workflow Rules...")
        results = []

        for rule in wf_list:
            prompt = f"""Analyze this Salesforce Workflow Rule.

RULE NAME: {rule.api_name}

SOURCE XML:
{rule.raw_body[:3000] if rule.raw_body else '[No source available]'}

Return JSON:
{{
  "purpose": "What does this workflow rule do when it fires?",
  "business_process": "Which business process does this automate?",
  "trigger_condition": "On which object, which event (create/update), and which conditions does it fire?",
  "objects_read": ["object this rule is on"],
  "objects_written": ["objects updated by field update actions"],
  "calls_apex": [],
  "calls_flows": [],
  "callouts": ["any outbound message endpoints"],
  "integration_direction": "outbound if has outbound message actions, else empty",
  "endpoint_url": "outbound message endpoint URL if present",
  "auth_mechanism": "",
  "data_exchanged": "fields sent in outbound message if applicable",
  "ui_context": "",
  "user_facing_actions": [],
  "hidden_logic": [
    "Specific field values set, email templates used, time-based actions",
    "Include actual field names and values"
  ],
  "risk_flags": [
    "Legacy Workflow Rule — consider migrating to Flow",
    "Time-based actions with no dequeue handling",
    "Outbound messages with no retry/failure handling"
  ],
  "dependencies": ["email templates, field update targets, outbound endpoints"],
  "confidence": 0.80
}}"""

            ann = self._run_and_build(rule.api_name, "Workflow Rule", prompt)
            results.append(ann)

        return results

    # ─────────────────────────────────────────────────────────────────
    # APPROVAL PROCESSES
    # ─────────────────────────────────────────────────────────────────

    def annotate_approval_processes(self, ap_list: list) -> list[ComponentAnnotation]:
        logger.info(f"Annotating {len(ap_list)} Approval Processes...")
        results = []

        for ap in ap_list:
            prompt = f"""Analyze this Salesforce Approval Process.

PROCESS NAME: {ap.api_name}

SOURCE XML:
{ap.raw_body[:3000] if ap.raw_body else '[No source available]'}

Return JSON:
{{
  "purpose": "What record type does this approve, and what is the business significance of the approval?",
  "business_process": "Which approval workflow does this implement? (e.g. expense approval, discount approval, contract sign-off)",
  "trigger_condition": "What criteria must be met to submit a record for approval? Who submits it?",
  "objects_read": ["object this process runs on"],
  "objects_written": ["fields updated on approve/reject"],
  "calls_apex": ["any Apex invoked on approval/rejection"],
  "calls_flows": [],
  "callouts": [],
  "integration_direction": "",
  "endpoint_url": "",
  "auth_mechanism": "",
  "data_exchanged": "",
  "ui_context": "Approval triggered from record page",
  "user_facing_actions": [
    "Submit for approval",
    "Approve",
    "Reject",
    "Recall"
  ],
  "hidden_logic": [
    "Approval steps and who approves at each step",
    "Escalation rules if approver doesn't act",
    "Field values set on approval vs rejection",
    "Whether delegation is allowed"
  ],
  "risk_flags": [
    "Single approver — no backup/delegation configured",
    "No recall action configured",
    "Field lock on submission may block automation"
  ],
  "dependencies": ["approver users/queues/roles, email templates, field updates"],
  "confidence": 0.82
}}"""

            ann = self._run_and_build(ap.api_name, "Approval Process", prompt)
            results.append(ann)

        return results

    # ─────────────────────────────────────────────────────────────────
    # OUTBOUND INTEGRATIONS (Named Credentials, Remote Site Settings)
    # ─────────────────────────────────────────────────────────────────

    def annotate_outbound_integrations(self, nc_list: list, rss_list: list) -> list[ComponentAnnotation]:
        logger.info(f"Annotating {len(nc_list)} Named Credentials + {len(rss_list)} Remote Site Settings...")
        results = []

        for item in nc_list:
            name     = item.get("fullName", item.get("api_name", ""))
            endpoint = item.get("endpoint", "")
            auth     = item.get("authProtocol", item.get("principalType", ""))

            prompt = f"""Analyze this Salesforce Named Credential (outbound integration endpoint).

NAME: {name}
ENDPOINT URL: {endpoint}
AUTH PROTOCOL: {auth}
RAW CONFIG: {str(item)[:500]}

Return JSON:
{{
  "purpose": "What external system does this connect to? What is the integration for?",
  "business_process": "Which business function does this external system support?",
  "trigger_condition": "Called from Apex via callout:NamedCredential pattern",
  "objects_read": [],
  "objects_written": [],
  "calls_apex": [],
  "calls_flows": [],
  "callouts": ["{endpoint}"],
  "integration_direction": "outbound",
  "endpoint_url": "{endpoint}",
  "auth_mechanism": "{auth} — describe what this means (OAuth 2.0, Basic Auth, JWT, API Key, etc.)",
  "data_exchanged": "Infer from the endpoint URL what data is likely exchanged (e.g. credit bureau data, payment info, document signing)",
  "ui_context": "",
  "user_facing_actions": [],
  "hidden_logic": [],
  "risk_flags": [
    "No certificate pinning",
    "Credentials stored in Named Credential — ensure rotation policy exists",
    "Timeout not configured — default may be too long/short"
  ],
  "dependencies": ["Apex classes that reference callout:{name}"],
  "confidence": 0.88
}}"""

            ann = self._run_and_build(name, "Outbound Integration", prompt)
            results.append(ann)

        # Remote Site Settings — simpler, batch summarise
        for rss in rss_list:
            name = rss.get("fullName", rss.get("api_name", ""))
            url  = rss.get("url", "")
            ann = ComponentAnnotation(
                api_name=name,
                metadata_type="RemoteSiteSetting",
                component_category="Outbound Integration",
                purpose=f"Remote Site Setting whitelisting outbound callouts to: {url}",
                business_process="Network security — allows Apex to make HTTP calls to this domain",
                trigger_condition="Required before any Apex HTTP callout to this domain",
                objects_read=[], objects_written=[], calls_apex=[], calls_flows=[],
                callouts=[url],
                integration_direction="outbound",
                endpoint_url=url,
                auth_mechanism="Managed by Named Credential or inline in Apex",
                data_exchanged="Unknown — see Apex classes that call this domain",
                ui_context="", user_facing_actions=[],
                hidden_logic=[],
                risk_flags=["Wildcard URL may be too permissive" if "*" in url else ""],
                dependencies=[],
                dependents=[],
                confidence=0.95,
                raw_llm_response={},
            )
            results.append(ann)
            self.annotations[name] = ann

        return results

    # ─────────────────────────────────────────────────────────────────
    # INBOUND INTEGRATIONS (Connected Apps, External Services, @RestResource)
    # ─────────────────────────────────────────────────────────────────

    def annotate_inbound_integrations(self, connected_apps: list) -> list[ComponentAnnotation]:
        logger.info(f"Annotating {len(connected_apps)} Connected Apps / Inbound Integrations...")
        results = []

        for app in connected_apps:
            name   = app.get("fullName", app.get("api_name", ""))
            scopes = app.get("oauthConfig", {}).get("scopes", []) if isinstance(app.get("oauthConfig"), dict) else []

            prompt = f"""Analyze this Salesforce Connected App (inbound OAuth integration).

APP NAME: {name}
OAUTH SCOPES: {scopes}
CONFIG: {str(app)[:800]}

Return JSON:
{{
  "purpose": "What external system or application connects INTO Salesforce via this Connected App?",
  "business_process": "What does this external system do with Salesforce data?",
  "trigger_condition": "External system authenticates via OAuth to call Salesforce APIs",
  "objects_read": ["SF objects the external app likely reads based on scopes and name"],
  "objects_written": ["SF objects the external app likely writes"],
  "calls_apex": [],
  "calls_flows": [],
  "callouts": [],
  "integration_direction": "inbound",
  "endpoint_url": "Salesforce REST/SOAP API endpoint",
  "auth_mechanism": "OAuth 2.0 — scopes: {scopes}",
  "data_exchanged": "Infer from app name and scopes what data flows in from the external system",
  "ui_context": "",
  "user_facing_actions": [],
  "hidden_logic": [],
  "risk_flags": [
    "Overly broad scopes (full/api) — should be restricted",
    "No IP restrictions configured",
    "Refresh token policy not configured",
    "No connected app policy for session timeout"
  ],
  "dependencies": ["OAuth policies, profiles/permission sets that allow this app"],
  "confidence": 0.75
}}"""

            ann = self._run_and_build(name, "Inbound Integration / REST API", prompt)
            results.append(ann)

        return results

    # ─────────────────────────────────────────────────────────────────
    # CORE HELPER: run Claude + build ComponentAnnotation
    # ─────────────────────────────────────────────────────────────────

    def _run_and_build(
        self,
        api_name: str,
        category: str,
        prompt: str,
        call_graph_deps: dict = None,
    ) -> ComponentAnnotation:
        try:
            r = self.claude.ask_json(prompt, system=SYSTEM_PROMPT)
        except RuntimeError as e:
            # All retries exhausted (connection error, rate limit, etc.)
            logger.error(f"  ❌ {api_name}: all retries failed — {e}")
            r = {"purpose": f"[Skipped — API unreachable after retries: {str(e)[:80]}]",
                 "confidence": 0.0}
        except Exception as e:
            logger.warning(f"  ⚠️  {api_name}: analysis error — {e}")
            r = {"purpose": f"[Analysis error: {str(e)[:120]}]", "confidence": 0.0}

        deps = list(set(
            r.get("dependencies", []) +
            (call_graph_deps.get("calls", []) if call_graph_deps else [])
        ))

        ann = ComponentAnnotation(
            api_name=api_name,
            metadata_type=category,
            component_category=category,
            purpose=r.get("purpose", ""),
            business_process=r.get("business_process", ""),
            trigger_condition=r.get("trigger_condition", ""),
            objects_read=r.get("objects_read", []),
            objects_written=r.get("objects_written", []),
            calls_apex=r.get("calls_apex", []),
            calls_flows=r.get("calls_flows", []),
            callouts=r.get("callouts", []),
            integration_direction=r.get("integration_direction", ""),
            endpoint_url=r.get("endpoint_url", ""),
            auth_mechanism=r.get("auth_mechanism", ""),
            data_exchanged=r.get("data_exchanged", ""),
            ui_context=r.get("ui_context", ""),
            user_facing_actions=r.get("user_facing_actions", []),
            hidden_logic=r.get("hidden_logic", []),
            risk_flags=[f for f in r.get("risk_flags", []) if f],
            dependencies=deps,
            dependents=call_graph_deps.get("called_by", []) if call_graph_deps else [],
            confidence=float(r.get("confidence", 0.5)),
            raw_llm_response=r,
        )
        self.annotations[api_name] = ann
        return ann

    def _xml_val(self, xml: str, tag: str) -> Optional[str]:
        if not xml:
            return None
        m = re.search(rf"<{tag}>(.*?)</{tag}>", xml, re.DOTALL)
        return m.group(1).strip() if m else None

    def get_all_annotations(self) -> dict[str, ComponentAnnotation]:
        return self.annotations
