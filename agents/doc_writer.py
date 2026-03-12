"""
agents/doc_writer.py

Generates a complete Markdown documentation site from an OrgProfile.

Output structure (domain-agnostic — works for any Salesforce org):

    output/docs/
    ├── INDEX.md
    ├── 00-org-overview.md
    ├── apex-classes/
    │   ├── overview.md
    │   └── components.md
    ├── apex-triggers/
    ├── flows/
    ├── process-builder/
    ├── workflow-rules/
    ├── validation-rules/
    ├── lwc-components/
    ├── aura-components/
    ├── visualforce-pages/
    ├── approval-processes/
    ├── outbound-integrations.md
    ├── inbound-integrations.md
    ├── hidden-logic-discovered.md     ← highest value
    ├── risk-register.md
    └── object-usage-map.md
"""
import logging
from pathlib import Path
from datetime import datetime

from agents.org_mapper import OrgProfile, CategoryProfile, IntegrationEntry
from agents.semantic_reasoner import ComponentAnnotation

logger = logging.getLogger(__name__)

# Category name → folder slug
CATEGORY_SLUG = {
    "Apex Class":                    "apex-classes",
    "Apex Trigger":                  "apex-triggers",
    "Flow — Record Triggered":       "flows-record-triggered",
    "Flow — Screen Flow":            "flows-screen",
    "Flow — Scheduled":              "flows-scheduled",
    "Flow — AutoLaunched":           "flows-autolaunched",
    "Process Builder":               "process-builder",
    "Workflow Rule":                 "workflow-rules",
    "Validation Rule":               "validation-rules",
    "LWC Component":                 "lwc-components",
    "Aura Component":                "aura-components",
    "Visualforce Page":              "visualforce-pages",
    "Approval Process":              "approval-processes",
    "Outbound Integration":          None,   # written as flat files
    "Inbound Integration / REST API": None,
    "Custom Object":                 "custom-objects",
    "Custom Field":                  "custom-fields",
    "Other":                         "other",
}


