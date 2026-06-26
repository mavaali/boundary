from __future__ import annotations
import argparse
import sys
from pathlib import Path

from boundary.agent import Agent
from boundary.overlay import Overlay


def _print_best_of_k(bres, k: int) -> None:
    """Render a best-of-K result summary for the CLI (shared by run + fielding-coach)."""
    print(f"\n=== best-of-{k} ===")
    for c in bres.candidates:
        print(f"  run{c.k}: {c.verdict:4}  ${getattr(c.result, 'estimated_dollars', 0.0):.4f}  -> {list(c.run_paths)}")
    if bres.judge:
        print(f"  judge: margin={bres.judge.margin:.2f} abstain={bres.judge.abstain} ranking={bres.judge.ranking}")
    wk = bres.winner.k if bres.winner else None
    print(f"  winner: run{wk}  escalation={bres.escalation}")
    if bres.promoted:
        print(f"  promoted: {bres.promoted}")
    else:
        print("  promoted: none" + (f" — queued for review #{bres.review_id}" if bres.review_id else ""))
    if bres.review_id:
        print(f"  review: #{bres.review_id} ({bres.escalation}) — boundary review-queue list")


def _prompt_commit_policy(agent, on_commit_flag: str | None, commit_allow_flag: list[str]) -> tuple[str, list[str]]:
    """Decide the commit policy for an interactive run.

    If --on-commit was passed, honor it (always wins).
    If no commit tools are registered, default to "refuse" silently — no prompt.
    Otherwise prompt the human: [r]efuse / [q]ueue / [a]sk (default r).
    "allow" requires CLI flags — we don't offer it via prompt to avoid
    accidental commits.
    """
    if on_commit_flag:
        return on_commit_flag, list(commit_allow_flag or [])
    # Detect commit-kind tools in the agent's registry.
    commit_tools = [t.name for t in agent.tools._tools.values() if t.kind == "commit"]
    if not commit_tools:
        return "refuse", []
    import sys as _sys
    if not _sys.stdin.isatty():
        # Non-TTY run without explicit flag: refuse is the safe default.
        return "refuse", []
    print(f"\n[commit-policy] Commit-class tools in this run: {commit_tools}")
    print("  r = refuse  (any commit tool call is refused — default)")
    print("  q = queue   (halt and queue for review)")
    print("  a = ask     (route commit attempts through ask_human)")
    try:
        resp = input("[commit-policy] choose [r/q/a] (Enter = r): ").strip().lower()
    except EOFError:
        resp = ""
    if resp.startswith("q"):
        return "queue", []
    if resp.startswith("a"):
        return "ask", []
    return "refuse", []


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser("boundary")
    sub = p.add_subparsers(dest="cmd", required=True)

    cp = sub.add_parser("copilot", help="Copilot backend utilities")
    cp_sub = cp.add_subparsers(dest="copilot_cmd", required=True)
    cp_sub.add_parser("login", help="device-code login for GitHub Copilot")
    cp_sub.add_parser("status", help="show current Copilot auth status")
    cp_sub.add_parser("models", help="list available Copilot models")

    fc = sub.add_parser(
        "fielding-coach",
        help="planner layer — propose & dispatch an envelope from a loose prompt",
    )
    fc.add_argument("prompt", help="loose user prompt to translate into an envelope")
    fc.add_argument("--workspace", default=None)
    fc.add_argument("--overlay", help="overlay name/path for workspace defaults and local skin")
    fc.add_argument("--auto", action="store_true", help="skip approval gate; dispatch the proposal immediately")
    fc.add_argument("--client", default="copilot")
    fc.add_argument("--model", default=None)
    fc.add_argument("--on-commit", choices=["refuse", "queue", "ask", "allow"], default=None)
    fc.add_argument("--commit-allow", action="append", default=[])
    fc.add_argument("--verbose", "-v", action="store_true")
    # Best-of-K (feature C) passthrough for Fielding Coach dispatch.
    fc.add_argument("--runs", type=int, default=1,
                    help="best-of-K: dispatch the proposal K times and select a winner. K=1 (default) disables.")
    fc.add_argument("--mode", choices=["interactive", "headless"], default="interactive",
                    help="best-of-K selection mode (interactive blocks on close calls; headless never blocks)")
    fc.add_argument("--select-margin", type=float, default=0.15,
                    help="best-of-K: judge score margin below which a selection is a close call")
    fc.add_argument("--judge-model", default=None, help="best-of-K: model for the selection judge")
    fc.add_argument("--headless-fallback", choices=["auto_pick_flag", "defer"], default="auto_pick_flag",
                    help="best-of-K headless close-call behavior")

    # Phase 3: schedules
    sched_inst = sub.add_parser("schedule", help="install/uninstall/list scheduled headless runs (OS scheduler)")
    sched_sub = sched_inst.add_subparsers(dest="schedule_cmd", required=True)
    si = sched_sub.add_parser("install", help="install a schedule YAML on the OS scheduler (launchd / schtasks)")
    si.add_argument("path", help="path to schedule.yaml")
    su = sched_sub.add_parser("uninstall", help="remove an installed schedule by name")
    su.add_argument("name")
    sched_sub.add_parser("list", help="list installed boundary schedules")
    sv = sched_sub.add_parser("validate", help="parse a schedule YAML and print what would be installed")
    sv.add_argument("path")

    srun = sub.add_parser("schedule-run", help="execute a schedule headlessly (used by the OS scheduler, also runnable manually)")
    srun.add_argument("path", help="path to schedule.yaml")
    srun.add_argument("--verbose", "-v", action="store_true")

    pipe = sub.add_parser("pipeline", help="install/uninstall/list multi-step headless pipelines")
    pipe_sub = pipe.add_subparsers(dest="pipeline_cmd", required=True)
    pi = pipe_sub.add_parser("install", help="install a pipeline YAML on the OS scheduler (launchd / schtasks)")
    pi.add_argument("path", help="path to pipeline.yaml")
    pu = pipe_sub.add_parser("uninstall", help="remove an installed pipeline by name")
    pu.add_argument("name")
    pipe_sub.add_parser("list", help="list installed boundary schedules and pipelines")
    pv = pipe_sub.add_parser("validate", help="parse a pipeline YAML and print what would run")
    pv.add_argument("path")

    prun = sub.add_parser("pipeline-run", help="execute a multi-step pipeline headlessly")
    prun.add_argument("path", help="path to pipeline.yaml")
    prun.add_argument("--verbose", "-v", action="store_true")

    hist = sub.add_parser("history", help="show recent runs")
    hist.add_argument("--limit", type=int, default=20)
    hist.add_argument("--schedule", default=None)

    rq = sub.add_parser("review-queue", help="show ambiguity halts waiting for human input")
    rq_sub = rq.add_subparsers(dest="rq_cmd")
    rq_list = rq_sub.add_parser("list", help="list open review items (default)")
    rq_resolve = rq_sub.add_parser("resolve")
    rq_resolve.add_argument("id", type=int)
    rq_resolve.add_argument("resolution", help="free-text resolution note")

    tu = sub.add_parser(
        "third-umpire",
        help="grade a transcript against envelope eval",
    )
    tu.add_argument("transcript", help="path to a JSONL transcript")

    sub.add_parser(
        "selftest",
        help="run adversarial fixtures asserting the envelope's guarantees (exit non-zero on regression)",
    )

    overlays = sub.add_parser("overlays", help="list/show available overlays")
    overlays_sub = overlays.add_subparsers(dest="overlays_cmd", required=True)
    overlays_sub.add_parser("list", help="list installed overlays")
    overlays_show = overlays_sub.add_parser("show", help="show overlay details")
    overlays_show.add_argument("name_or_path")

    taint_p = sub.add_parser("taint", help="inspect/clear the persisted taint ledger for a workspace")
    taint_p.add_argument("workspace", help="workspace path")
    taint_g = taint_p.add_mutually_exclusive_group()
    taint_g.add_argument("--show", action="store_true", help="print the ledger (default)")
    taint_g.add_argument("--clear", action="store_true", help="delete the ledger")

    run = sub.add_parser("run", help="run an agent on a task")
    run.add_argument("--name", default="agent")
    run.add_argument("--system", help="system prompt string")
    run.add_argument("--system-file", help="read system prompt from file")
    run.add_argument("--task", required=True)
    run.add_argument("--workspace", default=".")
    run.add_argument("--overlay", help="overlay name/path for role and workspace defaults")
    run.add_argument("--role", help="role name from the selected overlay (e.g. natasha, banner)")
    run.add_argument("--client", default="copilot", choices=["copilot", "together", "anthropic"])
    run.add_argument("--model", help="override model name")
    run.add_argument("--max-iters", type=int, default=25)
    run.add_argument("--no-shell", action="store_true")
    run.add_argument("--no-fs", action="store_true")
    run.add_argument("--sandbox-driver", choices=["seatbelt", "srt", "none"], default="seatbelt",
                     help="OS sandbox for the bash tool: seatbelt (macOS write-jail, default), "
                          "srt (cross-platform + egress allowlist; needs `npm i -g @anthropic-ai/sandbox-runtime`), "
                          "or none (no sandbox)")
    run.add_argument("--egress-allow", action="append", default=[],
                     help="under --sandbox-driver srt, allow network egress to this domain "
                          "(repeat for multiple). Empty = no network. Supports wildcards like *.example.com")
    run.add_argument("--persona", help="path to a persona charter.md to load as system prompt (Clawpilot adapter)")
    run.add_argument("--web", action="store_true", help="enable fetch_url tool")
    run.add_argument("--clawpilot", action="store_true", help="enable skill_load/charter_load/workiq bridge tools")
    run.add_argument("--envelope-writable", action="append", default=[], help="add a writable path/glob to the envelope (repeat for multiple). Activates envelope mode.")
    run.add_argument("--envelope-max-writes", type=int, default=10)
    run.add_argument("--envelope-min-writes", type=int, default=1)
    run.add_argument("--envelope-max-appends", type=int, default=10, help="max append_file calls (chunked-write continuations); separate from max_writes")
    run.add_argument("--envelope-max-external", type=int, default=20)
    run.add_argument("--envelope-max-unstaged-reads", type=int, default=3,
                     help="orientation read_file calls allowed before stage_proposal is required")
    run.add_argument("--no-staging-gate", action="store_true",
                     help="disable the stage_proposal pivot for this envelope run")
    run.add_argument("--envelope-max-input-tokens", type=int, default=500_000)
    run.add_argument("--envelope-max-output-tokens", type=int, default=50_000)
    run.add_argument("--envelope-max-dollars", type=float, default=None)
    run.add_argument("--envelope-max-wall-seconds", type=float, default=900.0)
    run.add_argument("--on-commit", choices=["refuse", "queue", "ask", "allow"], default=None,
                     help="commit-tool policy (refuse|queue|ask|allow). If omitted and commit "
                          "tools are registered, you'll be prompted interactively.")
    run.add_argument("--on-taint", choices=["refuse", "warn", "allow"], default="warn",
                     help="taint policy: what happens when untrusted external content (fetch_url) "
                          "could flow into a write. warn (default) records a taint_flow event, "
                          "refuse blocks the write, allow disables the check (a downgrade).")
    run.add_argument("--commit-allow", action="append", default=[],
                     help="under --on-commit=allow, name a specific commit tool to permit "
                          "(repeat for multiple). Empty list means all commit tools.")
    run.add_argument("--verbose", "-v", action="store_true")
    # Best-of-K (feature C): run the task K times and select a winner.
    run.add_argument("--runs", type=int, default=1,
                     help="best-of-K: run the task K times into per-run paths and select a winner "
                          "(requires --envelope-writable). K=1 (default) disables.")
    run.add_argument("--mode", choices=["interactive", "headless"], default="interactive",
                     help="best-of-K selection mode: interactive blocks on close calls (review-queue), "
                          "headless never blocks")
    run.add_argument("--select-margin", type=float, default=0.15,
                     help="best-of-K: judge score margin below which a selection is a close call")
    run.add_argument("--judge-model", default=None,
                     help="best-of-K: model for the selection judge (defaults to --model)")
    run.add_argument("--headless-fallback", choices=["auto_pick_flag", "defer"], default="auto_pick_flag",
                     help="best-of-K headless close-call behavior: auto_pick_flag promotes the top pick "
                          "and files a non-blocking advisory; defer promotes nothing")

    args = p.parse_args(argv)

    if args.cmd == "fielding-coach":
        from boundary.fielding_coach import FieldingCoach, dispatch
        from boundary.third_umpire import ThirdUmpire
        overlay = Overlay.load(args.overlay) if args.overlay else None
        workspace = overlay.workspace_or(args.workspace) if overlay else args.workspace
        if not workspace:
            print("ERROR: --workspace is required unless the overlay provides default_workspace")
            return 2
        role_label = "fielding-coach"
        s = FieldingCoach(client=args.client, model=args.model or "claude-sonnet-4.5")
        print(f"[{role_label}] proposing envelope...")
        proposal = s.propose(args.prompt, workspace_hint=workspace)
        print("\n" + proposal.to_markdown() + "\n")
        if proposal.clarifying_questions and not args.auto:
            print(f"[{role_label}] BLOCKING clarifying questions present — answer them and re-run.")
            return 1
        # Commit policy: explicit flag wins; otherwise prompt unless --auto + no commit tools registered.
        on_commit = args.on_commit
        commit_allowlist = list(args.commit_allow or [])
        if on_commit is None:
            import sys as _sys
            if args.auto or not _sys.stdin.isatty():
                on_commit = "refuse"
            else:
                print(f"[commit-policy] {role_label} dispatches enable shell (incl. bash_commit).")
                print("  r = refuse (default)  q = queue  a = ask")
                try:
                    resp = input("[commit-policy] choose [r/q/a] (Enter = r): ").strip().lower()
                except EOFError:
                    resp = ""
                on_commit = "queue" if resp.startswith("q") else ("ask" if resp.startswith("a") else "refuse")
        if not args.auto:
            try:
                resp = input(f"\n[{role_label}] dispatch this envelope? [y/N] ").strip().lower()
            except EOFError:
                resp = "n"
            if resp != "y":
                print(f"[{role_label}] cancelled (re-run with a tightened prompt to revise).")
                return 1
        print(f"\n[{role_label}] dispatching (on_commit={on_commit})...\n")
        if args.runs and args.runs > 1:
            if not proposal.writable_paths:
                print("ERROR: --runs requires the proposal to declare writable_paths")
                return 2
            from boundary.fielding_coach import dispatch_best_of_k
            bres = dispatch_best_of_k(
                proposal, workspace=workspace, client=args.client, model=args.model,
                verbose=args.verbose, on_commit=on_commit, commit_allowlist=commit_allowlist,
                runs=args.runs, mode=args.mode, select_margin=args.select_margin,
                judge_model=args.judge_model, headless_fallback=args.headless_fallback,
            )
            _print_best_of_k(bres, args.runs)
            return 0
        result = dispatch(
            proposal, workspace=workspace, client=args.client, model=args.model,
            verbose=args.verbose, on_commit=on_commit, commit_allowlist=commit_allowlist,
        )
        print("\n=== final ===")
        print(result.loop_result.final_message.content or "(no content)")
        print(f"\n[envelope: writes={result.writes_executed}/{proposal.max_writes} attempted={result.writes_attempted} appends={result.appends_executed} external={result.external_calls} halted={result.halted_for_ambiguity}]")
        # auto Third Umpire
        from boundary.third_umpire import ThirdUmpire
        # find latest transcript by mtime
        tx_dir = Path.home() / ".boundary" / "transcripts"
        latest = max(tx_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        print(f"\n[third-umpire] {latest}")
        report = ThirdUmpire.grade(latest)
        print(report.markdown())
        return 0 if report.verdict != "FAIL" else 2

    if args.cmd == "overlays":
        from boundary.overlay import list_overlays
        if args.overlays_cmd == "list":
            paths = list_overlays()
            if not paths:
                print("(no overlays found)")
                return 0
            for path in paths:
                ov = Overlay.load(str(path))
                print(f"{ov.name:12s} {path}")
            return 0
        if args.overlays_cmd == "show":
            ov = Overlay.load(args.name_or_path)
            print(f"name:              {ov.name}")
            print(f"path:              {ov.path}")
            print(f"default_workspace: {ov.workspace_or(None) if ov.default_workspace else '-'}")
            print(f"enable_clawpilot:  {ov.enable_clawpilot}")
            print(f"roles:             {', '.join(sorted(ov.roles)) or '-'}")
            return 0

    if args.cmd == "schedule":
        from boundary.schedule import ScheduleConfig, parse_schedule
        from boundary import scheduler as _lc
        if args.schedule_cmd == "install":
            installed = _lc.install(args.path)
            print(f"[ok] installed {installed}")
            print("    Logs: ~/.boundary/scheduler-logs/")
            return 0
        if args.schedule_cmd == "uninstall":
            out = _lc.uninstall(args.name)
            print(f"[ok] removed {out}")
            return 0
        if args.schedule_cmd == "list":
            paths = _lc.list_installed()
            if not paths:
                print("(no schedules installed)")
                return 0
            for p_ in paths:
                print(f"  {p_.name}")
            return 0
        if args.schedule_cmd == "validate":
            cfg = ScheduleConfig.load(args.path)
            parsed = parse_schedule(cfg.schedule)
            print(f"name:           {cfg.name}")
            print(f"persona:        {cfg.persona}")
            print(f"workspace:      {cfg.workspace}")
            print(f"schedule:       {cfg.schedule}  -> {parsed}")
            print(f"writable_paths: {cfg.rendered_writable_paths()}")
            print(f"max_writes:     {cfg.max_writes}  min: {cfg.min_writes}  iters: {cfg.max_iters}")
            print(f"staging:        require={cfg.require_staging}  max_unstaged_reads={cfg.max_unstaged_reads}")
            print(f"caps:           ${cfg.max_dollars}  {cfg.max_wall_seconds}s  in/out {cfg.max_input_tokens}/{cfg.max_output_tokens}")
            print(f"on_ambiguity:   {cfg.on_ambiguity}")
            print(f"on_commit:      {cfg.on_commit}  allowlist={cfg.commit_allowlist}")
            cp_errs = cfg.validate_commit_policy()
            if cp_errs:
                print("commit-policy errors:")
                for e in cp_errs:
                    print(f"  - {e}")
                return 2
            return 0
        return 1

    if args.cmd == "schedule-run":
        from boundary.schedule import ScheduleConfig
        from boundary.headless import run_headless
        cfg = ScheduleConfig.load(args.path)
        if not cfg.enabled:
            print(f"[skip] {cfg.name} is disabled")
            return 0
        print(f"[run] {cfg.name} ({cfg.persona} @ {cfg.workspace})")
        out = run_headless(cfg, verbose=args.verbose)
        print(f"[done] run_id={out['run_id']} stop={out['stop_reason']} umpire={out['third_umpire_verdict']} "
              f"writes={out['writes']} ${out['dollars']:.4f} {out['wall_seconds']:.1f}s")
        if out.get("review_id"):
            print(f"[review] queued as review_id={out['review_id']} — see `boundary review-queue list`")
        if out.get("event_path"):
            print(f"[event] {out['event_path']}")
        if out.get("error"):
            print(f"[error] {out['error'][:500]}")
            return 2
        return 0

    if args.cmd == "pipeline":
        from boundary.pipeline import PipelineConfig
        from boundary.schedule import parse_schedule
        from boundary import scheduler as _lc
        if args.pipeline_cmd == "install":
            installed = _lc.install_pipeline(args.path)
            print(f"[ok] installed {installed}")
            print("    Logs: ~/.boundary/scheduler-logs/")
            return 0
        if args.pipeline_cmd == "uninstall":
            out = _lc.uninstall(args.name)
            print(f"[ok] removed {out}")
            return 0
        if args.pipeline_cmd == "list":
            paths = _lc.list_installed()
            if not paths:
                print("(no schedules or pipelines installed)")
                return 0
            for p_ in paths:
                print(f"  {p_.name}")
            return 0
        if args.pipeline_cmd == "validate":
            cfg = PipelineConfig.load(args.path)
            print(f"name:           {cfg.name}")
            print(f"workspace:      {cfg.workspace}")
            if cfg.schedule:
                print(f"schedule:       {cfg.schedule}  -> {parse_schedule(cfg.schedule)}")
            else:
                print("schedule:       (manual)")
            print(f"stop_on:        {cfg.stop_on}")
            print(f"planning:       {'enabled' if cfg.planning.enabled else 'disabled'}")
            if cfg.planning.enabled:
                print(f"  output_path:  {cfg.planning.output_path}")
                print(f"  max_writes:   {cfg.planning.max_writes}  min: {cfg.planning.min_writes}  iters: {cfg.planning.max_iters}")
                print(f"  on_commit:    {cfg.planning.on_commit}  on_taint={cfg.planning.on_taint}")
            print(f"steps:          {len(cfg.steps)}")
            for idx, step in enumerate(cfg.steps, start=1):
                step_cfg = cfg.to_schedule_config(step)
                print(f"  {idx}. {step.name} ({step.persona})")
                print(f"     writable_paths: {step_cfg.rendered_writable_paths()}")
                print(f"     max_writes:     {step_cfg.max_writes}  min: {step_cfg.min_writes}  iters: {step_cfg.max_iters}")
                print(f"     on_ambiguity:   {step_cfg.on_ambiguity}")
                print(f"     on_commit:      {step_cfg.on_commit}  allowlist={step_cfg.commit_allowlist}")
            errs = cfg.validate()
            if errs:
                print("pipeline errors:")
                for e in errs:
                    print(f"  - {e}")
                return 2
            return 0
        return 1

    if args.cmd == "pipeline-run":
        from boundary.pipeline import PipelineConfig, run_pipeline
        cfg = PipelineConfig.load(args.path)
        if not cfg.enabled:
            print(f"[skip] {cfg.name} is disabled")
            return 0
        errs = cfg.validate()
        if errs:
            print("pipeline errors:")
            for e in errs:
                print(f"  - {e}")
            return 2
        print(f"[pipeline] {cfg.name} ({len(cfg.steps)} steps @ {cfg.workspace})")
        out = run_pipeline(cfg, verbose=args.verbose)
        planning = out.get("planning")
        if planning:
            print(
                f"[squad-plan] run_id={planning['run_id']} "
                f"stop={planning['stop_reason']} umpire={planning['third_umpire_verdict']} "
                f"writes={planning['writes']} ${planning['dollars']:.4f} "
                f"{planning['wall_seconds']:.1f}s"
            )
            if planning.get("plan_path"):
                print(f"[squad-plan] {planning['plan_path']}")
            if planning.get("error"):
                print(f"[error] {planning['error'][:500]}")
        for step in out["steps"]:
            print(
                f"[step] {step['step']} ({step['persona']}) "
                f"run_id={step['run_id']} stop={step['stop_reason']} "
                f"umpire={step['third_umpire_verdict']} writes={step['writes']} "
                f"${step['dollars']:.4f} {step['wall_seconds']:.1f}s"
            )
            if step.get("review_id"):
                print(f"[review] {step['step']} queued as review_id={step['review_id']} — see `boundary review-queue list`")
            if step.get("event_path"):
                print(f"[event] {step['event_path']}")
            if step.get("error"):
                print(f"[error] {step['error'][:500]}")
        print(f"[done] pipeline={out['pipeline']} stop={out['stop_reason']} {out['wall_seconds']:.1f}s")
        failed = out.get("stop_reason") == "planning_failed" or any(step.get("error") for step in out["steps"])
        return 2 if failed else 0

    if args.cmd == "history":
        from boundary.history import History
        from boundary.third_umpire import downgrade_tags
        import datetime as _dt, json as _json
        h = History()
        rows = h.list_runs(limit=args.limit, schedule_name=args.schedule)
        if not rows:
            print("(no runs yet)")
            return 0
        for r in rows:
            ts = _dt.datetime.fromtimestamp(r["started_at"]).strftime("%Y-%m-%d %H:%M")
            verdict = (r["third_umpire_verdict"] or "-")
            try:
                summary = _json.loads(r["third_umpire_summary_json"] or "{}")
            except (ValueError, TypeError):
                summary = {}
            tags = downgrade_tags(
                require_staging=summary.get("require_staging"),
                on_commit=summary.get("on_commit"),
                on_taint=summary.get("on_taint"),
            )
            downgrade = f"  downgrade={','.join(tags)}" if tags else ""
            print(f"  {r['id']:4d}  {ts}  {r['schedule_name'] or '(adhoc)':30s} {r['persona'] or '-':10s} "
                  f"stop={r['stop_reason']:14s} umpire={verdict:5s} "
                  f"writes={r['writes_executed']:2d} ${r['estimated_dollars'] or 0:.4f} {r['wall_seconds'] or 0:.0f}s{downgrade}")
        return 0

    if args.cmd == "review-queue":
        from boundary.history import History
        import datetime as _dt, json as _json
        h = History()
        if getattr(args, "rq_cmd", None) == "resolve":
            h.resolve_review(args.id, args.resolution)
            print(f"[ok] resolved review {args.id}")
            return 0
        rows = h.list_open_reviews()
        if not rows:
            print("(no open reviews)")
            return 0
        for r in rows:
            ts = _dt.datetime.fromtimestamp(r["queued_at"]).strftime("%Y-%m-%d %H:%M")
            opts = _json.loads(r["options_json"] or "[]")
            print(f"  #{r['id']}  {ts}  {r['schedule_name']} / {r['persona']}")
            print(f"      Q: {r['question'][:300]}")
            if opts:
                print(f"      options: {opts}")
            print(f"      transcript: {r['transcript_path']}")
            print(f"      resolve: boundary review-queue resolve {r['id']} 'your note'")
        return 0

    if args.cmd == "third-umpire":
        from boundary.third_umpire import ThirdUmpire
        report = ThirdUmpire.grade(args.transcript)
        print(report.markdown())
        return 0 if report.verdict != "FAIL" else 2

    if args.cmd == "taint":
        from boundary.taint import TaintStore
        store = TaintStore.load(args.workspace)
        if args.clear:
            store.clear()
            print(f"cleared taint ledger for {args.workspace}")
        else:
            print(store.render())
        return 0

    if args.cmd == "selftest":
        from boundary.selftest import run_selftest
        return run_selftest()

    if args.cmd == "copilot":
        from boundary.clients.copilot import (
            APPS_JSON_PATH, CopilotClient, _load_oauth_token_from_disk, device_code_login,
        )
        import httpx
        if args.copilot_cmd == "login":
            device_code_login()
            return 0
        if args.copilot_cmd == "status":
            tok = _load_oauth_token_from_disk()
            print(f"apps.json: {APPS_JSON_PATH} {'(present)' if APPS_JSON_PATH.exists() else '(missing)'}")
            print(f"oauth token: {'present' if tok else 'missing'}")
            if tok:
                try:
                    c = CopilotClient()
                    t = c._refresh_copilot_token()
                    print(f"copilot token: ok (len={len(t)}, expires={c._copilot_token_expires})")
                except Exception as e:
                    print(f"copilot token: FAILED — {e}")
            return 0
        if args.copilot_cmd == "models":
            c = CopilotClient()
            t = c._refresh_copilot_token()
            r = httpx.get(
                "https://api.githubcopilot.com/models",
                headers={
                    "Authorization": f"Bearer {t}",
                    "Editor-Version": c.editor_version,
                    "Copilot-Integration-Id": c.integration_id,
                },
                timeout=30.0,
            )
            data = r.json()
            for m in data.get("data", []):
                print(f"  {m.get('id'):40s}  {m.get('name', '')}")
            return 0

    if args.cmd == "run":
        overlay = Overlay.load(args.overlay) if args.overlay else None
        workspace = overlay.workspace_or(args.workspace) if overlay else args.workspace
        persona_path = args.persona
        extra_system = None
        enable_clawpilot = args.clawpilot
        if overlay:
            extra_system = overlay.extra_system
            enable_clawpilot = enable_clawpilot or overlay.enable_clawpilot
            if args.role:
                persona_path = str(overlay.resolve_role(args.role))
        if args.role and not overlay:
            print("ERROR: --role requires --overlay")
            return 2

        def make_agent():
            if persona_path:
                from boundary.adapters.clawpilot import load_persona
                return load_persona(
                    charter=persona_path,
                    workspace=workspace,
                    name=args.role or (args.name if args.name != "agent" else None),
                    client=args.client,
                    model=args.model,
                    enable_fs=not args.no_fs,
                    enable_shell=not args.no_shell,
                    enable_web=args.web,
                    enable_clawpilot=enable_clawpilot,
                    extra_system=extra_system,
                    max_iters=args.max_iters,
                    sandbox_driver=args.sandbox_driver,
                    egress_allowlist=args.egress_allow,
                )
            if args.system_file:
                system_prompt = Path(args.system_file).expanduser().read_text(encoding="utf-8")
            elif args.system:
                system_prompt = args.system
            else:
                system_prompt = "You are a helpful coding agent. Use tools to inspect and modify files. Be concise."
            if extra_system:
                system_prompt += "\n\n" + extra_system
            client_kwargs = {"model": args.model} if args.model else {}
            return Agent(
                name=args.name,
                system_prompt=system_prompt,
                workspace=workspace,
                client=args.client,
                client_kwargs=client_kwargs,
                enable_fs=not args.no_fs,
                enable_shell=not args.no_shell,
                enable_web=args.web,
                enable_clawpilot=enable_clawpilot,
                max_iters=args.max_iters,
                sandbox_driver=args.sandbox_driver,
                egress_allowlist=args.egress_allow,
            )

        # Best-of-K branch (feature C): fan out K runs and select a winner.
        if args.runs and args.runs > 1:
            if not args.envelope_writable:
                print("ERROR: --runs K requires --envelope-writable (best-of-K templates and promotes a path)")
                return 2
            from boundary.envelope import Envelope
            from boundary.multirun import run_best_of_k
            from boundary.clients import make_client
            from boundary.transcript import Transcript
            from boundary.history import History
            probe = make_agent()
            on_commit, commit_allowlist = _prompt_commit_policy(probe, args.on_commit, args.commit_allow)
            probe.close()
            base_env = Envelope(
                writable_paths=args.envelope_writable,
                max_writes=args.envelope_max_writes,
                min_writes=args.envelope_min_writes,
                max_appends=args.envelope_max_appends,
                max_external=args.envelope_max_external,
                require_staging=not args.no_staging_gate,
                max_unstaged_reads=args.envelope_max_unstaged_reads,
                max_input_tokens=args.envelope_max_input_tokens,
                max_output_tokens=args.envelope_max_output_tokens,
                max_dollars=args.envelope_max_dollars,
                max_wall_seconds=args.envelope_max_wall_seconds,
                on_commit=on_commit,
                commit_allowlist=commit_allowlist,
                on_taint=args.on_taint,
            )
            k = args.runs

            def factory(run_index):
                a = make_agent()
                if a.transcript:
                    a.transcript.close()
                a.transcript = Transcript(agent_name=f"{args.role or args.name}-run{run_index}")
                return a

            def temp_for(run_index):
                if k <= 1:
                    return {}
                return {"temperature": round(0.2 + 0.4 * (run_index - 1) / (k - 1), 3)}

            judge_client = make_client(args.client, model=(args.judge_model or args.model)) \
                if (args.judge_model or args.model or args.client) else None
            hist = History()
            try:
                bres = run_best_of_k(
                    agent_factory=factory, base_envelope=base_env, task=args.task,
                    workspace_root=Path(workspace).expanduser(), k=k, chat_kwargs_for=temp_for,
                    judge_client=judge_client, mode=args.mode, select_margin=args.select_margin,
                    headless_fallback=args.headless_fallback, history=hist, verbose=args.verbose,
                )
            finally:
                hist.close()
            _print_best_of_k(bres, k)
            return 0

        agent = make_agent()
        try:
            if args.envelope_writable:
                from boundary.envelope import Envelope, EnvelopeRunner
                on_commit, commit_allowlist = _prompt_commit_policy(
                    agent, args.on_commit, args.commit_allow,
                )
                env = Envelope(
                    writable_paths=args.envelope_writable,
                    max_writes=args.envelope_max_writes,
                    min_writes=args.envelope_min_writes,
                    max_appends=args.envelope_max_appends,
                    max_external=args.envelope_max_external,
                    require_staging=not args.no_staging_gate,
                    max_unstaged_reads=args.envelope_max_unstaged_reads,
                    max_input_tokens=args.envelope_max_input_tokens,
                    max_output_tokens=args.envelope_max_output_tokens,
                    max_dollars=args.envelope_max_dollars,
                    max_wall_seconds=args.envelope_max_wall_seconds,
                    on_commit=on_commit,
                    commit_allowlist=commit_allowlist,
                    on_taint=args.on_taint,
                )
                runner = EnvelopeRunner(agent, env)
                result = runner.run(args.task, verbose=args.verbose)
                print("\n=== final ===")
                print(result.loop_result.final_message.content or "(no content)")
                print(f"\n[iterations={result.loop_result.iterations} stop={result.loop_result.stop_reason} wall={result.wall_seconds:.1f}s]")
                print(f"[envelope: writes={result.writes_executed}/{env.max_writes} attempted={result.writes_attempted} appends={result.appends_executed}/{env.max_appends} external={result.external_calls}]")
                print(f"[spend: in={result.input_tokens:,} (cached={result.cached_input_tokens:,}) out={result.output_tokens:,} est=${result.estimated_dollars:.4f}]")
                if agent.transcript:
                    print(f"[transcript: {agent.transcript.path}]")
                    print(f"[grade with: boundary third-umpire {agent.transcript.path}]")
                return 0
            result = agent.run(args.task, verbose=args.verbose)
            print("\n=== final ===")
            print(result.final_message.content or "(no content)")
            print(f"\n[iterations={result.iterations} stop={result.stop_reason}]")
            if agent.transcript:
                print(f"[transcript: {agent.transcript.path}]")
            return 0
        finally:
            agent.close()
    return 1


if __name__ == "__main__":
    sys.exit(main())
