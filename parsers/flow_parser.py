"""
parsers/flow_parser.py

Converts Salesforce Flow metadata XML into structured, LLM-readable pseudocode.

The raw XML is dense and self-referential. This parser:
1. Extracts all elements (decisions, assignments, actions, sub-flows, etc.)
2. Builds a directed graph of execution paths
3. Generates human-readable pseudocode for each path
4. Returns a structured dict the LLM can reason about

Example output pseudocode:
    TRIGGER: When LoanApplication__c [Status__c] changes
    ENTRY: Status__c was NOT 'Approved' AND is now 'Approved'

    DECISION: Check Loan Amount
      IF LoanAmount__c > 10000000 (1 Crore):
        → ACTION: Apex - SendToCreditCommittee(loanId)
        → UPDATE: Stage__c = 'Credit Committee Review'
        → NOTIFY: AssignedOfficer@email
      ELSE:
        → UPDATE: Stage__c = 'Relationship Manager Review'
        → CREATE: Task for AssignedUser
"""

import xml.etree.ElementTree as ET
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

SF_NS = "http://soap.sforce.com/2006/04/metadata"


def _tag(name: str) -> str:
    return f"{{{SF_NS}}}{name}"


@dataclass
class FlowNode:
    name: str
    node_type: str  # decision, assignment, recordUpdate, recordCreate, actionCall, subflow, etc.
    label: str = ""
    conditions: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    true_connector: Optional[str] = None
    false_connector: Optional[str] = None
    next_connector: Optional[str] = None


@dataclass
class ParsedFlow:
    api_name: str
    label: str
    trigger_type: str           # RecordBeforeSave, RecordAfterSave, Scheduled, etc.
    trigger_object: Optional[str]
    entry_conditions: list[str]
    nodes: dict[str, FlowNode]
    start_element: Optional[str]
    pseudocode: str             # Human-readable execution paths
    raw_stats: dict             # counts of element types


