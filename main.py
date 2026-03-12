#!/usr/bin/env python3
"""
main.py — Salesforce Org Documentation Agent

Usage:
  python main.py run                        Full pipeline (all phases)
  python main.py run --resume               Resume from last checkpoint
  python main.py run --from-phase reason    Skip harvest, start at LLM analysis
  python main.py run --from-phase document  Re-generate docs only (no API calls)
  python main.py run --dry-run              Test SF connection only
  python main.py serve                      Serve generated docs locally
  python main.py status                     Show checkpoint status
  python main.py clean                      Clear all cached state
"""
import sys
import json
import logging
import time
from pathlib import Path
from datetime import datetime, timedelta

import click
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    Progress, SpinnerColumn, TextColumn,
    BarColumn, TimeElapsedColumn, MofNCompleteColumn
)
from rich.table import Table
from rich.live import Live
from rich.text import Text
from rich import print as rprint

# ── Logging: file only (Rich handles console) ─────────────────────────
Path("logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.FileHandler("logs/agent.log")],
)
logger = logging.getLogger(__name__)

console = Console()

# ─────────────────────────────────────────────────────────────────────
# CHECKPOINT HELPERS
# ─────────────────────────────────────────────────────────────────────

CHECKPOINT_DIR = Path(".cache/checkpoints")

PHASE_ORDER = ["connect", "harvest", "parse", "reason", "map_org", "write_docs"]
PHASE_LABELS = {
    "connect":    "Connect to Salesforce",
    "harvest":    "Harvest metadata",
    "parse":      "Parse Apex & Flows",
    "reason":     "LLM semantic analysis",
    "map_org":"Map org profile",
    "write_docs": "Write documentation",
}

def save_checkpoint(phase: str, state: dict) -> None:
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    path = CHECKPOINT_DIR / f"{phase}.json"
    with open(path, "w") as f:
        json.dump(state, f, default=str, indent=2)
    logger.info(f"Checkpoint saved: {phase}")

def load_checkpoint(phase: str) -> dict | None:
    path = CHECKPOINT_DIR / f"{phase}.json"
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return None

def latest_checkpoint() -> str | None:
    """Return the name of the latest completed phase checkpoint."""
    for phase in reversed(PHASE_ORDER):
        if (CHECKPOINT_DIR / f"{phase}.json").exists():
            return phase
    return None

def checkpoint_summary() -> list[dict]:
    rows = []
    for phase in PHASE_ORDER:
        path = CHECKPOINT_DIR / f"{phase}.json"
        if path.exists():
            mtime = datetime.fromtimestamp(path.stat().st_mtime)
            size = path.stat().st_size
            rows.append({"phase": phase, "status": "✅ Done", "saved": mtime.strftime("%H:%M:%S"), "size": f"{size/1024:.0f}KB"})
        else:
            rows.append({"phase": phase, "status": "⬜ Pending", "saved": "—", "size": "—"})
    return rows


# ─────────────────────────────────────────────────────────────────────
# RICH UI HELPERS
# ─────────────────────────────────────────────────────────────────────

def print_banner():
    console.print()
    console.print(Panel(
        "[bold white]Salesforce Org Documentation Agent[/bold white]\n"
        "[dim]Reverse-engineering hidden knowledge from your Salesforce org[/dim]\n"
        "[dim]Powered by LangGraph + Claude AI[/dim]",
        style="bold blue",
        padding=(1, 4),
    ))
    console.print()

def print_phase_header(num: int, name: str, desc: str):
    console.print(f"\n[bold cyan]Phase {num}[/bold cyan] [bold white]·[/bold white] [bold]{name}[/bold]")
    console.print(f"[dim]{desc}[/dim]")

def print_success(msg: str):
    console.print(f"[bold green]✅[/bold green] {msg}")

def print_warning(msg: str):
    console.print(f"[bold yellow]⚠️[/bold yellow]  {msg}")

def print_error(msg: str):
    console.print(f"[bold red]❌[/bold red] {msg}")