class DocWriter:

    def __init__(self, output_dir: str = "output/docs"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.parent.mkdir(parents=True, exist_ok=True)
        self.generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def write_all(self, profile: OrgProfile) -> list[Path]:
        logger.info(f"Writing documentation to {self.output_dir}")
        files = []

        files.append(self._write_overview(profile))

        for cat_profile in profile.categories:
            slug = CATEGORY_SLUG.get(cat_profile.category)
            if slug:
                files.extend(self._write_category(cat_profile, slug))

        files.append(self._write_outbound_integrations(profile.outbound_integrations))
        files.append(self._write_inbound_integrations(profile.inbound_integrations))
        files.append(self._write_hidden_logic(profile.hidden_logic_master))
        files.append(self._write_risk_register(profile.risk_register))
        files.append(self._write_object_usage_map(profile.object_usage_map))
        files.append(self._write_callout_map(profile.callout_map))
        files.insert(0, self._write_index(profile, files))

        logger.info(f"Generated {len(files)} documentation files")
        return files

    # ─────────────────────────────────────────────────────────────────
    # ORG OVERVIEW
    # ─────────────────────────────────────────────────────────────────

    def _write_overview(self, profile: OrgProfile) -> Path:
        path = self.output_dir / "00-org-overview.md"

        # Category breakdown table
        rows = []
        for cat in profile.categories:
            rows.append(
                f"| {cat.category} | {len(cat.components)} | "
                f"{cat.hidden_logic_count} | {cat.risk_flag_count} |"
            )

        content = f"""# Salesforce Org — Technical Overview

> **Generated:** {self.generated_at}
> **Total Components Analyzed:** {profile.total_components}
> **Outbound Integrations:** {len(profile.outbound_integrations)}
> **Inbound Integrations:** {len(profile.inbound_integrations)}
> **Hidden Logic Rules Found:** {len(profile.hidden_logic_master)}
> **Risk Flags:** {len(profile.risk_register)}

---

## What This Org Does

{profile.overview_narrative}

---

## Component Breakdown

| Category | Count | Hidden Logic | Risk Flags |
|----------|-------|-------------|------------|
{chr(10).join(rows)}

---

## Key Documents

| Document | What's Inside |
|----------|---------------|
| [Hidden Logic Discovered](hidden-logic-discovered.md) | ⚡ Business rules found in code — start here |
| [Outbound Integrations](outbound-integrations.md) | Every external system this org calls |
| [Inbound Integrations](inbound-integrations.md) | External systems that call into this org |
| [Risk Register](risk-register.md) | Technical debt by severity |
| [Object Usage Map](object-usage-map.md) | Which components touch which objects |
| [Callout Map](callout-map.md) | Which components call which endpoints |
"""
        path.write_text(content, encoding="utf-8")
        logger.info(f"  Wrote: {path.name}")
        return path

    # ─────────────────────────────────────────────────────────────────
    # CATEGORY PAGES (one folder per component type)
    # ─────────────────────────────────────────────────────────────────

    def _write_category(self, cat: CategoryProfile, slug: str) -> list[Path]:
        cat_dir = self.output_dir / slug
        cat_dir.mkdir(exist_ok=True)
        files = []
        files.append(self._write_category_overview(cat, cat_dir))
        files.append(self._write_category_components(cat, cat_dir))
        return files

    def _write_category_overview(self, cat: CategoryProfile, cat_dir: Path) -> Path:
        path = cat_dir / "overview.md"

        # Build component summary table
        rows = []
        for c in cat.components:
            purpose = c.purpose[:90] + "…" if len(c.purpose) > 90 else c.purpose
            trigger = c.trigger_condition[:60] + "…" if len(c.trigger_condition) > 60 else c.trigger_condition
            rows.append(f"| [`{c.api_name}`](components.md#{_anchor(c.api_name)}) | {purpose} | {trigger} |")

        # Hidden logic summary
        all_logic = [r for c in cat.components for r in c.hidden_logic]
        logic_section = ""
        if all_logic:
            logic_section = "\n## Hidden Logic Found in This Category\n\n" + "\n".join(
                f"> ⚡ {r}" for r in all_logic[:8]
            )
            if len(all_logic) > 8:
                logic_section += f"\n\n_...and {len(all_logic) - 8} more. See [Hidden Logic](../hidden-logic-discovered.md)._"

        # Integrations
        all_callouts = list(set(ep for c in cat.components for ep in c.callouts if ep))
        callout_section = ""
        if all_callouts:
            callout_section = "\n## External Endpoints Called\n\n" + "\n".join(
                f"- `{ep}`" for ep in all_callouts[:10]
            )

        content = f"""# {cat.category} — Overview

> **{len(cat.components)} components** | **{cat.hidden_logic_count} hidden logic rules** | **{cat.risk_flag_count} risk flags**

---

## What These Components Do

{cat.summary or f'This org contains {len(cat.components)} {cat.category} components.'}

{logic_section}
{callout_section}

---

## All Components

| Component | Purpose | Trigger / When |
|-----------|---------|----------------|
{chr(10).join(rows)}
"""
        path.write_text(content, encoding="utf-8")
        return path

    def _write_category_components(self, cat: CategoryProfile, cat_dir: Path) -> Path:
        path = cat_dir / "components.md"
        sections = [f"# {cat.category} — Component Details\n"]

        for comp in cat.components:
            sections.append(self._render_component(comp))

        path.write_text("\n".join(sections), encoding="utf-8")
        return path

    def _render_component(self, c: ComponentAnnotation) -> str:
        """Render one component as a detailed Markdown section."""

        def _list(items, label=""):
            if not items:
                return ""
            bullet = "\n".join(f"  - `{i}`" for i in items if i)
            return f"\n**{label}**\n{bullet}\n" if label else bullet

        def _flag_list(items, icon="⚡"):
            if not items:
                return ""
            return "\n".join(f"> {icon} {r}" for r in items if r)

        conf_note = f" _(confidence: {c.confidence:.0%})_" if c.confidence < 0.7 else ""

        # Build each section only if non-empty
        objects_section = ""
        if c.objects_read or c.objects_written:
            read_str  = ", ".join(f"`{o}`" for o in c.objects_read)  if c.objects_read  else "—"
            write_str = ", ".join(f"`{o}`" for o in c.objects_written) if c.objects_written else "—"
            objects_section = f"\n**Objects Read:** {read_str}  \n**Objects Written:** {write_str}\n"

        calls_section = ""
        if c.calls_apex or c.calls_flows:
            calls_section = _list(c.calls_apex, "Calls Apex") + _list(c.calls_flows, "Calls Flows")

        integration_section = ""
        if c.callouts or c.integration_direction:
            direction = f"**Direction:** {c.integration_direction}  \n" if c.integration_direction else ""
            endpoint  = f"**Endpoint:** `{c.endpoint_url}`  \n" if c.endpoint_url else ""
            auth      = f"**Auth:** {c.auth_mechanism}  \n" if c.auth_mechanism else ""
            data      = f"**Data:** {c.data_exchanged}  \n" if c.data_exchanged else ""
            callout_list = _list(c.callouts, "Callout URLs")
            integration_section = f"\n**Integration Details:**  \n{direction}{endpoint}{auth}{data}{callout_list}\n"

        ui_section = ""
        if c.ui_context or c.user_facing_actions:
            ctx     = f"**UI Context:** {c.ui_context}  \n" if c.ui_context else ""
            actions = _list(c.user_facing_actions, "User Actions") if c.user_facing_actions else ""
            ui_section = f"\n{ctx}{actions}\n"

        hidden_section = ""
        if c.hidden_logic:
            hidden_section = f"\n**Hidden Logic:**\n{_flag_list(c.hidden_logic, '⚡')}\n"

        risk_section = ""
        if c.risk_flags:
            risk_section = f"\n**Risk Flags:**\n{_flag_list(c.risk_flags, '⚠️')}\n"

        deps_section = ""
        if c.dependencies:
            deps_section = "\n**Dependencies:** " + ", ".join(f"`{d}`" for d in c.dependencies[:8]) + "\n"

        dependents_section = ""
        if c.dependents:
            dependents_section = "\n**Called By:** " + ", ".join(f"`{d}`" for d in c.dependents[:8]) + "\n"

        return f"""
---

## `{c.api_name}`{conf_note} {{#{_anchor(c.api_name)}}}

**Category:** {c.component_category}  
**Business Process:** {c.business_process or '—'}  
**Trigger / When:** {c.trigger_condition or '—'}

**Purpose:**
{c.purpose or '—'}
{objects_section}{calls_section}{integration_section}{ui_section}{hidden_section}{risk_section}{deps_section}{dependents_section}"""

    # ─────────────────────────────────────────────────────────────────
    # INTEGRATION PAGES
    # ─────────────────────────────────────────────────────────────────

    def _write_outbound_integrations(self, integrations: list[IntegrationEntry]) -> Path:
        path = self.output_dir / "outbound-integrations.md"

        by_cat: dict[str, list] = {}
        for i in integrations:
            by_cat.setdefault(i.category, []).append(i)

        sections = [f"""# Outbound Integrations

> **Generated:** {self.generated_at}
> **Total:** {len(integrations)} outbound endpoints

This org makes HTTP calls to these external systems.  
Discovered from: Named Credentials, Remote Site Settings, Apex callout code.

---
"""]
        for cat, items in sorted(by_cat.items()):
            sections.append(f"\n## {cat}\n")
            for i in items:
                used_by = ", ".join(f"`{u}`" for u in i.used_by[:5]) if i.used_by else "—"
                sections.append(f"""
### `{i.name}`

| Field | Value |
|-------|-------|
| Endpoint | `{i.endpoint_url or '—'}` |
| Auth | {i.auth_mechanism or '—'} |
| Data Exchanged | {i.data_exchanged or '—'} |
| Used By | {used_by} |
""")

        path.write_text("\n".join(sections), encoding="utf-8")
        logger.info(f"  Wrote: {path.name}")
        return path

    def _write_inbound_integrations(self, integrations: list[IntegrationEntry]) -> Path:
        path = self.output_dir / "inbound-integrations.md"

        sections = [f"""# Inbound Integrations & REST APIs

> **Generated:** {self.generated_at}
> **Total:** {len(integrations)} inbound endpoints

External systems that call INTO this Salesforce org via REST/SOAP APIs or Connected Apps.

---
"""]
        for i in integrations:
            sections.append(f"""
## `{i.name}`

| Field | Value |
|-------|-------|
| Direction | {i.direction} |
| Endpoint | `{i.endpoint_url or 'Salesforce REST API'}` |
| Auth | {i.auth_mechanism or '—'} |
| Data Exchanged | {i.data_exchanged or '—'} |
""")

        path.write_text("\n".join(sections), encoding="utf-8")
        logger.info(f"  Wrote: {path.name}")
        return path

    # ─────────────────────────────────────────────────────────────────
    # HIDDEN LOGIC  — most valuable output
    # ─────────────────────────────────────────────────────────────────

    def _write_hidden_logic(self, rules: list[dict]) -> Path:
        path = self.output_dir / "hidden-logic-discovered.md"

        # Group by business process
        by_process: dict[str, list] = {}
        for r in rules:
            proc = r.get("business_process") or "Uncategorised"
            by_process.setdefault(proc, []).append(r)

        content = f"""# Hidden Logic Discovered

> **Generated:** {self.generated_at}
> **Total rules found:** {len(rules)}

Business rules and logic embedded in code, flows, and configuration that were
**never explicitly documented** — surfaced by AI analysis of the org's metadata.

> **How to use this:**
> 1. Review each rule with your business and technical teams
> 2. Confirm or correct the AI's interpretation
> 3. Move confirmed rules to your official documentation
> 4. Flag any hardcoded thresholds for migration to Custom Settings / Custom Metadata

⚡ = high confidence finding (>70%)  🔍 = inferred (≤70%)

---

"""
        for process, process_rules in sorted(by_process.items()):
            content += f"## {process}\n\n"
            for r in process_rules:
                icon = "⚡" if r.get("confidence", 0) > 0.7 else "🔍"
                content += (
                    f"{icon} **{r['rule']}**  \n"
                    f"   *Found in: `{r['component']}` — {r['category']}*\n\n"
                )

        path.write_text(content, encoding="utf-8")
        logger.info(f"  Wrote: {path.name} ({len(rules)} rules)")
        return path

    # ─────────────────────────────────────────────────────────────────
    # RISK REGISTER
    # ─────────────────────────────────────────────────────────────────

    def _write_risk_register(self, risks: list[dict]) -> Path:
        path = self.output_dir / "risk-register.md"

        high   = [r for r in risks if r["severity"] == "HIGH"]
        medium = [r for r in risks if r["severity"] == "MEDIUM"]
        low    = [r for r in risks if r["severity"] == "LOW"]

        def _risk_rows(items):
            return "\n".join(
                f"| `{r['component']}` | {r['category']} | {r['flag']} |"
                for r in items
            )

        content = f"""# Technical Risk Register

> **Generated:** {self.generated_at}
> 🔴 HIGH: {len(high)} | 🟡 MEDIUM: {len(medium)} | 🟢 LOW: {len(low)}

---

## 🔴 HIGH — Fix Before Production

| Component | Type | Risk |
|-----------|------|------|
{_risk_rows(high) or "| — | — | None found |"}

---

## 🟡 MEDIUM — Schedule for Next Sprint

| Component | Type | Risk |
|-----------|------|------|
{_risk_rows(medium) or "| — | — | None found |"}

---

## 🟢 LOW — Track and Address

| Component | Type | Risk |
|-----------|------|------|
{_risk_rows(low[:30]) or "| — | — | None found |"}
"""
        path.write_text(content, encoding="utf-8")
        logger.info(f"  Wrote: {path.name}")
        return path

    # ─────────────────────────────────────────────────────────────────
    # CROSS-REFERENCE MAPS
    # ─────────────────────────────────────────────────────────────────

    def _write_object_usage_map(self, obj_map: dict[str, list[str]]) -> Path:
        path = self.output_dir / "object-usage-map.md"

        rows = "\n".join(
            f"| `{obj}` | {len(comps)} | {', '.join(f'`{c}`' for c in comps[:5])}"
            + (" ..." if len(comps) > 5 else "") + " |"
            for obj, comps in sorted(obj_map.items(), key=lambda x: -len(x[1]))
        )

        content = f"""# Object Usage Map

> **Generated:** {self.generated_at}

Shows which Salesforce objects are read or written by which components.
Use this to understand data flow and assess impact of schema changes.

| Object | Component Count | Components |
|--------|-----------------|------------|
{rows or "| — | 0 | — |"}
"""
        path.write_text(content, encoding="utf-8")
        logger.info(f"  Wrote: {path.name}")
        return path

    def _write_callout_map(self, callout_map: dict[str, list[str]]) -> Path:
        path = self.output_dir / "callout-map.md"

        rows = "\n".join(
            f"| `{ep}` | {', '.join(f'`{c}`' for c in comps[:5])}"
            + (" ..." if len(comps) > 5 else "") + " |"
            for ep, comps in sorted(callout_map.items())
        )

        content = f"""# Callout Map

> **Generated:** {self.generated_at}

Shows which external endpoints are called by which Apex classes or Flows.
Use this to assess the blast radius of external API changes.

| Endpoint / Named Credential | Called By |
|-----------------------------|-----------|
{rows or "| — | No callouts found |"}
"""
        path.write_text(content, encoding="utf-8")
        logger.info(f"  Wrote: {path.name}")
        return path

    # ─────────────────────────────────────────────────────────────────
    # INDEX
    # ─────────────────────────────────────────────────────────────────

    def _write_index(self, profile: OrgProfile, files: list[Path]) -> Path:
        path = self.output_dir / "INDEX.md"

        cat_links = "\n".join(
            f"- [{cat.category}]({CATEGORY_SLUG.get(cat.category, 'other')}/overview.md)"
            f" — {len(cat.components)} components"
            for cat in profile.categories
            if CATEGORY_SLUG.get(cat.category)
        )

        content = f"""# Salesforce Org Documentation

> Auto-generated by the Salesforce Org Intelligence Agent
> Generated: {self.generated_at}
> Components Analyzed: {profile.total_components}

## Start Here

| Document | Description |
|----------|-------------|
| [Org Overview](00-org-overview.md) | What this org does and full component breakdown |
| [Hidden Logic Discovered](hidden-logic-discovered.md) | ⚡ Business rules found in code — highest value |
| [Outbound Integrations](outbound-integrations.md) | Every external API this org calls |
| [Inbound Integrations](inbound-integrations.md) | External systems that call into this org |
| [Risk Register](risk-register.md) | Technical debt by severity |
| [Object Usage Map](object-usage-map.md) | Which components touch which objects |
| [Callout Map](callout-map.md) | Which components call which endpoints |

## Components by Type

{cat_links}
"""
        path.write_text(content, encoding="utf-8")
        logger.info(f"  Wrote: {path.name}")
        return path


def _anchor(name: str) -> str:
    """Convert a component name to a valid Markdown anchor."""
    return name.lower().replace("_", "-").replace(" ", "-").replace(".", "-")