class FlowParser:
    """
    Parses Salesforce Flow XML into structured pseudocode.
    """

    def parse(self, api_name: str, xml_content: str) -> ParsedFlow:
        """
        Parse a flow XML string into a structured ParsedFlow object.
        """
        if not xml_content or not xml_content.strip():
            return ParsedFlow(
                api_name=api_name, label=api_name,
                trigger_type="Unknown", trigger_object=None,
                entry_conditions=[], nodes={}, start_element=None,
                pseudocode="[No source available]", raw_stats={}
            )

        try:
            root = ET.fromstring(xml_content)
        except ET.ParseError as e:
            logger.warning(f"XML parse error for flow {api_name}: {e}")
            return ParsedFlow(
                api_name=api_name, label=api_name,
                trigger_type="Unknown", trigger_object=None,
                entry_conditions=[], nodes={}, start_element=None,
                pseudocode=f"[XML parse error: {e}]", raw_stats={}
            )

        label = self._text(root, "label") or api_name
        trigger_type = self._detect_trigger_type(root)
        trigger_object = self._detect_trigger_object(root)
        entry_conditions = self._extract_entry_conditions(root)
        nodes = self._extract_all_nodes(root)
        start_element = self._find_start_element(root)
        pseudocode = self._generate_pseudocode(
            root, nodes, start_element, trigger_type, trigger_object, entry_conditions
        )
        raw_stats = self._count_elements(root)

        return ParsedFlow(
            api_name=api_name,
            label=label,
            trigger_type=trigger_type,
            trigger_object=trigger_object,
            entry_conditions=entry_conditions,
            nodes=nodes,
            start_element=start_element,
            pseudocode=pseudocode,
            raw_stats=raw_stats,
        )

    def _text(self, element, tag: str) -> str:
        child = element.find(_tag(tag))
        return child.text.strip() if child is not None and child.text else ""

    def _detect_trigger_type(self, root) -> str:
        # Check processType element
        process_type = self._text(root, "processType")
        if process_type:
            return process_type

        # Check triggerType in start element
        start = root.find(_tag("start"))
        if start is not None:
            trigger = self._text(start, "triggerType")
            if trigger:
                return trigger

        return "AutoLaunchedFlow"

    def _detect_trigger_object(self, root) -> Optional[str]:
        start = root.find(_tag("start"))
        if start is not None:
            obj = self._text(start, "object")
            return obj or None

        # Check processMetadataValues for Process Builder
        for pmv in root.findall(_tag("processMetadataValues")):
            name = self._text(pmv, "name")
            if name == "ObjectType":
                value = pmv.find(_tag("value"))
                if value is not None:
                    return self._text(value, "stringValue")

        return None

    def _extract_entry_conditions(self, root) -> list[str]:
        conditions = []

        # Record-triggered flow entry conditions
        start = root.find(_tag("start"))
        if start is not None:
            filter_logic = self._text(start, "filterLogic")
            for f in start.findall(_tag("filters")):
                field = self._text(f, "field")
                op = self._text(f, "operator")
                value_el = f.find(_tag("value"))
                value = ""
                if value_el is not None:
                    for vtype in ["stringValue", "numberValue", "booleanValue", "dateValue"]:
                        v = self._text(value_el, vtype)
                        if v:
                            value = v
                            break
                if field:
                    conditions.append(f"{field} {op} '{value}'")

            # Also check triggerType for WHEN the flow fires
            rec_trigger = self._text(start, "recordTriggerType")
            if rec_trigger:
                conditions.insert(0, f"Trigger: {rec_trigger}")

        return conditions

    def _extract_all_nodes(self, root) -> dict[str, FlowNode]:
        nodes = {}

        # Element types we care about
        type_map = {
            "decisions": "DECISION",
            "assignments": "ASSIGNMENT",
            "recordLookups": "RECORD_LOOKUP",
            "recordUpdates": "RECORD_UPDATE",
            "recordCreates": "RECORD_CREATE",
            "recordDeletes": "RECORD_DELETE",
            "actionCalls": "ACTION_CALL",
            "subflows": "SUBFLOW",
            "screens": "SCREEN",
            "loops": "LOOP",
            "waits": "WAIT",
            "customErrors": "CUSTOM_ERROR",
        }

        for xml_type, node_type in type_map.items():
            for elem in root.findall(_tag(xml_type)):
                name = self._text(elem, "name")
                label = self._text(elem, "label")
                if not name:
                    continue

                conditions = self._extract_node_conditions(elem, node_type)
                actions = self._extract_node_actions(elem, node_type)
                true_conn, false_conn, next_conn = self._extract_connectors(elem)

                nodes[name] = FlowNode(
                    name=name,
                    node_type=node_type,
                    label=label,
                    conditions=conditions,
                    actions=actions,
                    true_connector=true_conn,
                    false_connector=false_conn,
                    next_connector=next_conn,
                )

        return nodes

    def _extract_node_conditions(self, elem, node_type: str) -> list[str]:
        conditions = []

        if node_type == "DECISION":
            for rule in elem.findall(_tag("rules")):
                rule_label = self._text(rule, "label")
                rule_conditions = []
                for cond in rule.findall(_tag("conditions")):
                    left = self._text(cond, "leftValueReference")
                    op = self._text(cond, "operator")
                    right_el = cond.find(_tag("rightValue"))
                    right = ""
                    if right_el is not None:
                        for vtype in ["stringValue", "numberValue", "booleanValue", "elementReference"]:
                            v = self._text(right_el, vtype)
                            if v:
                                right = v
                                break
                    if left:
                        rule_conditions.append(f"{left} {op} '{right}'")
                if rule_conditions:
                    conditions.append(f"WHEN {rule_label}: " + " AND ".join(rule_conditions))

        return conditions

    def _extract_node_actions(self, elem, node_type: str) -> list[str]:
        actions = []

        if node_type == "ACTION_CALL":
            action_name = self._text(elem, "actionName")
            action_type = self._text(elem, "actionType")
            label = self._text(elem, "label")
            actions.append(f"CALL {action_type}: {action_name} [{label}]")
            # Capture input parameters
            for param in elem.findall(_tag("inputParameters")):
                pname = self._text(param, "name")
                pval_el = param.find(_tag("value"))
                pval = ""
                if pval_el is not None:
                    for vtype in ["stringValue", "elementReference", "numberValue"]:
                        v = self._text(pval_el, vtype)
                        if v:
                            pval = v
                            break
                if pname and pval:
                    actions.append(f"  INPUT: {pname} = {pval}")

        elif node_type == "SUBFLOW":
            flow_name = self._text(elem, "flowName")
            actions.append(f"CALL SUBFLOW: {flow_name}")

        elif node_type == "RECORD_UPDATE":
            obj = self._text(elem, "object")
            if not obj:
                record_ref = self._text(elem, "inputReference")
                obj = record_ref or "record"
            actions.append(f"UPDATE {obj}:")
            for field_val in elem.findall(_tag("inputAssignments")):
                fname = self._text(field_val, "field")
                fval_el = field_val.find(_tag("value"))
                fval = ""
                if fval_el is not None:
                    for vtype in ["stringValue", "elementReference", "numberValue", "booleanValue"]:
                        v = self._text(fval_el, vtype)
                        if v:
                            fval = v
                            break
                if fname:
                    actions.append(f"  SET {fname} = '{fval}'")

        elif node_type == "RECORD_CREATE":
            obj = self._text(elem, "object")
            actions.append(f"CREATE {obj or 'record'}")

        elif node_type == "ASSIGNMENT":
            for item in elem.findall(_tag("assignmentItems")):
                ref = self._text(item, "assignToReference")
                op = self._text(item, "operator")
                val_el = item.find(_tag("value"))
                val = ""
                if val_el is not None:
                    for vtype in ["stringValue", "elementReference", "numberValue"]:
                        v = self._text(val_el, vtype)
                        if v:
                            val = v
                            break
                if ref:
                    actions.append(f"SET {ref} {op} '{val}'")

        elif node_type == "CUSTOM_ERROR":
            msg = self._text(elem, "description")
            actions.append(f"THROW ERROR: {msg}")

        return actions

    def _extract_connectors(self, elem) -> tuple[Optional[str], Optional[str], Optional[str]]:
        true_conn = false_conn = next_conn = None

        # For decisions — rules have their own connectors
        # For other elements
        connector = elem.find(_tag("connector"))
        if connector is not None:
            next_conn = self._text(connector, "targetReference")

        fault = elem.find(_tag("faultConnector"))
        if fault is not None:
            false_conn = self._text(fault, "targetReference")

        return true_conn, false_conn, next_conn

    def _find_start_element(self, root) -> Optional[str]:
        start = root.find(_tag("start"))
        if start is not None:
            connector = start.find(_tag("connector"))
            if connector is not None:
                return self._text(connector, "targetReference")
        return None

    def _generate_pseudocode(
        self, root, nodes, start_element, trigger_type, trigger_object, entry_conditions
    ) -> str:
        lines = []

        # Header
        if trigger_object:
            lines.append(f"TRIGGER ON: {trigger_object} [{trigger_type}]")
        else:
            lines.append(f"TYPE: {trigger_type}")

        if entry_conditions:
            lines.append(f"ENTRY CONDITIONS:")
            for cond in entry_conditions:
                lines.append(f"  {cond}")

        lines.append("")
        lines.append("EXECUTION PATH:")

        # Walk the flow graph from start
        visited = set()
        queue = [start_element] if start_element else []

        # Limit traversal depth to prevent infinite loops
        max_nodes = 50
        count = 0

        while queue and count < max_nodes:
            current = queue.pop(0)
            if not current or current in visited:
                continue
            visited.add(current)
            count += 1

            node = nodes.get(current)
            if not node:
                continue

            indent = "  "
            lines.append(f"{indent}[{node.node_type}] {node.label or node.name}")

            for cond in node.conditions:
                lines.append(f"{indent}  {cond}")
            for action in node.actions:
                lines.append(f"{indent}  {action}")

            if node.next_connector:
                queue.append(node.next_connector)

        # For decision nodes, also capture the rules branches
        for elem in root.findall(_tag("decisions")):
            dname = self._text(elem, "name")
            if dname not in visited:
                continue
            for rule in elem.findall(_tag("rules")):
                rlabel = self._text(rule, "label")
                connector = rule.find(_tag("connector"))
                if connector is not None:
                    target = self._text(connector, "targetReference")
                    if target:
                        lines.append(f"    → IF '{rlabel}' THEN GOTO {target}")
                        queue.append(target)

        if count >= max_nodes:
            lines.append(f"  ... [truncated after {max_nodes} nodes for brevity]")

        return "\n".join(lines)

    def _count_elements(self, root) -> dict:
        types = ["decisions", "assignments", "recordUpdates", "recordCreates",
                 "recordLookups", "actionCalls", "subflows", "screens", "loops"]
        return {t: len(root.findall(_tag(t))) for t in types}

    def to_summary_dict(self, parsed: ParsedFlow) -> dict:
        """Convert ParsedFlow to a dict suitable for LLM analysis."""
        return {
            "api_name": parsed.api_name,
            "label": parsed.label,
            "trigger_type": parsed.trigger_type,
            "trigger_object": parsed.trigger_object,
            "entry_conditions": parsed.entry_conditions,
            "pseudocode": parsed.pseudocode,
            "element_counts": parsed.raw_stats,
            "node_count": len(parsed.nodes),
        }
