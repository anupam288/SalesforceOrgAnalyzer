"""
agents/org_mapper.py

Synthesizes all ComponentAnnotations into a structured OrgProfile —
a complete, domain-agnostic map of the Salesforce org.

Groups components by their metadata category, builds cross-reference
maps, and generates the org overview narrative.
"""
import logging
from dataclasses import dataclass, field
from collections import defaultdict

from agents.semantic_reasoner import ComponentAnnotation, COMPONENT_CATEGORIES

logger = logging.getLogger(__name__)


@dataclass
class CategoryProfile:
    """All components of one metadata category (e.g. all Flows)."""
    category: str
    components: list[ComponentAnnotation] = field(default_factory=list)
    summary: str = ""                        # LLM-generated category summary
    hidden_logic_count: int = 0
    risk_flag_count: int = 0


@dataclass
class IntegrationEntry:
    """One external system or endpoint."""
    name: str
    direction: str           # outbound / inbound / both
    endpoint_url: str
    auth_mechanism: str
    data_exchanged: str
    used_by: list[str]       # component api_names that reference this
    category: str            # inferred: "Payment Gateway", "Identity Verification", etc.


@dataclass
class OrgProfile:
    """Complete picture of the Salesforce org's metadata."""
    overview_narrative: str
    categories: list[CategoryProfile]          # components grouped by type
    total_components: int
    outbound_integrations: list[IntegrationEntry]
    inbound_integrations: list[IntegrationEntry]
    hidden_logic_master: list[dict]            # all hidden rules, cross-referenced
    risk_register: list[dict]                  # all risk flags with severity
    object_usage_map: dict[str, list[str]]     # object → [components that use it]
    callout_map: dict[str, list[str]]          # endpoint → [components that call it]


OVERVIEW_PROMPT = """\
You are writing the technical overview of a Salesforce org for its documentation.

Based on the component breakdown below, write a clear technical narrative (4-6 paragraphs) that:
1. Describes what this Salesforce org appears to do (what business it supports)
2. Summarises the automation landscape (flows, apex, process builder, workflows)
3. Highlights integration footprint (external systems, APIs)
4. Notes any significant UI patterns (LWC, Aura, Visualforce presence)
5. Flags the most important technical observations

COMPONENT BREAKDOWN:
{category_summaries}

TOTAL COMPONENTS: {total}
OUTBOUND INTEGRATIONS: {outbound_count}
INBOUND INTEGRATIONS: {inbound_count}
HIDDEN LOGIC RULES FOUND: {hidden_count}

Write clearly and specifically. Start with "This Salesforce org..."
"""

CATEGORY_SUMMARY_PROMPT = """\
Write a concise technical summary (2-3 paragraphs) of this group of Salesforce {category} components.

COMPONENTS ({count} total):
{component_summaries}

Describe:
1. What these components collectively do
2. Key patterns you observe across them
3. Any notable dependencies or integration points

Be specific about what THIS org's components do, not generic Salesforce theory.
"""