def print_final_summary(state: dict, output_dir: str, elapsed: float):
    journey = state.get("org_profile")
    files = state.get("generated_files", [])
    annotations = state.get("annotations", {})
    timings = state.get("phase_timings", {})

    console.print()
    console.rule("[bold green]Documentation Complete[/bold green]")
    console.print()

    # Stats table
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("", style="dim")
    table.add_column("", style="bold white")

    table.add_row("Components analyzed", str(len(annotations)))
    if journey:
        active_stages = sum(1 for s in journey.categories if s.components)
        table.add_row("Loan stages documented", str(active_stages))
        table.add_row("Hidden rules discovered", f"[bold yellow]{len(journey.hidden_logic_master)}[/bold yellow]")
        table.add_row("Risk flags found", str(len(journey.risk_register)))
        table.add_row("External integrations", str(len(journey.outbound_integrations)))
    table.add_row("Files generated", str(len(files)))
    table.add_row("Total time", f"{elapsed:.0f}s")

    console.print(table)
    console.print()

    # Phase timing breakdown
    if timings:
        timing_table = Table(title="Phase Timings", show_header=True, header_style="bold dim")
        timing_table.add_column("Phase")
        timing_table.add_column("Time", justify="right")
        for phase, secs in timings.items():
            timing_table.add_row(PHASE_LABELS.get(phase, phase), f"{secs:.1f}s")
        console.print(timing_table)
        console.print()

    # Output paths
    out = Path(output_dir).absolute()
    console.print(f"[bold]Output directory:[/bold] {out}")
    console.print()
    console.print(f"  📄  [underline]{out}/INDEX.md[/underline]                    ← Start here")
    console.print(f"  ⚡  [underline]{out}/hidden-rules-discovered.md[/underline]  ← Most valuable")
    console.print(f"  🌐  [underline]{out.parent}/site/index.html[/underline]       ← MkDocs site (if built)")
    console.print()
    console.print("[dim]Tip: Run [bold]python main.py serve[/bold] to preview the docs in your browser[/dim]")


# ─────────────────────────────────────────────────────────────────────
# CLI COMMANDS
# ─────────────────────────────────────────────────────────────────────

@click.group()
def cli():
    """Salesforce Org Documentation Agent — powered by LangGraph + Claude AI."""
    pass


@cli.command()
@click.option("--config",       default="config/config.yaml",  help="Path to config file", show_default=True)
@click.option("--output-dir",   default="output/docs",         help="Where to write docs", show_default=True)
@click.option("--resume",       is_flag=True,                  help="Resume from last checkpoint")
@click.option("--from-phase",   default=None,
              type=click.Choice(["harvest", "reason", "map_org", "write_docs"]),
              help="Start from a specific phase (uses checkpoint for earlier phases)")
