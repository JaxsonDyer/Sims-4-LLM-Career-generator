"""
Local web server for Career Forge.

Binds to 127.0.0.1 only. This is a single-user tool that holds an API key and
writes files anywhere on disk you point it, so it must never be reachable
from the network. There is no authentication because there is no remote
access; if you change the host, add auth first.

Generation and building run on background threads. The browser polls a job
endpoint for progress rather than holding a long request open, which keeps
the page responsive and survives a refresh mid-build.
"""

from __future__ import annotations

import io
import json
import threading
import traceback
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from flask import Flask, jsonify, request, send_file, render_template

from forge.builder import build_career, BuildError
from forge.config import Settings, default_mods_folder
from forge.llm import OpenRouterClient, generate_career, LLMError
from forge.schema import CareerSpec, SpecError

EXAMPLES = [
    "A cryptid hunter who chases things that probably do not exist",
    "Competitive sandwich artist, deadly serious about bread",
    "Deep sea welder with a fear of fish",
    "Ghost lawyer representing the recently deceased in property disputes",
    "Volcano insurance adjuster",
    "Professional queue stander for people too busy to wait",
    "Museum night guard who suspects the exhibits move",
]


@dataclass
class Job:
    """One unit of background work the browser can poll."""

    id: str
    kind: str                      # "generate" or "build"
    status: str = "running"        # running | done | error
    log: list[dict] = field(default_factory=list)
    error: str = ""
    spec: dict | None = None
    build: dict | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, message: str, level: str = "info") -> None:
        with self.lock:
            self.log.append({"level": level, "message": message})

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "id": self.id,
                "kind": self.kind,
                "status": self.status,
                "log": list(self.log),
                "error": self.error,
                "spec": self.spec,
                "build": self.build,
            }


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()

# Where each mod was most recently built. Without this, download and install
# guess at the default output folder and miss any build sent elsewhere.
BUILT: dict[str, Path] = {}
BUILT_LOCK = threading.Lock()


def _built_folder(mod_key: str, settings: Settings) -> Path | None:
    """Resolve a mod key to the folder it was built into, if it exists."""
    safe = "".join(c for c in mod_key if c.isalnum() or c == "_")
    if not safe:
        return None
    with BUILT_LOCK:
        known = BUILT.get(safe)
    if known and known.is_dir():
        return known
    # Fall back to the configured output folder, so a mod built in an earlier
    # session is still reachable after a restart.
    fallback = Path(settings.output_dir) / safe
    return fallback if fallback.is_dir() else None


