# Salesforce Org Intelligence Agent

Reverse-engineers any Salesforce org into complete technical documentation —
automatically, using AI. Works for any org, any domain, any industry.

## What it documents

| Component Type | What it extracts |
|---|---|
| **Apex Classes** | Purpose, objects read/written, callouts, hidden logic, call graph |
| **Apex Triggers** | Trigger events, DML side-effects, chained automation |
| **Flows** (all types) | Record-triggered, screen, scheduled, autolaunched — full pseudocode |
| **Process Builder** | Legacy automation, field updates, Apex invocations |
| **Workflow Rules** | Field updates, email alerts, outbound messages, time-based actions |
| **Validation Rules** | Formula decoded to plain English, business constraint explained |
| **LWC Components** | Wire calls, Apex dependencies, user-facing actions |
| **Aura Components** | Controllers, events, actions |
| **Visualforce Pages** | Controller class, forms, access method |
| **Approval Processes** | Who approves, escalation rules, field changes |
| **Outbound Integrations** | Named Credentials, Remote Site Settings, HTTP callouts |
| **Inbound Integrations** | Connected Apps, REST API endpoints (`@RestResource`) |

## Output

```
output/docs/
├── INDEX.md
├── 00-org-overview.md              What this org does + full component breakdown
├── apex-classes/                   One page per class with full detail
├── flows-record-triggered/
├── flows-screen/
├── lwc-components/
├── approval-processes/
├── outbound-integrations.md        Every external API this org calls
├── inbound-integrations.md         External systems that call into this org
├── hidden-logic-discovered.md  ⚡  Business rules found in code — highest value
├── risk-register.md                Tech debt by severity (HIGH / MEDIUM / LOW)
├── object-usage-map.md             Which components touch which Salesforce objects
└── callout-map.md                  Which components call which endpoints
```

Also builds a searchable **MkDocs Material** website from the same files.

## Quick Start

```bash
# 1. Install
pip install -r requirements.txt

# 2. Configure (interactive wizard)
python main.py setup

# 3. Test connection
python main.py run --dry-run

# 4. Run
python main.py run --org-name "My Company"

# 5. View docs in browser
python main.py serve
```

## All Commands

```bash
python main.py run                          # Full pipeline
python main.py run --resume                 # Resume from last checkpoint
python main.py run --from-phase reason      # Skip harvest, re-run LLM analysis
python main.py run --from-phase write_docs  # Re-generate docs only (no API calls)
python main.py run --dry-run                # Test SF connection only
python main.py status                       # Show checkpoint status
python main.py serve                        # Preview docs at localhost:8000
python main.py clean                        # Clear all cached state
python main.py setup                        # Re-run config wizard
```

## Authentication Methods

**Method A — Username + Password** (simplest, for dev/POC):
```yaml
salesforce:
  instance_url: "https://yourorg.my.salesforce.com"
  username: "user@yourcompany.com"
  password: "YourPassword"
  security_token: "YourToken"   # from Setup > My Personal Info > Reset Token
```

**Method B — OAuth Connected App** (recommended for production):
```yaml
salesforce:
  instance_url: "https://yourorg.my.salesforce.com"
  client_id: "3MVG9..."       # Consumer Key
  client_secret: "abc123..."  # Consumer Secret
  # Connected App must have: Client Credentials Flow enabled + Run As user set
```

**Method C — JWT Bearer** (for CI/CD):
```yaml
salesforce:
  instance_url: "https://yourorg.my.salesforce.com"
  client_id: "3MVG9..."
  username: "user@yourcompany.com"
  private_key_file: "config/server.key"
```

## Permissions Required

The Salesforce integration user needs:
- `View Setup and Configuration`
- `Author Apex` (or `View All Apex`)  
- `Manage Flow` (or `View All Flows`)
- `View All Data` (for metadata retrieval)
- API access enabled on their profile

## Architecture

```
LangGraph Pipeline:

connect → harvest ──┬── parse_apex      ──┐
                    ├── parse_flows      ──┤
                    ├── parse_ui         ──┤  (parallel)
                    ├── parse_rules      ──┤
                    └── parse_processes  ──┘
                                          │
                                       reason   (Claude LLM analysis)
                                          │
                                       map_org  (assemble OrgProfile)
                                          │
                                      write_docs (Markdown + MkDocs)
```

Built with: LangGraph · Claude API · simple-salesforce · MkDocs Material
# SalesforceOrgAnalyzer
# SalesforceOrgAnalyzer
# SalesforceOrgAnalyzer