@click.option("--dry-run",      is_flag=True,                  help="Test SF connection only, no analysis")
@click.option("--clean",        is_flag=True,                  help="Clear checkpoints before running")
@click.option("--no-mkdocs",    is_flag=True,                  help="Skip MkDocs site build")
@click.option("--org-name",     default="My LOS",              help="Display name for the org in docs", show_default=True)
def run(config, output_dir, resume, from_phase, dry_run, clean, no_mkdocs, org_name):
    """Run the full documentation pipeline."""
    print_banner()
    start = time.time()

    # ── Load config ──────────────────────────────────────────────────
    try:
        from config.settings import get_settings
        settings = get_settings(config)
    except FileNotFoundError as e:
        print_error(str(e))
        sys.exit(1)
    except Exception as e:
        print_error(f"Config error: {e}")
        sys.exit(1)

    config_dict = {
        "salesforce": settings.salesforce.model_dump(),
        "llm": settings.llm.model_dump(),
        "crawl": settings.crawl.model_dump(),
        "output": settings.output.model_dump(),
    }

    # ── Clear checkpoints if requested ──────────────────────────────
    if clean and CHECKPOINT_DIR.exists():
        import shutil
        shutil.rmtree(CHECKPOINT_DIR)
        console.print("[dim]Checkpoints cleared.[/dim]")

    # ── Determine start phase ────────────────────────────────────────
    start_from = None
    if from_phase:
        start_from = from_phase
        console.print(f"[dim]Starting from phase: [bold]{from_phase}[/bold][/dim]")
    elif resume:
        last = latest_checkpoint()
        if last:
            # Find the next phase after the last completed one
            idx = PHASE_ORDER.index(last) if last in PHASE_ORDER else -1
            if idx >= 0 and idx + 1 < len(PHASE_ORDER):
                # Map internal phase names to pipeline node names
                phase_map = {"parse": "reason", "connect": "harvest"}
                next_phase = PHASE_ORDER[idx + 1]
                start_from = phase_map.get(next_phase, next_phase)
                console.print(f"[dim]Resuming from: [bold]{start_from}[/bold] (last complete: {last})[/dim]")
            else:
                console.print("[dim]All phases already complete. Use --clean to re-run.[/dim]")
        else:
            console.print("[dim]No checkpoints found. Running from start.[/dim]")

    # ── Build initial state ──────────────────────────────────────────
    from agents.pipeline import make_initial_state, build_pipeline, build_partial_pipeline

    state = make_initial_state(config_dict, output_dir)

    # Load checkpoint data if resuming
    if start_from:
        for phase in PHASE_ORDER:
            ckpt = load_checkpoint(phase)
            if ckpt:
                state.update(ckpt)
                if phase == start_from:
                    break

    # ── Dry run: just test connection ────────────────────────────────
    if dry_run:
        with console.status("[bold cyan]Testing Salesforce connection...[/bold cyan]"):
            try:
                from tools.salesforce_client import SalesforceClient
                from config.settings import SalesforceConfig
                sf = SalesforceClient(SalesforceConfig(**config_dict["salesforce"]))
                sf.connect()
                print_success(f"Connected to Salesforce: {settings.salesforce.instance_url}")
                print_success(f"User: {settings.salesforce.username}")
                print_success(f"LLM provider: {settings.llm.display_name}")
                console.print("\n[bold green]Dry run passed — ready to go![/bold green]")
            except Exception as e:
                print_error(f"Connection failed: {e}")
                sys.exit(1)
        return

    # ── Build and run the pipeline ───────────────────────────────────
    if start_from:
        pipeline = build_partial_pipeline(start_from)
    else:
        pipeline = build_pipeline()

    console.print()
    console.rule("[dim]Starting Pipeline[/dim]")

    # Run pipeline with live status display
    final_state = _run_pipeline_with_progress(pipeline, state)

    # ── Save final checkpoint ────────────────────────────────────────
    save_checkpoint("write_docs", {
        "annotations": final_state.get("annotations", {}),
        "generated_files": final_state.get("generated_files", []),
        "phase_timings": final_state.get("phase_timings", {}),
    })

    # ── Build MkDocs site ────────────────────────────────────────────
    if not no_mkdocs and final_state.get("journey"):
        _build_mkdocs(final_state["org_profile"], output_dir, org_name)

    # ── Print final summary ──────────────────────────────────────────
    elapsed = time.time() - start
    print_final_summary(final_state, output_dir, elapsed)


