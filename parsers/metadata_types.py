"""
parsers/metadata_types.py

Complete list of Salesforce metadata types organized by category.
Used by the crawler to know what to fetch.
"""
from dataclasses import dataclass


@dataclass
class MetadataTypeConfig:
    api_name: str           # Salesforce metadata type name
    category: str           # Logical grouping
    priority: int           # 1=critical, 2=important, 3=nice-to-have
    use_tooling: bool       # True = use Tooling API (for source code)
    description: str        # What this type contains


# Complete Salesforce metadata type registry
METADATA_TYPES: list[MetadataTypeConfig] = [

    # ── APEX CODE ─────────────────────────────────────────────────────
    MetadataTypeConfig("ApexClass",         "apex",         1, True,  "Apex classes — business logic, services, utilities, handlers"),
    MetadataTypeConfig("ApexTrigger",       "apex",         1, True,  "Apex triggers — event-driven logic on object changes"),
    MetadataTypeConfig("ApexComponent",     "apex",         3, True,  "Visualforce components using Apex controllers"),
    MetadataTypeConfig("ApexPage",          "apex",         2, True,  "Visualforce pages — legacy UI"),
    MetadataTypeConfig("ApexTestSuite",     "apex",         3, False, "Test suites grouping test classes"),

    # ── AUTOMATION & FLOWS ────────────────────────────────────────────
    MetadataTypeConfig("Flow",              "automation",   1, False, "Screen flows, record-triggered flows, scheduled flows"),
    MetadataTypeConfig("WorkflowRule",      "automation",   1, False, "Legacy workflow rules — field updates, alerts, tasks"),
    MetadataTypeConfig("WorkflowFieldUpdate", "automation", 1, False, "Field values set by workflow rules"),
    MetadataTypeConfig("WorkflowAlert",     "automation",   2, False, "Email alerts triggered by workflow"),
    MetadataTypeConfig("WorkflowTask",      "automation",   2, False, "Tasks created by workflow rules"),
    MetadataTypeConfig("WorkflowOutboundMessage", "automation", 2, False, "Outbound messages from workflow"),
    MetadataTypeConfig("AutoResponseRule",  "automation",   3, False, "Auto-response rules for cases/leads"),
    MetadataTypeConfig("EscalationRule",    "automation",   2, False, "Case escalation rules and SLA logic"),
    MetadataTypeConfig("AssignmentRule",    "automation",   2, False, "Lead/case assignment rules"),

    # ── OBJECTS & SCHEMA ──────────────────────────────────────────────
    MetadataTypeConfig("CustomObject",      "schema",       1, False, "Custom objects — the loan data model"),
    MetadataTypeConfig("CustomField",       "schema",       1, False, "Custom fields on standard and custom objects"),
    MetadataTypeConfig("RecordType",        "schema",       1, False, "Record types — different loan product subtypes"),
    MetadataTypeConfig("CustomRelationship","schema",       2, False, "Relationships between objects"),
    MetadataTypeConfig("CustomIndex",       "schema",       3, False, "Custom database indexes"),
    MetadataTypeConfig("CustomTab",         "schema",       3, False, "Custom navigation tabs"),
    MetadataTypeConfig("ExternalDataSource","schema",       2, False, "External data source connections"),
    MetadataTypeConfig("ExternalObject",    "schema",       2, False, "External objects via OData/custom adapters"),

    # ── VALIDATION & RULES ────────────────────────────────────────────
    MetadataTypeConfig("ValidationRule",    "rules",        1, False, "Validation rules — data entry constraints and business rules"),
    MetadataTypeConfig("DuplicateRule",     "rules",        2, False, "Duplicate detection rules"),
    MetadataTypeConfig("MatchingRule",      "rules",        2, False, "Record matching criteria"),
    MetadataTypeConfig("BusinessProcess",   "rules",        2, False, "Picklist value subsets per record type"),

    # ── UI COMPONENTS ─────────────────────────────────────────────────
    MetadataTypeConfig("LightningComponentBundle", "ui",   1, False, "LWC components — modern UI"),
    MetadataTypeConfig("AuraDefinitionBundle",      "ui",  1, False, "Aura components — Lightning components"),
    MetadataTypeConfig("FlexiPage",         "ui",          1, False, "Lightning App Builder pages (Lightning pages)"),
    MetadataTypeConfig("Layout",            "ui",          2, False, "Page layouts — field organization per record type"),
    MetadataTypeConfig("CompactLayout",     "ui",          3, False, "Compact layouts for mobile/highlights panel"),
    MetadataTypeConfig("ListView",          "ui",          3, False, "List view definitions"),
    MetadataTypeConfig("CustomPageWebLink", "ui",          3, False, "Custom buttons and links"),
    MetadataTypeConfig("HomePageComponent", "ui",          3, False, "Classic home page components"),
    MetadataTypeConfig("HomePageLayout",    "ui",          3, False, "Classic home page layouts"),

    # ── APPROVALS ─────────────────────────────────────────────────────
    MetadataTypeConfig("ApprovalProcess",   "approvals",   1, False, "Approval processes — credit authority hierarchy"),
    MetadataTypeConfig("Queue",             "approvals",   2, False, "Queues for routing records to teams"),
    MetadataTypeConfig("Group",             "approvals",   3, False, "Public groups used in sharing and routing"),

    # ── INTEGRATIONS ──────────────────────────────────────────────────
    MetadataTypeConfig("NamedCredential",   "integrations",1, False, "Named credentials — external endpoint configs"),
    MetadataTypeConfig("RemoteSiteSetting", "integrations",1, False, "Whitelisted external URLs for callouts"),
    MetadataTypeConfig("ConnectedApp",      "integrations",2, False, "Connected apps — OAuth integrations"),
    MetadataTypeConfig("ExternalService",   "integrations",2, False, "External service registrations (OpenAPI)"),
    MetadataTypeConfig("CspTrustedSite",    "integrations",3, False, "Content security policy trusted sites"),

    # ── SECURITY & ACCESS ─────────────────────────────────────────────
    MetadataTypeConfig("Profile",           "security",    1, False, "User profiles — base permissions"),
    MetadataTypeConfig("PermissionSet",     "security",    1, False, "Permission sets — additive permissions"),
    MetadataTypeConfig("PermissionSetGroup","security",    2, False, "Permission set groups"),
    MetadataTypeConfig("CustomPermission",  "security",    2, False, "Custom permissions for feature flags"),
    MetadataTypeConfig("SharingRules",      "security",    2, False, "Record sharing rules — data visibility"),
    MetadataTypeConfig("Role",              "security",    2, False, "Role hierarchy — manager rollup and visibility"),
    MetadataTypeConfig("Territory",         "security",    3, False, "Territory management rules"),

    # ── CONFIGURATION ─────────────────────────────────────────────────
    MetadataTypeConfig("CustomLabel",       "config",      1, False, "Custom labels — config values, thresholds, messages"),
    MetadataTypeConfig("CustomMetadata",    "config",      1, False, "Custom metadata types — configuration tables"),
    MetadataTypeConfig("CustomSetting",     "config",      1, False, "Custom settings — org/profile/user config"),
    MetadataTypeConfig("FeatureParameter",  "config",      3, False, "Feature parameters"),
    MetadataTypeConfig("StaticResource",    "config",      3, False, "Static resources — JS libraries, CSS, images"),

    # ── EMAIL & NOTIFICATIONS ─────────────────────────────────────────
    MetadataTypeConfig("EmailTemplate",     "notifications",2, False, "Email templates used in workflow/flows"),
    MetadataTypeConfig("EmailServicesFunction","notifications",2, False, "Inbound email service handlers"),
    MetadataTypeConfig("NotificationTypeConfig","notifications",3,False,"Custom notification configurations"),

    # ── REPORTING ─────────────────────────────────────────────────────
    MetadataTypeConfig("Report",            "reporting",   3, False, "Reports (large orgs may have thousands)"),
    MetadataTypeConfig("Dashboard",         "reporting",   3, False, "Dashboards"),
    MetadataTypeConfig("ReportType",        "reporting",   3, False, "Custom report types"),

    # ── COMMUNITY / EXPERIENCE CLOUD ──────────────────────────────────
    MetadataTypeConfig("Community",         "experience",  3, False, "Experience Cloud sites"),
    MetadataTypeConfig("Network",           "experience",  3, False, "Experience Cloud network config"),

    # ── DATA MANAGEMENT ───────────────────────────────────────────────
    MetadataTypeConfig("DataCategoryGroup", "data_mgmt",   3, False, "Data category groups"),
    MetadataTypeConfig("MilestoneType",     "data_mgmt",   3, False, "Milestone types for entitlements"),
    MetadataTypeConfig("EntitlementProcess","data_mgmt",   2, False, "Entitlement processes — SLA timers"),

    # ── PLATFORM EVENTS & MESSAGING ───────────────────────────────────
    MetadataTypeConfig("PlatformEventChannel",    "events",2, False, "Platform event channel definitions"),
    MetadataTypeConfig("EventDelivery",           "events",2, False, "Event delivery configurations"),
    MetadataTypeConfig("EventSubscription",       "events",2, False, "Platform event subscriptions"),
    MetadataTypeConfig("PlatformCachePartition",  "events",3, False, "Platform cache partition config"),

    # ── OMNI-CHANNEL ──────────────────────────────────────────────────
    MetadataTypeConfig("ServiceChannel",    "omnichannel", 3, False, "Service channel for routing"),
    MetadataTypeConfig("QueueRoutingConfig","omnichannel", 3, False, "Queue routing configuration"),
    MetadataTypeConfig("RoutingConfiguration","omnichannel",3,False, "Omni-channel routing config"),
]


# Convenient grouped access
def get_types_by_category(category: str) -> list[MetadataTypeConfig]:
    return [t for t in METADATA_TYPES if t.category == category]


def get_types_by_priority(max_priority: int = 2) -> list[MetadataTypeConfig]:
    return [t for t in METADATA_TYPES if t.priority <= max_priority]


def get_critical_types() -> list[MetadataTypeConfig]:
    """Returns the metadata types most critical for LOS documentation."""
    return get_types_by_priority(1)


def get_all_api_names() -> list[str]:
    return [t.api_name for t in METADATA_TYPES]


# Categories map for reporting
CATEGORIES = {
    "apex": "Apex Code",
    "automation": "Flows & Automation",
    "schema": "Objects & Schema",
    "rules": "Validation & Business Rules",
    "ui": "UI Components",
    "approvals": "Approvals & Queues",
    "integrations": "Integrations",
    "security": "Security & Permissions",
    "config": "Configuration",
    "notifications": "Email & Notifications",
}
