"""
parsers/apex_parser.py

Lightweight Apex static analyzer that:
1. Extracts class structure (methods, inner classes)
2. Finds all SOQL queries and their purposes
3. Finds all DML operations (insert/update/delete/upsert)
4. Finds all HTTP callouts and their endpoints
5. Builds a method call graph for dependency analysis
6. Extracts inline comments (often the ONLY existing documentation)

Does NOT require a full Apex AST compiler — uses regex-based extraction
which is sufficient for LLM-assisted documentation purposes.
"""
import re
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ApexMethod:
    name: str
    signature: str
    visibility: str         # public, private, protected, global
    is_static: bool
    return_type: str
    comment: str            # Javadoc/inline comment above the method
    soql_queries: list[str] = field(default_factory=list)
    dml_operations: list[str] = field(default_factory=list)
    callouts: list[str] = field(default_factory=list)
    called_methods: list[str] = field(default_factory=list)


@dataclass
class ParsedApex:
    api_name: str
    class_type: str         # class, trigger, interface, enum
    visibility: str
    is_test: bool
    extends: Optional[str]
    implements: list[str]
    annotations: list[str]  # @IsTest, @AuraEnabled, @RestResource, etc.
    methods: list[ApexMethod]
    all_soql: list[str]
    all_dml: list[str]
    all_callouts: list[str]
    all_comments: list[str] # File-level comments
    trigger_objects: list[str]  # For triggers: which objects + events
    trigger_events: list[str]
    raw_body: str

    def to_summary_dict(self) -> dict:
        return {
            "api_name": self.api_name,
            "class_type": self.class_type,
            "visibility": self.visibility,
            "is_test": self.is_test,
            "annotations": self.annotations,
            "method_count": len(self.methods),
            "method_names": [m.name for m in self.methods[:20]],
            "soql_queries": self.all_soql[:10],
            "dml_operations": self.all_dml[:10],
            "external_callouts": self.all_callouts,
            "trigger_objects": self.trigger_objects,
            "trigger_events": self.trigger_events,
            "key_comments": self.all_comments[:5],
            "source_excerpt": self.raw_body[:2000] if self.raw_body else "",
        }