def _run_pipeline_with_progress(pipeline, initial_state: dict) -> dict:
    """
    Run the LangGraph pipeline, showing a Rich progress display.
    LangGraph streams node-by-node events which we map to progress steps.
    """
    NODE_LABELS = {
        "connect":      ("🔗", "Connecting to Salesforce"),
        "harvest":      ("📥", "Crawling metadata from Salesforce"),
        "parse_apex":   ("⚙️ ", "Parsing Apex classes & triggers"),
        "parse_flows":  ("⚡", "Parsing Flows → pseudocode"),
        "parse_schema": ("🏛️ ", "Preparing schema & validation rules"),
        "reason":       ("🧠", "AI semantic analysis (Claude)"),
        "map_org":  ("🗺️ ", "Mapping org profile"),
        "write_docs":   ("✍️ ", "Writing documentation files"),
    }

    final_state = dict(initial_state)
    current_node = None

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold cyan]{task.description}"),
        BarColumn(bar_width=30),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress:

        main_task = progress.add_task("[bold]Running pipeline...[/bold]", total=len(NODE_LABELS))

        try:
            # LangGraph stream mode — yields state updates after each node
            for event in pipeline.stream(initial_state, stream_mode="updates"):
                for node_name, node_output in event.items():
                    if node_name == "__end__":
                        continue

                    icon, label = NODE_LABELS.get(node_name, ("●", node_name))
                    progress.update(main_task, description=f"{icon}  {label}", advance=1)

                    # Merge output into final state
                    if isinstance(node_output, dict):
                        final_state.update(node_output)

                        # Save checkpoint after important phases
                        if node_name in ("harvest", "parse_apex", "reason"):
                            _save_node_checkpoint(node_name, node_output)

                        # Show inline stats
                        status = node_output.get("status", "")
                        if status:
                            console.print(f"  [dim]{icon} {status}[/dim]")

                    current_node = node_name

        except KeyboardInterrupt:
            print_warning("Interrupted. Progress saved in checkpoints.")
            raise
        except Exception as e:
            print_error(f"Pipeline error in node '{current_node}': {e}")
            logger.exception(f"Pipeline error in {current_node}")
            raise

    return final_state


def _save_node_checkpoint(node_name: str, node_output: dict) -> None:
    """Save relevant fields from a node output as a checkpoint."""
    CHECKPOINT_FIELDS = {
        "harvest":    ["apex_classes", "apex_triggers", "flows", "validation_rules",
                       "named_credentials", "remote_site_settings", "approval_processes"],
        "parse_apex": ["apex_classes", "apex_triggers", "call_graph"],
        "reason":     ["annotations"],
    }
    fields = CHECKPOINT_FIELDS.get(node_name, [])
    if fields:
        data = {k: node_output[k] for k in fields if k in node_output}
        if data:
            save_checkpoint(node_name, data)


def _build_mkdocs(org_profile, output_dir: str, org_name: str) -> None:
    """Build the MkDocs site from generated docs."""
    console.print()
    with console.status("[bold cyan]Building MkDocs site...[/bold cyan]"):
        try:
            from tools.mkdocs_builder import MkDocsBuilder
            builder = MkDocsBuilder(docs_dir=output_dir, org_name=org_name)
            site_path = builder.build(org_profile)
            print_success(f"MkDocs site built → {site_path}")
        except Exception as e:
            print_warning(f"MkDocs build failed (docs still available as Markdown): {e}")
            logger.warning(f"MkDocs build error: {e}")


# ─────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--port", default=8000, help="Port to serve on", show_default=True)
@click.option("--output-dir", default="output", help="Output directory (parent of docs/)", show_default=True)
def serve(port, output_dir):
    """Serve the generated MkDocs documentation locally with live reload."""
    print_banner()

    mkdocs_yml = Path(output_dir) / "mkdocs.yml"
    if not mkdocs_yml.exists():
        print_error(f"No mkdocs.yml found at {mkdocs_yml}")
        print_warning("Run 'python main.py run' first to generate documentation.")
        sys.exit(1)

    from tools.mkdocs_builder import MkDocsBuilder
    builder = MkDocsBuilder(docs_dir=str(Path(output_dir) / "docs"))
    builder.serve(port=port)


