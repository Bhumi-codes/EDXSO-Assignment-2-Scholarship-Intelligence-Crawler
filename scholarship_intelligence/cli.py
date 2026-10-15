"""Command-line entry points for live crawling and the local dashboard."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from scholarship_intelligence import pipeline
from scholarship_intelligence.settings import DATABASE_PATH, PROJECT_ROOT
from scholarship_intelligence.storage import initialize_database


def _emit(result, output=None):
    content = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if output is not None:
        path = (PROJECT_ROOT / output).resolve()
        if not path.is_relative_to((PROJECT_ROOT / "data/runtime").resolve()):
            raise ValueError("Save reports inside data/runtime.")
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            replaceable = {"seed_workflow", "live_source_workflows"}
            if result.get("operation") not in replaceable:
                raise FileExistsError("An immutable report already exists at this output path.")
            previous = json.loads(path.read_bytes())
            if (not isinstance(previous, dict)
                    or previous.get("operation") != result.get("operation")
                    or previous.get("seed_id") != result.get("seed_id")):
                raise ValueError("Existing output is not a workflow report for this seed; choose another path.")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             delete=False, suffix=".tmp") as stream:
                temporary = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    summary = {key: value for key, value in result.items() if key != "offerings"}
    if result.get("operation") == "live_source_workflows":
        summary["sources"] = [{key: value for key, value in source.items()
                               if key not in {"offerings", "availability_refresh"}}
                              for source in result["sources"]]
    print(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False))


def _failed(result):
    return any(source.get("stage") == "failed" or source.get("counts", {}).get("failed")
               for source in result.get("sources", [result]))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Scholarship Intelligence: live crawling, Python extraction and source evidence.")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start", help="Open saved results immediately; crawl live from the dashboard.")
    start.add_argument("--no-crawl", action="store_true", help="Compatibility option: start now opens saved results by default.")
    start.add_argument("--crawl", action="store_true", help="Explicitly crawl before opening the dashboard.")
    start.add_argument("--port", type=int, default=8501)
    batch = commands.add_parser("workflow-all", help="Run the complete live workflow for configured sources.")
    batch.add_argument("--seed-id", action="append", help="Repeat to select sources; default: all five enabled sources.")
    batch.add_argument("--existing-only", action="store_true", help="Recheck stored identities without importing new discoveries.")
    one = commands.add_parser("workflow", help="Run one source from discovery through scoring and storage.")
    one.add_argument("--seed-id", required=True)
    one.add_argument("--resume", action="store_true", help="Recover a stopped run using its preserved live-crawl snapshots.")
    for command in (start, batch, one):
        command.add_argument("--max-pages", type=int, default=1000, help="Safety budget per source (1-1000).")
    for command in (batch, one):
        command.add_argument("--output", type=Path, help="Optional JSON report under data/runtime.")
    commands.add_parser("init-db", help="Initialize SQLite without deleting existing records.")
    replay = commands.add_parser("replay-official-notices", help="Fetch historical official revisions and evaluate expiry in isolation.")
    replay.add_argument("--output", type=Path)
    inspect = commands.add_parser("inspect", help="Show a saved scholarship and its provenance.")
    inspect.add_argument("--scholarship-id", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "replay-official-notices":
            from scholarship_intelligence.official_replay import run_official_replay
            result = run_official_replay()
            _emit(result, args.output)
            return 0 if result["target_met"] else 1
        if args.command == "inspect":
            _emit(pipeline.inspect_scholarship(args.scholarship_id))
            return 0
        initialize_database(DATABASE_PATH)
        if args.command == "init-db":
            _emit({"database": str(DATABASE_PATH)})
            return 0
        if args.command == "start":
            if not 1 <= args.port <= 65535:
                raise ValueError("Port must be between 1 and 65535.")
            if args.crawl and not args.no_crawl:
                print("Running live discovery and extraction. The first crawl may take several minutes.", flush=True)
                result = pipeline.run_live_workflows(max_pages=args.max_pages)
                _emit(result, Path("data/runtime/live-source-workflows.json"))
                if _failed(result):
                    print("Some sources failed. Completed records are retained; inspect the report or retry from the dashboard.", file=sys.stderr)
            return subprocess.call([sys.executable, "-m", "streamlit", "run",
                                    str(PROJECT_ROOT / "scholarship_intelligence/dashboard.py"),
                                    "--server.port", str(args.port)], cwd=PROJECT_ROOT)
        if args.command == "workflow-all":
            result = pipeline.run_live_workflows(args.seed_id, max_pages=args.max_pages,
                                                 existing_only=args.existing_only)
            output = args.output or Path("data/runtime/live-source-workflows.json")
        else:
            result = pipeline.run_seed_workflow(args.seed_id, resume=args.resume, reextract=False,
                                                 limit=1000, max_pages=args.max_pages)
            output = args.output or Path("data/runtime") / (args.seed_id + "-workflow.json")
        _emit(result, output)
        return 1 if _failed(result) else 0
    except KeyboardInterrupt:
        print("Stopped. Completed records and source snapshots are retained.", file=sys.stderr)
        return 130
    except (ValueError, LookupError, TypeError, RuntimeError, OSError, sqlite3.Error) as error:
        print(json.dumps({"error_type": type(error).__name__, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