def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    settings = Settings.load()

    def new_job(kind: str) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind)
        with JOBS_LOCK:
            JOBS[job.id] = job
            # Keep the job table from growing without bound across a long
            # session. Finished jobs older than the last 40 are dropped.
            if len(JOBS) > 40:
                for stale in list(JOBS)[:-40]:
                    JOBS.pop(stale, None)
        return job

    # -- pages ------------------------------------------------------------

    @app.get("/")
    def index():
        return render_template("index.html")

    # -- settings ---------------------------------------------------------

    @app.get("/api/settings")
    def get_settings():
        """Never returns the API key itself, only where it came from."""
        return jsonify({
            "model": settings.model,
            "temperature": settings.temperature,
            "max_attempts": settings.max_attempts,
            "output_dir": settings.output_dir,
            "mods_dir": settings.mods_dir or str(default_mods_folder()),
            "has_key": bool(settings.resolved_api_key),
            "key_source": settings.key_source,
            "models": settings.recent_models,
            "examples": EXAMPLES,
        })

    @app.post("/api/settings")
    def save_settings():
        data = request.get_json(silent=True) or {}
        if "model" in data:
            settings.model = str(data["model"]).strip() or settings.model
        if "temperature" in data:
            try:
                settings.temperature = max(0.0, min(1.5, float(data["temperature"])))
            except (TypeError, ValueError):
                pass
        if "max_attempts" in data:
            try:
                settings.max_attempts = max(1, min(5, int(data["max_attempts"])))
            except (TypeError, ValueError):
                pass
        if "output_dir" in data:
            settings.output_dir = str(data["output_dir"]).strip() or settings.output_dir
        if "mods_dir" in data:
            settings.mods_dir = str(data["mods_dir"]).strip()
        if data.get("api_key"):
            settings.api_key = str(data["api_key"]).strip()
        settings.save()
        return jsonify({"saved": True, "key_source": settings.key_source})

    @app.post("/api/models")
    def fetch_models():
        if not settings.resolved_api_key:
            return jsonify({"error": "No API key set."}), 400
        try:
            models = OpenRouterClient(settings.resolved_api_key).list_models()
        except LLMError as exc:
            return jsonify({"error": str(exc)}), 502
        settings.recent_models = models
        settings.save()
        return jsonify({"models": models})

    # -- generate ---------------------------------------------------------

    @app.post("/api/generate")
    def start_generate():
        data = request.get_json(silent=True) or {}
        idea = str(data.get("idea", "")).strip()
        if not idea:
            return jsonify({"error": "Describe a career first."}), 400
        if not settings.resolved_api_key:
            return jsonify({
                "error": "No OpenRouter key. Put OPENROUTER_API_KEY in a .env "
                         "file next to main.py, or paste one under Model."
            }), 400

        model = str(data.get("model") or settings.model).strip()
        try:
            temperature = float(data.get("temperature", settings.temperature))
            attempts = int(data.get("max_attempts", settings.max_attempts))
        except (TypeError, ValueError):
            return jsonify({"error": "Temperature and attempts must be numbers."}), 400

        job = new_job("generate")

        def work() -> None:
            try:
                client = OpenRouterClient(settings.resolved_api_key)
                result = generate_career(
                    client, idea, model,
                    max_attempts=attempts, temperature=temperature,
                    on_progress=lambda m: job.add(m),
                )
                job.spec = result.spec.to_dict()
                job.add(
                    f"Drafted {result.spec.name} in "
                    f"{result.attempts} attempt(s).", "good"
                )
                job.status = "done"
            except (LLMError, SpecError) as exc:
                job.error = str(exc)
                job.add(str(exc), "error")
                job.status = "error"
            except Exception:
                job.error = traceback.format_exc()
                job.add("Unexpected failure. See the server console.", "error")
                job.status = "error"

        threading.Thread(target=work, daemon=True).start()
        return jsonify({"job_id": job.id})

    # -- build ------------------------------------------------------------

    @app.post("/api/build")
    def start_build():
        data = request.get_json(silent=True) or {}
        raw_spec = data.get("spec")
        if not raw_spec:
            return jsonify({"error": "Nothing to build yet."}), 400

        try:
            spec = CareerSpec.from_dict(raw_spec)
        except SpecError as exc:
            return jsonify({"error": str(exc)}), 400

        problems = spec.validate()
        if problems:
            return jsonify({
                "error": "That career is not valid:\n"
                         + "\n".join(f"- {p}" for p in problems)
            }), 400

        output = str(data.get("output_dir") or settings.output_dir).strip()
        job = new_job("build")

        def work() -> None:
            try:
                result = build_career(spec, output, on_progress=lambda m: job.add(m))
                with BUILT_LOCK:
                    BUILT[spec.mod_key] = result.output_dir
                job.build = {
                    "mod_key": spec.mod_key,
                    "output_dir": str(result.output_dir),
                    "package": result.package_path.name,
                    "script": result.script_path.name,
                    "resources": result.resource_count,
                    "strings": result.string_count,
                    "warnings": result.warnings,
                    "command": f"forge.{spec.mod_key}",
                }
                job.add(f"Built to {result.output_dir}", "good")
                for warning in result.warnings:
                    job.add(warning, "warn")
                job.status = "done"
            except BuildError as exc:
                job.error = str(exc)
                job.add(str(exc), "error")
                job.status = "error"
            except Exception:
                job.error = traceback.format_exc()
                job.add("Unexpected failure. See the server console.", "error")
                job.status = "error"

        threading.Thread(target=work, daemon=True).start()
        return jsonify({"job_id": job.id})

    # -- polling ----------------------------------------------------------

    @app.get("/api/job/<job_id>")
    def job_status(job_id: str):
        with JOBS_LOCK:
            job = JOBS.get(job_id)
        if job is None:
            return jsonify({"error": "No such job."}), 404
        return jsonify(job.snapshot())

    # -- download and install ---------------------------------------------

    @app.get("/api/download/<mod_key>")
    def download(mod_key: str):
        """Send the built mod as a zip the browser can save."""
        folder = _built_folder(mod_key, settings)
        if folder is None:
            return jsonify({"error": f"No build found for {mod_key!r}."}), 404
        safe = folder.name

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for item in sorted(folder.iterdir()):
                if item.is_file():
                    archive.write(item, item.name)
        buffer.seek(0)
        return send_file(
            buffer, mimetype="application/zip",
            as_attachment=True, download_name=f"{safe}.zip",
        )

    @app.post("/api/install/<mod_key>")
    def install(mod_key: str):
        """Copy the .package and .ts4script into the Mods folder."""
        import shutil

        folder = _built_folder(mod_key, settings)
        if folder is None:
            return jsonify({"error": f"No build found for {mod_key!r}."}), 404
        safe = folder.name

        mods = Path(
            (request.get_json(silent=True) or {}).get("mods_dir")
            or settings.mods_dir or default_mods_folder()
        )
        if not mods.is_dir():
            return jsonify({
                "error": f"No Mods folder at {mods}. Check the path under "
                         f"Output, or copy the files yourself."
            }), 400

        target = mods / safe
        target.mkdir(parents=True, exist_ok=True)
        copied = []
        for item in folder.iterdir():
            if item.suffix in {".package", ".ts4script"}:
                shutil.copy2(item, target / item.name)
                copied.append(item.name)

        if not copied:
            return jsonify({"error": "No mod files found to copy."}), 400
        return jsonify({"copied": copied, "target": str(target)})

    # -- spec round-trip ---------------------------------------------------

    @app.post("/api/validate")
    def validate_spec():
        """Let the browser check hand-edited specs before building."""
        try:
            spec = CareerSpec.from_dict(request.get_json(silent=True) or {})
        except SpecError as exc:
            return jsonify({"valid": False, "problems": [str(exc)]})
        problems = spec.validate()
        return jsonify({
            "valid": not problems,
            "problems": problems,
            "spec": spec.to_dict(),
        })

    return app


def run(host: str = "127.0.0.1", port: int = 7878, open_browser: bool = True) -> None:
    app = create_app()
    url = f"http://{host}:{port}"

    if open_browser:
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    print(f"\n  Career Forge running at {url}")
    print("  Press Ctrl+C to stop.\n")
    app.run(host=host, port=port, debug=False, threaded=True)