@cli.command()
def status():
    """Show the status of pipeline checkpoints."""
    print_banner()

    rows = checkpoint_summary()
    table = Table(title="Pipeline Checkpoint Status", header_style="bold cyan")
    table.add_column("Phase", style="bold")
    table.add_column("Status")
    table.add_column("Saved At", style="dim")
    table.add_column("Size", style="dim", justify="right")

    for row in rows:
        status_color = "green" if "✅" in row["status"] else "dim"
        table.add_row(
            PHASE_LABELS.get(row["phase"], row["phase"]),
            f"[{status_color}]{row['status']}[/{status_color}]",
            row["saved"],
            row["size"],
        )

    console.print(table)

    last = latest_checkpoint()
    if last:
        console.print(f"\n[dim]Resume from next phase with: [bold]python main.py run --resume[/bold][/dim]")
    else:
        console.print(f"\n[dim]No checkpoints found. Run: [bold]python main.py run[/bold][/dim]")


@cli.command()
def clean():
    """Clear all cached checkpoints and metadata cache."""
    if CHECKPOINT_DIR.exists():
        import shutil
        shutil.rmtree(CHECKPOINT_DIR)
        console.print("[green]✅ Checkpoints cleared[/green]")
    cache = Path(".cache/metadata")
    if cache.exists():
        import shutil
        shutil.rmtree(cache)
        console.print("[green]✅ Metadata cache cleared[/green]")
    console.print("[dim]Run 'python main.py run' to start fresh.[/dim]")


