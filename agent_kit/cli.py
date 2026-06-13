from __future__ import annotations
import argparse
import sys
from pathlib import Path

from agent_kit.agent import Agent


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser("agent-kit")
    sub = p.add_subparsers(dest="cmd", required=True)

    cp = sub.add_parser("copilot", help="Copilot backend utilities")
    cp_sub = cp.add_subparsers(dest="copilot_cmd", required=True)
    cp_sub.add_parser("login", help="device-code login for GitHub Copilot")
    cp_sub.add_parser("status", help="show current Copilot auth status")
    cp_sub.add_parser("models", help="list available Copilot models")

    stark = sub.add_parser("stark", help="captain layer — propose & dispatch an envelope from a loose prompt")
    stark.add_argument("prompt", help="loose user prompt to translate into an envelope")
    stark.add_argument("--workspace", required=True)
    stark.add_argument("--auto", action="store_true", help="skip approval gate; dispatch the proposal immediately")
    stark.add_argument("--client", default="copilot")
    stark.add_argument("--model", default=None)
    stark.add_argument("--verbose", "-v", action="store_true")

    # Phase 3: schedules
    sched_inst = sub.add_parser("schedule", help="install/uninstall/list scheduled headless runs (launchd)")
    sched_sub = sched_inst.add_subparsers(dest="schedule_cmd", required=True)
    si = sched_sub.add_parser("install", help="install a schedule YAML as a launchd LaunchAgent")
    si.add_argument("path", help="path to schedule.yaml")
    su = sched_sub.add_parser("uninstall", help="remove an installed schedule by name")
    su.add_argument("name")
    sched_sub.add_parser("list", help="list installed agent-kit schedules")
    sv = sched_sub.add_parser("validate", help="parse a schedule YAML and print what would be installed")
    sv.add_argument("path")

    srun = sub.add_parser("schedule-run", help="execute a schedule headlessly (used by launchd, also runnable manually)")
    srun.add_argument("path", help="path to schedule.yaml")
    srun.add_argument("--verbose", "-v", action="store_true")

    hist = sub.add_parser("history", help="show recent runs")
    hist.add_argument("--limit", type=int, default=20)
    hist.add_argument("--schedule", default=None)

    rq = sub.add_parser("review-queue", help="show ambiguity halts waiting for human input")
    rq_sub = rq.add_subparsers(dest="rq_cmd")
    rq_list = rq_sub.add_parser("list", help="list open review items (default)")
    rq_resolve = rq_sub.add_parser("resolve")
    rq_resolve.add_argument("id", type=int)
    rq_resolve.add_argument("resolution", help="free-text resolution note")

    fury = sub.add_parser("fury", help="grade a transcript against envelope eval")
    fury.add_argument("transcript", help="path to a JSONL transcript")

    run = sub.add_parser("run", help="run an agent on a task")
    run.add_argument("--name", default="agent")
    run.add_argument("--system", help="system prompt string")
    run.add_argument("--system-file", help="read system prompt from file")
    run.add_argument("--task", required=True)
    run.add_argument("--workspace", default=".")
    run.add_argument("--client", default="copilot", choices=["copilot", "together", "anthropic"])
    run.add_argument("--model", help="override model name")
    run.add_argument("--max-iters", type=int, default=25)
    run.add_argument("--no-shell", action="store_true")
    run.add_argument("--no-fs", action="store_true")
    run.add_argument("--persona", help="path to a persona charter.md to load as system prompt (Clawpilot adapter)")
    run.add_argument("--web", action="store_true", help="enable fetch_url tool")
    run.add_argument("--clawpilot", action="store_true", help="enable skill_load/charter_load/workiq bridge tools")
    run.add_argument("--envelope-writable", action="append", default=[], help="add a writable path/glob to the envelope (repeat for multiple). Activates envelope mode.")
    run.add_argument("--envelope-max-writes", type=int, default=10)
    run.add_argument("--envelope-min-writes", type=int, default=1)
    run.add_argument("--envelope-max-external", type=int, default=20)
    run.add_argument("--envelope-max-input-tokens", type=int, default=500_000)
    run.add_argument("--envelope-max-output-tokens", type=int, default=50_000)
    run.add_argument("--envelope-max-dollars", type=float, default=None)
    run.add_argument("--envelope-max-wall-seconds", type=float, default=900.0)
    run.add_argument("--verbose", "-v", action="store_true")

    args = p.parse_args(argv)

    if args.cmd == "stark":
        from agent_kit.stark import Stark, dispatch
        from agent_kit.fury import Fury
        s = Stark(client=args.client, model=args.model or "claude-sonnet-4.5")
        print("[stark] proposing envelope...")
        proposal = s.propose(args.prompt, workspace_hint=args.workspace)
        print("\n" + proposal.to_markdown() + "\n")
        if proposal.clarifying_questions and not args.auto:
            print("[stark] BLOCKING clarifying questions present — answer them and re-run.")
            return 1
        if not args.auto:
            try:
                resp = input("\n[stark] dispatch this envelope? [y/N/edit] ").strip().lower()
            except EOFError:
                resp = "n"
            if resp == "edit":
                print("[stark] edit not implemented in CLI yet — re-run with a tightened prompt or use the Python API")
                return 1
            if resp != "y":
                print("[stark] cancelled.")
                return 1
        print("\n[stark] dispatching...\n")
        result = dispatch(proposal, workspace=args.workspace, client=args.client, model=args.model, verbose=args.verbose)
        print("\n=== final ===")
        print(result.loop_result.final_message.content or "(no content)")
        print(f"\n[envelope: writes={result.writes_executed}/{proposal.max_writes} attempted={result.writes_attempted} external={result.external_calls} halted={result.halted_for_ambiguity}]")
        # auto-Fury
        from agent_kit.fury import Fury
        # find latest transcript by mtime
        tx_dir = Path.home() / ".agent-kit" / "transcripts"
        latest = max(tx_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        print(f"\n[fury] {latest}")
        report = Fury.grade(latest)
        print(report.markdown())
        return 0 if report.verdict != "FAIL" else 2

    if args.cmd == "schedule":
        from agent_kit.schedule import ScheduleConfig, parse_schedule
        from agent_kit import launchd as _lc
        if args.schedule_cmd == "install":
            installed = _lc.install(args.path)
            print(f"[ok] installed {installed}")
            print("    Logs: ~/.agent-kit/launchd-logs/")
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
            print(f"caps:           ${cfg.max_dollars}  {cfg.max_wall_seconds}s  in/out {cfg.max_input_tokens}/{cfg.max_output_tokens}")
            print(f"on_ambiguity:   {cfg.on_ambiguity}")
            return 0
        return 1

    if args.cmd == "schedule-run":
        from agent_kit.schedule import ScheduleConfig
        from agent_kit.headless import run_headless
        cfg = ScheduleConfig.load(args.path)
        if not cfg.enabled:
            print(f"[skip] {cfg.name} is disabled")
            return 0
        print(f"[run] {cfg.name} ({cfg.persona} @ {cfg.workspace})")
        out = run_headless(cfg, verbose=args.verbose)
        print(f"[done] run_id={out['run_id']} stop={out['stop_reason']} fury={out['fury_verdict']} "
              f"writes={out['writes']} ${out['dollars']:.4f} {out['wall_seconds']:.1f}s")
        if out.get("review_id"):
            print(f"[review] queued as review_id={out['review_id']} — see `agent-kit review-queue list`")
        if out.get("error"):
            print(f"[error] {out['error'][:500]}")
            return 2
        return 0

    if args.cmd == "history":
        from agent_kit.history import History
        import datetime as _dt
        h = History()
        rows = h.list_runs(limit=args.limit, schedule_name=args.schedule)
        if not rows:
            print("(no runs yet)")
            return 0
        for r in rows:
            ts = _dt.datetime.fromtimestamp(r["started_at"]).strftime("%Y-%m-%d %H:%M")
            verdict = (r["fury_verdict"] or "-")
            print(f"  {r['id']:4d}  {ts}  {r['schedule_name'] or '(adhoc)':30s} {r['persona'] or '-':10s} "
                  f"stop={r['stop_reason']:14s} fury={verdict:5s} "
                  f"writes={r['writes_executed']:2d} ${r['estimated_dollars'] or 0:.4f} {r['wall_seconds'] or 0:.0f}s")
        return 0

    if args.cmd == "review-queue":
        from agent_kit.history import History
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
            print(f"      resolve: agent-kit review-queue resolve {r['id']} 'your note'")
        return 0

    if args.cmd == "fury":
        from agent_kit.fury import Fury
        report = Fury.grade(args.transcript)
        print(report.markdown())
        return 0 if report.verdict != "FAIL" else 2

    if args.cmd == "copilot":
        from agent_kit.clients.copilot import (
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
        if args.persona:
            from agent_kit.adapters.clawpilot import load_persona
            agent = load_persona(
                charter=args.persona,
                workspace=args.workspace,
                name=args.name if args.name != "agent" else None,
                client=args.client,
                model=args.model,
                enable_fs=not args.no_fs,
                enable_shell=not args.no_shell,
                enable_web=args.web,
                max_iters=args.max_iters,
            )
        else:
            if args.system_file:
                system_prompt = Path(args.system_file).expanduser().read_text(encoding="utf-8")
            elif args.system:
                system_prompt = args.system
            else:
                system_prompt = "You are a helpful coding agent. Use tools to inspect and modify files. Be concise."
            client_kwargs = {"model": args.model} if args.model else {}
            agent = Agent(
                name=args.name,
                system_prompt=system_prompt,
                workspace=args.workspace,
                client=args.client,
                client_kwargs=client_kwargs,
                enable_fs=not args.no_fs,
                enable_shell=not args.no_shell,
                enable_web=args.web,
                enable_clawpilot=args.clawpilot,
                max_iters=args.max_iters,
            )
        try:
            if args.envelope_writable:
                from agent_kit.envelope import Envelope, EnvelopeRunner
                env = Envelope(
                    writable_paths=args.envelope_writable,
                    max_writes=args.envelope_max_writes,
                    min_writes=args.envelope_min_writes,
                    max_external=args.envelope_max_external,
                    max_input_tokens=args.envelope_max_input_tokens,
                    max_output_tokens=args.envelope_max_output_tokens,
                    max_dollars=args.envelope_max_dollars,
                    max_wall_seconds=args.envelope_max_wall_seconds,
                )
                runner = EnvelopeRunner(agent, env)
                result = runner.run(args.task, verbose=args.verbose)
                print("\n=== final ===")
                print(result.loop_result.final_message.content or "(no content)")
                print(f"\n[iterations={result.loop_result.iterations} stop={result.loop_result.stop_reason} wall={result.wall_seconds:.1f}s]")
                print(f"[envelope: writes={result.writes_executed}/{env.max_writes} attempted={result.writes_attempted} external={result.external_calls}]")
                print(f"[spend: in={result.input_tokens:,} (cached={result.cached_input_tokens:,}) out={result.output_tokens:,} est=${result.estimated_dollars:.4f}]")
                if agent.transcript:
                    print(f"[transcript: {agent.transcript.path}]")
                    print(f"[grade with: agent-kit fury {agent.transcript.path}]")
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
