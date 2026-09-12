#!/usr/bin/env python3
"""
Sims 4 Career Forge - entry point.

    python main.py                          open the web interface
    python main.py --gui                    desktop window instead
    python main.py --idea "a ghost lawyer"  one-shot, no interface
    python main.py --spec career.spec.json  rebuild a saved career, offline
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from forge.env import load_env, find_env_file  # noqa: E402


def run_cli(args) -> int:
    from forge.builder import build_career, BuildError
    from forge.config import Settings
    from forge.llm import OpenRouterClient, generate_career, LLMError
    from forge.schema import CareerSpec, SpecError

    settings = Settings.load()
    key = args.api_key or settings.resolved_api_key
    # A key is only needed to draft a new career. Rebuilding a saved spec is
    # entirely offline.
    if not args.spec and not key:
        print("error: no API key.\n"
              "  Put OPENROUTER_API_KEY=... in a .env next to main.py,\n"
              "  or pass --api-key.", file=sys.stderr)
        return 2

    try:
        if args.spec:
            spec = CareerSpec.from_json(Path(args.spec).read_text(encoding="utf-8"))
            print(f"Loaded spec: {spec.name}")
        else:
            result = generate_career(
                OpenRouterClient(key), args.idea, args.model or settings.model,
                max_attempts=args.attempts, temperature=args.temperature,
                on_progress=lambda m: print(f"  {m}"),
            )
            spec = result.spec

        build = build_career(spec, args.output or settings.output_dir,
                             on_progress=lambda m: print(f"  {m}"))
        print(f"\nBuilt: {build.output_dir}")
        for warning in build.warnings:
            print(f"  note: {warning}")
        print(f"\nRead {build.report_path.name} before installing.")
        return 0
    except (LLMError, BuildError, SpecError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _find_spec(given: str | None, output_dir: str) -> Path | None:
    """
    Locate a career spec to work from.

    Accepts a path to the .spec.json, a path to a built mod folder, or
    nothing at all - in which case the most recently built career in the
    output folder is used. Hunting for a file path is not a thing anyone
    should have to do to run a diagnostic.
    """
    if given:
        candidate = Path(given)
        if candidate.is_file():
            return candidate
        if candidate.is_dir():
            found = sorted(candidate.glob("*.spec.json"))
            if found:
                return found[0]
        return None

    root = Path(output_dir)
    if not root.is_dir():
        return None
    specs = list(root.glob("*/*.spec.json")) + list(root.glob("*.spec.json"))
    if not specs:
        return None
    # Most recently built wins, which is almost always the one that crashed.
    return max(specs, key=lambda p: p.stat().st_mtime)


def run_bisect(args) -> int:
    """Emit both bisection series so a load crash can be pinned down."""
    from forge.bisect import build_bisect
    from forge.config import Settings
    from forge.schema import CareerSpec, SpecError

    settings = Settings.load()

    spec_path = _find_spec(args.spec, args.output or settings.output_dir)
    if spec_path is None:
        print("error: could not find a career to bisect.\n\n"
              "Build a career first (python main.py), then run --bisect again.\n"
              f"Looked in: {args.output or settings.output_dir}\n\n"
              "Or point at one directly:\n"
              "    python main.py --bisect --spec path\\to\\name.spec.json",
              file=sys.stderr)
        return 2

    print(f"Using {spec_path}")
    try:
        spec = CareerSpec.from_json(spec_path.read_text(encoding="utf-8"))
    except (SpecError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    out = args.output or settings.output_dir
    for generic in (False, True):
        label = "generic tuning type" if generic else "career-specific types"
        print(f"\nBuilding series with {label}:")
        result = build_bisect(spec, out, generic_type=generic,
                              on_progress=lambda m: print(f"  {m}"))
        print(f"  -> {result.output_dir}")

    print("\nRead HOW_TO_USE.txt in either folder. Test one package at a")
    print("time and report which number first crashes, for both series.")
    return 0


def layout_error(here: Path) -> str:
    return (
        "error: project files are in the wrong place.\n\n"
        "Expected layout, next to main.py:\n"
        "    main.py\n"
        "    forge/   ids.py dbpf.py stbl.py schema.py llm.py env.py\n"
        "             tuning.py script_mod.py builder.py config.py\n"
        "    web/     server.py  templates/index.html\n"
        "    gui/     app.py\n\n"
        f"Looked in: {here}\n"
        "If every .py file is loose in one folder, move them into the\n"
        "subfolders shown above."
    )


def run_web(args) -> int:
    here = Path(__file__).parent
    if not (here / "web" / "templates" / "index.html").exists():
        print(layout_error(here), file=sys.stderr)
        return 2
    try:
        from web.server import run
    except ImportError as exc:
        if (getattr(exc, "name", "") or "").split(".")[0] == "flask":
            print("error: flask is not installed.\n"
                  "    pip install -r requirements.txt", file=sys.stderr)
            return 2
        print(f"error: could not start the web interface: {exc}\n", file=sys.stderr)
        traceback.print_exc()
        return 2

    run(host=args.host, port=args.port, open_browser=not args.no_browser)
    return 0


def run_gui() -> int:
    here = Path(__file__).parent
    try:
        from gui.app import main as gui_main
    except ImportError as exc:
        if (getattr(exc, "name", "") or "").split(".")[0] == "dearpygui":
            print("error: dearpygui is not installed.\n"
                  "    pip install dearpygui", file=sys.stderr)
            return 2
        if not (here / "gui" / "app.py").exists():
            print(layout_error(here), file=sys.stderr)
            return 2
        print(f"error: could not start the desktop window: {exc}\n", file=sys.stderr)
        traceback.print_exc()
        return 2
    gui_main()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Sims 4 career mods")
    parser.add_argument("--idea", help="career idea; runs without an interface")
    parser.add_argument("--spec", help="build from a saved .spec.json")
    parser.add_argument("--model", help="OpenRouter model id")
    parser.add_argument("--api-key", help="OpenRouter key (prefer a .env file)")
    parser.add_argument("--output", help="build folder")
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--temperature", type=float, default=0.8)
    parser.add_argument("--gui", action="store_true", help="desktop window")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7878)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--bisect", action="store_true",
                        help="emit test packages to isolate a load crash")
    args = parser.parse_args()

    found = load_env()
    if found:
        where = find_env_file()
        keys = ", ".join(sorted(found))
        print(f"Loaded .env from {where} ({keys})")

    if args.bisect:
        return run_bisect(args)
    if args.idea or args.spec:
        return run_cli(args)
    if args.gui:
        return run_gui()
    return run_web(args)


if __name__ == "__main__":
    sys.exit(main())