@cli.command()
@click.option("--config", default="config/config.yaml", help="Path to config file", show_default=True)
def setup(config):
    """Interactive setup wizard — create config.yaml from scratch."""
    print_banner()
    console.print(Panel(
        "[bold]Setup Wizard[/bold]\n"
        "[dim]Creates your config.yaml step by step.\n"
        "You can edit it manually afterwards at any time.[/dim]",
        style="cyan", padding=(0, 2)
    ))
    console.print()

    cfg_path = Path(config)
    if cfg_path.exists():
        overwrite = click.confirm(f"  ⚠️  {config} already exists. Overwrite?", default=False)
        if not overwrite:
            console.print("[dim]Cancelled — existing config kept.[/dim]")
            return
    cfg_path.parent.mkdir(parents=True, exist_ok=True)

    # ── Step 1: Org URL ──────────────────────────────────────────────
    console.rule("[bold cyan]Step 1 · Salesforce Org[/bold cyan]")
    console.print("[dim]Use your org's 'My Domain' URL — not login.salesforce.com[/dim]")
    console.print("[dim]Example: https://mycompany.my.salesforce.com[/dim]\n")
    instance_url = click.prompt("  Org URL").strip().rstrip("/")

    # ── Step 2: Auth method ──────────────────────────────────────────
    console.print()
    console.rule("[bold cyan]Step 2 · Authentication Method[/bold cyan]")
    console.print()
    console.print("  [bold]A)[/bold] Username + Password + Security Token")
    console.print("     [dim]Simplest. Use for dev/POC. Needs MFA turned off.[/dim]\n")
    console.print("  [bold]B)[/bold] OAuth Connected App — Client Credentials")
    console.print("     [dim]Best for shared/production use. Needs a Connected App with[/dim]")
    console.print("     [dim]'Client Credentials Flow' enabled and a 'Run As' user set.[/dim]\n")
    console.print("  [bold]C)[/bold] JWT Bearer Token")
    console.print("     [dim]For CI/CD pipelines. Needs a Connected App with digital[/dim]")
    console.print("     [dim]signature enabled and a pre-generated RSA key pair.[/dim]\n")

    auth_choice = click.prompt(
        "  Choose auth method",
        type=click.Choice(["A", "B", "C"], case_sensitive=False),
        default="A",
    ).upper()

    # ── Collect credentials based on chosen method ───────────────────
    sf_config: dict = {"instance_url": instance_url}

    if auth_choice == "A":
        console.print()
        console.print("[bold]Username + Password Setup[/bold]")
        console.print("[dim]Security token: Setup → My Personal Info → Reset My Security Token[/dim]")
        console.print("[dim]Leave token blank if your IP is whitelisted in Setup → Network Access[/dim]\n")
        sf_config["username"]       = click.prompt("  Username (email)").strip()
        sf_config["password"]       = click.prompt("  Password", hide_input=True)
        sf_config["security_token"] = click.prompt("  Security token (blank if IP whitelisted)", default="")

    elif auth_choice == "B":
        console.print()
        console.print("[bold]OAuth Client Credentials Setup[/bold]")
        console.print("[dim]Connected App checklist:[/dim]")
        console.print("[dim]  1. Enable OAuth Settings → Enable Client Credentials Flow[/dim]")
        console.print("[dim]  2. Set 'Run As' to your integration user[/dim]")
        console.print("[dim]  3. IP Relaxation → Relax IP restrictions[/dim]")
        console.print("[dim]  4. Copy Consumer Key and Consumer Secret below[/dim]\n")
        sf_config["client_id"]     = click.prompt("  Consumer Key (client_id)").strip()
        sf_config["client_secret"] = click.prompt("  Consumer Secret (client_secret)", hide_input=True)

    elif auth_choice == "C":
        console.print()
        console.print("[bold]JWT Bearer Token Setup[/bold]")
        console.print("[dim]Connected App checklist:[/dim]")
        console.print("[dim]  1. Enable OAuth Settings → Use Digital Signatures → upload server.crt[/dim]")
        console.print("[dim]  2. Pre-authorize the user in Manage Connected Apps[/dim]")
        console.print("[dim]  Generate key pair: openssl req -x509 -nodes -newkey rsa:2048[/dim]")
        console.print("[dim]    -keyout server.key -out server.crt -days 365[/dim]\n")
        sf_config["client_id"]        = click.prompt("  Consumer Key (client_id)").strip()
        sf_config["username"]         = click.prompt("  Username (pre-authorized user email)").strip()
        sf_config["private_key_file"] = click.prompt("  Path to private key file", default="config/server.key")

    sf_config["api_version"] = click.prompt("\n  API version", default="61.0")

    # ── Step 3: Anthropic ────────────────────────────────────────────
    console.print()
    console.rule("[bold cyan]Step 3 · LLM Provider[/bold cyan]")
    console.print()
    console.print("  [bold]1)[/bold] Anthropic  (Claude) — console.anthropic.com")
    console.print("  [bold]2)[/bold] OpenAI     (GPT-4o) — platform.openai.com")
    console.print("  [bold]3)[/bold] Azure OpenAI        — your Azure portal")
    console.print("  [bold]4)[/bold] Google     (Gemini) — aistudio.google.com")
    console.print("  [bold]5)[/bold] Ollama     (local)  — ollama.ai\n")
    llm_choice = click.prompt("  Choose provider", type=click.Choice(["1","2","3","4","5"]), default="1")
    provider_map = {"1":"anthropic","2":"openai","3":"azure","4":"google","5":"ollama"}
    llm_provider = provider_map[llm_choice]
    default_models = {"anthropic":"claude-sonnet-4-6","openai":"gpt-4o","azure":"gpt-4o","google":"gemini-1.5-pro","ollama":"llama3.1"}
    llm_config = {"provider": llm_provider}
    if llm_provider == "ollama":
        llm_config["model"] = click.prompt("  Model name", default="llama3.1")
        llm_config["ollama_base_url"] = click.prompt("  Ollama URL", default="http://localhost:11434")
        model = llm_config["model"]
    elif llm_provider == "azure":
        llm_config["api_key"]           = click.prompt("  Azure API key", hide_input=True).strip()
        llm_config["azure_endpoint"]    = click.prompt("  Azure endpoint").strip()
        llm_config["model"]             = click.prompt("  Deployment name", default="gpt-4o")
        llm_config["azure_api_version"] = click.prompt("  API version", default="2024-02-01")
        model = llm_config["model"]
    else:
        llm_config["api_key"] = click.prompt("  API key", hide_input=True).strip()
        llm_config["model"]   = click.prompt("  Model", default=default_models.get(llm_provider, ""))
        model = llm_config["model"]
    anthropic_key = llm_config.get("api_key", "")

    # ── Step 4: Output ───────────────────────────────────────────────
    console.print()
    console.rule("[bold cyan]Step 4 · Output[/bold cyan]")
    org_name   = click.prompt("  Display name for this org in docs", default="My LOS")
    output_dir = click.prompt("  Output directory for docs", default="output/docs")

    # ── Write config.yaml ────────────────────────────────────────────
    import yaml as _yaml

    config_data = {
        "salesforce": sf_config,
        "llm": {**llm_config, "max_tokens": 4096, "verify_ssl": False},
        "crawl": {
            "metadata_types":    "all",
            "parallel_requests": 3,
            "requests_per_second": 5,
            "enable_cache":      True,
            "cache_dir":         ".cache/metadata",
        },
        "output": {
            "output_dir":         output_dir,
            "format":             "markdown",
            "include_diagrams":   True,
            "include_raw_excerpts": False,
        },
    }

    # Add a comment header manually since PyYAML strips comments
    auth_method_comment = {
        "A": "# Auth: Username + Password",
        "B": "# Auth: OAuth Client Credentials (Connected App)",
        "C": "# Auth: JWT Bearer Token",
    }[auth_choice]

    yaml_content = (
        f"# Salesforce Org Documentation Agent — Config\n"
        f"# Generated by setup wizard on {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"# Org: {org_name}\n"
        f"{auth_method_comment}\n\n"
        + _yaml.dump(config_data, default_flow_style=False, sort_keys=False, allow_unicode=True)
    )

    cfg_path.write_text(yaml_content, encoding="utf-8")
    console.print()
    print_success(f"Config saved → [bold]{cfg_path}[/bold]")
    console.print(f"  [dim]Auth method: {auth_choice} — {['Username/Password','OAuth Client Credentials','JWT Bearer'][ord(auth_choice)-65]}[/dim]")

    # ── Test connection ──────────────────────────────────────────────
    console.print()
    if click.confirm("  Test Salesforce connection now?", default=True):
        with console.status("[bold cyan]Connecting...[/bold cyan]"):
            try:
                from tools.salesforce_client import SalesforceClient
                from config.settings import get_settings, reset_settings
                reset_settings()
                settings = get_settings(config)
                sf = SalesforceClient(settings.salesforce)
                sf.connect()
                print_success(f"Connected! Instance: {sf._instance_url}")
            except Exception as e:
                print_error(f"Connection failed: {e}")
                console.print()
                console.print("[dim]Common fixes:[/dim]")
                if auth_choice == "B":
                    console.print("[dim]  • Verify 'Client Credentials Flow' is enabled in your Connected App[/dim]")
                    console.print("[dim]  • Verify 'Run As' user is set[/dim]")
                    console.print("[dim]  • Verify IP Relaxation is set to 'Relax IP restrictions'[/dim]")
                    console.print("[dim]  • Wait 2-10 min after creating/editing the Connected App[/dim]")
                elif auth_choice == "A":
                    console.print("[dim]  • Reset security token: Setup → My Personal Info → Reset My Security Token[/dim]")
                    console.print("[dim]  • Or whitelist your IP: Setup → Network Access[/dim]")
                elif auth_choice == "C":
                    console.print("[dim]  • Verify private_key_file path exists[/dim]")
                    console.print("[dim]  • Ensure user has pre-authorized the Connected App[/dim]")
                console.print(f"\n[dim]Edit your config and re-test: [bold]python main.py run --dry-run[/bold][/dim]")
                return

    # ── Done ─────────────────────────────────────────────────────────
    console.print()
    console.print(Panel(
        f"[bold green]Setup complete![/bold green]\n\n"
        f"Run the full documentation pipeline:\n"
        f"  [bold]python main.py run --org-name '{org_name}'[/bold]\n\n"
        f"Or test connection only:\n"
        f"  [bold]python main.py run --dry-run[/bold]",
        style="green", padding=(0, 2)
    ))


if __name__ == "__main__":
    cli()