class OrgMapper:
    """
    Assembles a complete OrgProfile from all ComponentAnnotations.
    """

    def __init__(self, claude_client):
        self.claude = claude_client

    def map_org(
        self,
        annotations: dict[str, ComponentAnnotation],
        raw_integrations: list = None,
    ) -> OrgProfile:
        logger.info(f"Mapping org profile from {len(annotations)} annotations...")
        all_anns = list(annotations.values())

        # 1. Group by component category
        cat_groups = self._group_by_category(all_anns)

        # 2. Build CategoryProfile for each
        categories = []
        for cat in COMPONENT_CATEGORIES:
            comps = cat_groups.get(cat, [])
            if not comps:
                continue
            profile = CategoryProfile(
                category=cat,
                components=comps,
                hidden_logic_count=sum(len(c.hidden_logic) for c in comps),
                risk_flag_count=sum(len(c.risk_flags) for c in comps),
            )
            profile.summary = self._generate_category_summary(profile)
            categories.append(profile)

        # 3. Build integration inventories
        outbound = self._build_outbound_integrations(all_anns, raw_integrations or [])
        inbound  = self._build_inbound_integrations(all_anns, raw_integrations or [])

        # 4. Build cross-reference maps
        object_usage_map = self._build_object_map(all_anns)
        callout_map      = self._build_callout_map(all_anns)

        # 5. Collect hidden logic and risks
        hidden_logic_master = self._collect_hidden_logic(all_anns)
        risk_register       = self._collect_risks(all_anns)

        # 6. Generate overview
        overview = self._generate_overview(
            categories, len(all_anns), outbound, inbound, hidden_logic_master
        )

        return OrgProfile(
            overview_narrative=overview,
            categories=categories,
            total_components=len(all_anns),
            outbound_integrations=outbound,
            inbound_integrations=inbound,
            hidden_logic_master=hidden_logic_master,
            risk_register=risk_register,
            object_usage_map=object_usage_map,
            callout_map=callout_map,
        )

    # ─────────────────────────────────────────────────────────────────

    def _group_by_category(self, anns: list[ComponentAnnotation]) -> dict:
        groups = defaultdict(list)
        for ann in anns:
            groups[ann.component_category].append(ann)
        return dict(groups)

    def _generate_category_summary(self, profile: CategoryProfile) -> str:
        if not profile.components:
            return ""
        summaries = [
            f"- {c.api_name}: {c.purpose[:120]}"
            for c in profile.components[:15]
        ]
        if len(profile.components) > 15:
            summaries.append(f"  ...and {len(profile.components) - 15} more")
        prompt = CATEGORY_SUMMARY_PROMPT.format(
            category=profile.category,
            count=len(profile.components),
            component_summaries="\n".join(summaries),
        )
        try:
            return self.claude.ask(prompt)
        except Exception as e:
            logger.warning(f"Category summary failed for {profile.category}: {e}")
            return f"This org contains {len(profile.components)} {profile.category} components."

    def _generate_overview(
        self, categories, total, outbound, inbound, hidden_logic
    ) -> str:
        cat_summaries = "\n".join(
            f"  {p.category}: {len(p.components)} components"
            for p in categories
        )
        prompt = OVERVIEW_PROMPT.format(
            category_summaries=cat_summaries,
            total=total,
            outbound_count=len(outbound),
            inbound_count=len(inbound),
            hidden_count=len(hidden_logic),
        )
        try:
            return self.claude.ask(prompt)
        except Exception as e:
            logger.warning(f"Overview generation failed: {e}")
            return "Org overview could not be generated. See individual category documentation below."

    def _build_outbound_integrations(
        self, anns: list[ComponentAnnotation], raw: list
    ) -> list[IntegrationEntry]:
        seen: dict[str, IntegrationEntry] = {}

        # From annotations
        for ann in anns:
            if ann.integration_direction in ("outbound", "both") and ann.callouts:
                for endpoint in ann.callouts:
                    key = endpoint.strip()
                    if not key:
                        continue
                    if key not in seen:
                        seen[key] = IntegrationEntry(
                            name=ann.api_name if not ann.endpoint_url else key,
                            direction=ann.integration_direction,
                            endpoint_url=ann.endpoint_url or key,
                            auth_mechanism=ann.auth_mechanism,
                            data_exchanged=ann.data_exchanged,
                            used_by=[ann.api_name],
                            category=_categorise_endpoint(key),
                        )
                    else:
                        seen[key].used_by.append(ann.api_name)

        # From raw Named Credentials / Remote Site Settings
        for item in raw:
            name     = item.get("fullName", item.get("api_name", ""))
            endpoint = item.get("endpoint", item.get("url", ""))
            if not name:
                continue
            key = endpoint or name
            if key not in seen:
                seen[key] = IntegrationEntry(
                    name=name,
                    direction="outbound",
                    endpoint_url=endpoint,
                    auth_mechanism=item.get("authProtocol", item.get("principalType", "")),
                    data_exchanged="",
                    used_by=[],
                    category=_categorise_endpoint(name + " " + endpoint),
                )

        return sorted(seen.values(), key=lambda x: x.category)

    def _build_inbound_integrations(
        self, anns: list[ComponentAnnotation], raw: list
    ) -> list[IntegrationEntry]:
        seen: dict[str, IntegrationEntry] = {}
        for ann in anns:
            if ann.integration_direction in ("inbound", "both"):
                key = ann.api_name
                seen[key] = IntegrationEntry(
                    name=ann.api_name,
                    direction="inbound",
                    endpoint_url=ann.endpoint_url,
                    auth_mechanism=ann.auth_mechanism,
                    data_exchanged=ann.data_exchanged,
                    used_by=[],
                    category=_categorise_endpoint(ann.api_name),
                )
        return list(seen.values())

    def _build_object_map(self, anns: list[ComponentAnnotation]) -> dict:
        obj_map: dict[str, list[str]] = defaultdict(list)
        for ann in anns:
            for obj in set(ann.objects_read + ann.objects_written):
                if obj:
                    obj_map[obj].append(ann.api_name)
        return dict(sorted(obj_map.items()))

    def _build_callout_map(self, anns: list[ComponentAnnotation]) -> dict:
        cmap: dict[str, list[str]] = defaultdict(list)
        for ann in anns:
            for ep in ann.callouts:
                if ep:
                    cmap[ep].append(ann.api_name)
        return dict(cmap)

    def _collect_hidden_logic(self, anns: list[ComponentAnnotation]) -> list[dict]:
        rules = []
        for ann in anns:
            for rule in ann.hidden_logic:
                if rule and len(rule.strip()) > 10:
                    rules.append({
                        "rule": rule,
                        "component": ann.api_name,
                        "category": ann.component_category,
                        "business_process": ann.business_process,
                        "confidence": ann.confidence,
                    })
        return sorted(rules, key=lambda x: -x["confidence"])

    def _collect_risks(self, anns: list[ComponentAnnotation]) -> list[dict]:
        risks = []
        for ann in anns:
            for flag in ann.risk_flags:
                if not flag or len(flag.strip()) < 5:
                    continue
                flag_l = flag.lower()
                if any(k in flag_l for k in ["soql in loop", "soql inside loop", "null", "no error handling", "no try", "governor"]):
                    severity = "HIGH"
                elif any(k in flag_l for k in ["hardcoded", "no test", "deprecated", "undocumented", "legacy"]):
                    severity = "MEDIUM"
                else:
                    severity = "LOW"
                risks.append({
                    "flag": flag,
                    "component": ann.api_name,
                    "category": ann.component_category,
                    "severity": severity,
                })
        return sorted(risks, key=lambda x: {"HIGH": 0, "MEDIUM": 1, "LOW": 2}[x["severity"]])