class ApexParser:
    """
    Regex-based Apex static analyzer.

    Not a full AST parser but captures all structures relevant for
    LLM-assisted business logic documentation.
    """

    # ----------------------------------------------------------------
    # Regex patterns
    # ----------------------------------------------------------------
    # Class/trigger/interface declaration
    RE_CLASS = re.compile(
        r'(?:(?:public|private|protected|global|virtual|abstract|with\s+sharing|without\s+sharing)\s+)*'
        r'(class|interface|enum)\s+(\w+)'
        r'(?:\s+extends\s+(\w+))?'
        r'(?:\s+implements\s+([\w,\s]+))?',
        re.IGNORECASE
    )
    RE_TRIGGER = re.compile(
        r'trigger\s+(\w+)\s+on\s+(\w+)\s*\(([\w\s,]+)\)',
        re.IGNORECASE
    )
    RE_ANNOTATION = re.compile(r'@(\w+)(?:\([^)]*\))?')
    RE_METHOD = re.compile(
        r'(?:(?:public|private|protected|global|virtual|override|static|testMethod)\s+)+'
        r'(\w[\w<>.,\s]*)\s+(\w+)\s*\([^)]*\)',
        re.IGNORECASE
    )
    RE_SOQL = re.compile(r'\[\s*SELECT\s+.+?FROM\s+\w+[^\]]*\]', re.IGNORECASE | re.DOTALL)
    RE_DML = re.compile(
        r'\b(insert|update|delete|upsert|merge|undelete)\s+(\w[\w\[\].]*)',
        re.IGNORECASE
    )
    RE_HTTP_ENDPOINT = re.compile(
        r'''(?:endpoint\s*=\s*|new\s+Http\b|req\.setEndpoint\s*\(\s*)['"]([^'"]+)['"]''',
        re.IGNORECASE
    )
    RE_NAMED_CRED = re.compile(r'callout:(\w+)', re.IGNORECASE)
    RE_INLINE_COMMENT = re.compile(r'//\s*(.+)')
    RE_BLOCK_COMMENT = re.compile(r'/\*\*?\s*(.*?)\s*\*/', re.DOTALL)
    RE_VISIBILITY = re.compile(r'\b(public|private|protected|global)\b', re.IGNORECASE)

    def parse(self, api_name: str, source_code: str) -> ParsedApex:
        """
        Parse Apex source code and return a ParsedApex object.
        """
        if not source_code:
            return ParsedApex(
                api_name=api_name, class_type="unknown", visibility="unknown",
                is_test=False, extends=None, implements=[], annotations=[],
                methods=[], all_soql=[], all_dml=[], all_callouts=[],
                all_comments=[], trigger_objects=[], trigger_events=[],
                raw_body=""
            )

        annotations = self._extract_annotations(source_code)
        annotations_str = " ".join(annotations)
        is_test = "@IsTest" in annotations or "testmethod" in annotations_str.lower() or "istest" in source_code.lower()[:500]

        # Determine if trigger or class
        trigger_match = self.RE_TRIGGER.search(source_code[:500])
        class_match = self.RE_CLASS.search(source_code[:500])

        if trigger_match:
            class_type = "trigger"
            trigger_objects = [trigger_match.group(2)]
            trigger_events = [e.strip() for e in trigger_match.group(3).split(",")]
            visibility = "trigger"
            extends = None
            implements = []
        elif class_match:
            class_type = class_match.group(1).lower()
            trigger_objects = []
            trigger_events = []
            visibility_match = self.RE_VISIBILITY.search(source_code[:200])
            visibility = visibility_match.group(1).lower() if visibility_match else "unknown"
            extends = class_match.group(3)
            implements_str = class_match.group(4) or ""
            implements = [i.strip() for i in implements_str.split(",") if i.strip()]
        else:
            class_type = "unknown"
            trigger_objects = []
            trigger_events = []
            visibility = "unknown"
            extends = None
            implements = []

        all_soql = self._extract_soql(source_code)
        all_dml = self._extract_dml(source_code)
        all_callouts = self._extract_callouts(source_code)
        all_comments = self._extract_meaningful_comments(source_code)
        methods = self._extract_methods(source_code)

        return ParsedApex(
            api_name=api_name,
            class_type=class_type,
            visibility=visibility,
            is_test=is_test,
            extends=extends,
            implements=implements,
            annotations=annotations,
            methods=methods,
            all_soql=all_soql,
            all_dml=all_dml,
            all_callouts=all_callouts,
            all_comments=all_comments,
            trigger_objects=trigger_objects,
            trigger_events=trigger_events,
            raw_body=source_code,
        )

    def _extract_annotations(self, code: str) -> list[str]:
        return list(set(self.RE_ANNOTATION.findall(code[:2000])))

    def _extract_soql(self, code: str) -> list[str]:
        """Extract SOQL queries, cleaned up for readability."""
        queries = self.RE_SOQL.findall(code)
        cleaned = []
        for q in queries:
            # Normalize whitespace
            q_clean = re.sub(r'\s+', ' ', q.strip()).strip("[]")
            cleaned.append(q_clean[:200])  # Truncate very long queries
        return list(set(cleaned))

    def _extract_dml(self, code: str) -> list[str]:
        """Extract DML operations with the target object/variable."""
        ops = self.RE_DML.findall(code)
        return [f"{op[0].upper()} {op[1]}" for op in ops]

    def _extract_callouts(self, code: str) -> list[str]:
        """Extract HTTP callout endpoints and Named Credential references."""
        callouts = []
        # Explicit endpoint strings
        for match in self.RE_HTTP_ENDPOINT.finditer(code):
            callouts.append(match.group(1))
        # Named credential references (callout:NamedCred/path)
        for match in self.RE_NAMED_CRED.finditer(code):
            callouts.append(f"NamedCredential: {match.group(1)}")
        return list(set(callouts))

    def _extract_meaningful_comments(self, code: str) -> list[str]:
        """
        Extract comments that likely contain business logic explanations.
        Filters out boilerplate like 'TODO', '@param', etc.
        """
        comments = []
        SKIP_PATTERNS = re.compile(
            r'^\s*(TODO|FIXME|@param|@return|@throws|@author|@version|@since|Created by|Copyright)',
            re.IGNORECASE
        )

        # Block comments (Javadoc style)
        for match in self.RE_BLOCK_COMMENT.finditer(code):
            text = re.sub(r'\s*\*\s*', ' ', match.group(1)).strip()
            if text and not SKIP_PATTERNS.match(text) and len(text) > 20:
                comments.append(text[:300])

        # Inline comments — only keep ones that look like business explanations
        for match in self.RE_INLINE_COMMENT.finditer(code):
            text = match.group(1).strip()
            if (len(text) > 15
                    and not SKIP_PATTERNS.match(text)
                    and not text.startswith("=")
                    and not text.startswith("-")):
                comments.append(text[:200])

        # Deduplicate and return top 10 most meaningful
        seen = set()
        unique = []
        for c in comments:
            key = c[:50].lower()
            if key not in seen:
                seen.add(key)
                unique.append(c)

        return unique[:10]

    def _extract_methods(self, code: str) -> list[ApexMethod]:
        """Extract method signatures with their surrounding context."""
        methods = []
        for match in self.RE_METHOD.finditer(code):
            return_type = match.group(1).strip()
            name = match.group(2).strip()

            # Skip common false positives
            if name.lower() in ("if", "while", "for", "catch", "switch"):
                continue

            # Get visibility from nearby text
            start = max(0, match.start() - 100)
            nearby = code[start:match.start()]
            vis_match = self.RE_VISIBILITY.search(nearby)
            visibility = vis_match.group(1).lower() if vis_match else "private"
            is_static = "static" in nearby.lower()

            # Get comment above method (up to 5 lines back)
            preceding = code[max(0, match.start() - 500):match.start()]
            comment = self._get_method_comment(preceding)

            # Get method body (rough extraction)
            body_start = code.find("{", match.end())
            method_body = ""
            if body_start != -1:
                method_body = code[body_start:body_start + 1000]

            method = ApexMethod(
                name=name,
                signature=match.group(0)[:100],
                visibility=visibility,
                is_static=is_static,
                return_type=return_type,
                comment=comment,
                soql_queries=self._extract_soql(method_body),
                dml_operations=self._extract_dml(method_body),
                callouts=self._extract_callouts(method_body),
            )
            methods.append(method)

        return methods[:30]  # Cap to avoid noise

    def _get_method_comment(self, preceding_code: str) -> str:
        """Extract the comment immediately above a method declaration."""
        # Look for block comment
        block = list(self.RE_BLOCK_COMMENT.finditer(preceding_code))
        if block:
            last = block[-1]
            if len(preceding_code) - last.end() < 200:  # Close to method
                return re.sub(r'\s*\*\s*', ' ', last.group(1)).strip()[:200]

        # Look for consecutive inline comments
        lines = preceding_code.split("\n")
        comment_lines = []
        for line in reversed(lines[-5:]):
            stripped = line.strip()
            if stripped.startswith("//"):
                comment_lines.insert(0, stripped[2:].strip())
            elif stripped and not stripped.startswith("@"):
                break

        return " ".join(comment_lines)[:200] if comment_lines else ""

    def build_call_graph(self, parsed_classes: list[ParsedApex]) -> dict:
        """
        Build a cross-class method call graph.
        Returns: {class_name: {method_name: [called_class.method, ...]}}

        This is used by the Semantic Reasoner to provide full context
        when analyzing a class — it can see the entire call chain.
        """
        # Map method names to their classes
        method_to_class = {}
        for apex in parsed_classes:
            for method in apex.methods:
                method_to_class[method.name] = apex.api_name

        # Build call graph
        call_graph = {}
        for apex in parsed_classes:
            call_graph[apex.api_name] = {"calls": [], "called_by": []}
            for method in apex.methods:
                for other in parsed_classes:
                    if other.api_name != apex.api_name:
                        if other.api_name in apex.raw_body:
                            call_graph[apex.api_name]["calls"].append(other.api_name)

        # Build reverse edges
        for class_name, data in call_graph.items():
            for called in data["calls"]:
                if called in call_graph:
                    if class_name not in call_graph[called]["called_by"]:
                        call_graph[called]["called_by"].append(class_name)

        return call_graph