def _categorise_endpoint(name: str) -> str:
    n = name.lower()
    if any(k in n for k in ["payment", "stripe", "razorpay", "paytm", "braintree", "adyen", "neft", "imps"]):
        return "Payment"
    if any(k in n for k in ["identity", "kyc", "aadhaar", "pan", "auth0", "okta", "saml", "ldap"]):
        return "Identity / Auth"
    if any(k in n for k in ["erp", "sap", "oracle", "netsuite", "dynamics", "finacle", "temenos"]):
        return "ERP / Core System"
    if any(k in n for k in ["email", "sendgrid", "mailgun", "ses", "smtp", "postmark"]):
        return "Email"
    if any(k in n for k in ["sms", "twilio", "nexmo", "plivo", "whatsapp", "msg91"]):
        return "SMS / Messaging"
    if any(k in n for k in ["s3", "aws", "azure", "gcp", "blob", "storage", "bucket"]):
        return "Cloud Storage"
    if any(k in n for k in ["slack", "teams", "jira", "zendesk", "servicenow", "freshdesk"]):
        return "Collaboration / Support"
    if any(k in n for k in ["analytics", "tableau", "looker", "powerbi", "segment", "mixpanel"]):
        return "Analytics"
    if any(k in n for k in ["webhook", "callback", "notify", "event", "stream"]):
        return "Event / Webhook"
    if any(k in n for k in ["bureau", "cibil", "experian", "equifax", "crif", "credit"]):
        return "Credit / Bureau"
    if any(k in n for k in ["doc", "sign", "esign", "docusign", "adobe", "hellosign"]):
        return "Document / eSign"
    return "Other"
