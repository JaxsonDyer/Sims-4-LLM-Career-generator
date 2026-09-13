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

import base64
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
from forge.game_content import (
    GameContent, load_cached, rescan, skill_pack,
)
from forge.icons import find_game_dir, prepare_icon_art
from forge.ids import InstanceAllocator
from forge.imagegen import KNOWN_IMAGE_MODELS
from forge.llm import (
    OpenRouterClient, generate_career, refine_career, LLMError,
)
from forge.schema import CareerSpec, SpecError, set_extra_skills

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
    kind: str                      # "generate" | "refine" | "build"
    status: str = "running"        # running | done | error
    log: list[dict] = field(default_factory=list)
    error: str = ""
    spec: dict | None = None
    build: dict | None = None
    # Refinement results: what the model says it changed.
    summary: str = ""
    # Per-branch icon previews, branch key -> data URL.
    icons: dict[str, str] = field(default_factory=dict)
    session_id: str = ""
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
                "summary": self.summary,
                "icons": self.icons,
                "session_id": self.session_id,
            }


@dataclass
class Session:
    """
    An in-memory refinement session around one drafted career.

    Holds the current spec, the full LLM conversation so the model has
    context across refinements, and the icon previews. Deliberately not
    persisted: a server restart starts a fresh conversation, which the
    user chose over saved chat history.
    """

    id: str
    spec: dict
    messages: list[dict] = field(default_factory=list)
    # Player-visible turns only: {"role": "user"|"assistant", "text": ...}
    chat: list[dict] = field(default_factory=list)
    icon_mode: str = "ea"
    icons: dict[str, str] = field(default_factory=dict)


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()

SESSIONS: dict[str, Session] = {}
SESSIONS_LOCK = threading.Lock()

# Sessions are pure conversation state; dropping old ones just means the
# oldest careers lose their chat context, so cap them like jobs.
MAX_SESSIONS = 20


def _spec_for_ui(spec: CareerSpec) -> dict:
    """
    The spec as the browser needs it: each branch dict gains the `key` the
    server uses for icon previews. asdict() cannot provide it - it's a
    Python property, not a field - and the JS matches previews by key.
    """
    from forge.schema import _slug

    data = spec.to_dict()
    for branch in data.get("branches", []):
        branch["key"] = _slug(str(branch.get("name", ""))).lower()
    return data


def _known_skills(content: GameContent | None,
                  enabled_packs: list[str] | None) -> list[str] | None:
    """
    The skill list the model may use, or None for the default prompt.

    The registry (set_extra_skills) already covers enabled pack skills, so
    the base known_skills() set is exactly what the prompt should advertise.
    """
    from forge.schema import known_skills as all_known

    if content is None:
        return None
    return sorted(all_known())


def _skill_sources(content: GameContent | None,
                   enabled_packs: list[str] | None) -> dict[str, str]:
    """skill -> pack display name, for chip labels in the ladder."""
    if content is None:
        return {}
    enabled = _enabled_set(content, enabled_packs)
    return {
        skill: content.name_for(code)
        for code, skills in content.packs.items() if code in enabled
        for skill in skills
    }


def _enabled_set(content: GameContent | None,
                 enabled_packs: list[str] | None) -> set[str]:
    """Which pack folder codes are actually usable right now."""
    if content is None or enabled_packs is None:
        return set()
    return {code for code in enabled_packs if code in content.packs}

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

    # Game content (pack skill objectives) is cached on disk; load lazily so
    # a corrupt cache or missing game never blocks startup.
    _app_state = {"content": load_cached()}

    def content() -> GameContent | None:
        return _app_state["content"]

    def enabled_packs() -> list[str] | None:
        return settings.enabled_packs

    def _sync_extra_skills() -> None:
        """
        Keep the schema's allowed-skill set in step with the pack selection,
        so specs drafted with pack skills also pass validation.
        """
        game = content()
        enabled = _enabled_set(game, enabled_packs())
        set_extra_skills({
            skill
            for code in enabled
            for skill in (game.packs.get(code, {}) if game else {})
        })

    _sync_extra_skills()

    def packs_payload() -> dict:
        """What the settings panel shows: every detected pack, its skills,
        display name and whether it's currently enabled."""
        game = content()
        if game is None:
            return {"scanned": False, "packs": []}
        on = set(settings.enabled_packs or [])
        return {
            "scanned": True,
            "game_version": game.game_version,
            "scanned_utc": game.scanned_utc,
            "packs": [
                {
                    "code": code,
                    "name": game.name_for(code),
                    "skills": sorted(skills),
                    "enabled": code in on or settings.enabled_packs is None,
                }
                for code, skills in sorted(game.packs.items())
            ],
        }

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

    def new_session(spec: CareerSpec, messages: list[dict],
                    icon_mode: str, icons: dict[str, str]) -> Session:
        session = Session(
            id=uuid.uuid4().hex[:12], spec=spec.to_dict(),
            messages=messages, icon_mode=icon_mode, icons=icons,
        )
        with SESSIONS_LOCK:
            SESSIONS[session.id] = session
            if len(SESSIONS) > MAX_SESSIONS:
                for stale in list(SESSIONS)[:-MAX_SESSIONS]:
                    SESSIONS.pop(stale, None)
        return session

    def generate_icons(spec: CareerSpec, job: Job) -> dict[str, str]:
        """
        Draft-time custom icon art, cached under the build folder so the
        later Build call never re-bills for the same art.
        """
        alloc = InstanceAllocator(spec.mod_key)
        icon_set = prepare_icon_art(
            spec, alloc, Path(settings.output_dir) / spec.mod_key,
            api_key=settings.resolved_api_key,
            image_model=settings.image_model,
            on_progress=lambda m: job.add(m),
        )
        for warning in icon_set.warnings:
            job.add(warning, "warn")
        previews: dict[str, str] = {}
        for key, art in icon_set.branches.items():
            if art.preview_png:
                previews[key] = (
                    "data:image/png;base64,"
                    + base64.b64encode(art.preview_png).decode("ascii")
                )
        return previews

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
            "image_model": settings.image_model,
            "image_models": KNOWN_IMAGE_MODELS,
            "game_dir": settings.game_dir,
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
        if "game_dir" in data:
            settings.game_dir = str(data["game_dir"]).strip()
        if "image_model" in data:
            settings.image_model = str(data["image_model"]).strip() or settings.image_model
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

    # -- game content ------------------------------------------------------

    @app.get("/api/game-content")
    def get_game_content():
        return jsonify(packs_payload())

    @app.post("/api/game-content/scan")
    def scan_game_content():
        """Rescan the installed game for pack skill objectives (~20s)."""
        explicit = settings.game_dir.strip()
        if explicit:
            # An explicit path is a strong statement; don't second-guess it
            # with auto-detection if it's wrong.
            game = Path(explicit) if Path(explicit).is_dir() else None
        else:
            game = find_game_dir()
        if game is None:
            return jsonify({
                "error": "The Sims 4 install not found. Set the folder "
                         "under Game content once you have it."
            }), 400
        job = new_job("scan")

        def work() -> None:
            try:
                scanned = rescan(game, on_progress=lambda m: job.add(m))
                _app_state["content"] = scanned
                # First scan: everything detected is on, per the default.
                if settings.enabled_packs is None:
                    settings.enabled_packs = sorted(scanned.packs)
                    settings.save()
                _sync_extra_skills()
                job.add(f"Found {len(scanned.packs)} pack(s) with skill "
                        f"objectives.", "good")
                job.status = "done"
            except Exception:
                job.error = traceback.format_exc()
                job.add("Scan failed. See the server console.", "error")
                job.status = "error"

        threading.Thread(target=work, daemon=True).start()
        return jsonify({"job_id": job.id})

    @app.post("/api/game-content/packs")
    def set_enabled_packs():
        """The player's pack selection: list of folder codes to enable."""
        data = request.get_json(silent=True) or {}
        packs = data.get("enabled_packs")
        if packs is None:
            return jsonify({"error": "enabled_packs list required."}), 400
        game = content()
        valid = set(game.packs) if game else set()
        settings.enabled_packs = sorted(
            {str(p) for p in packs if str(p) in valid}
        )
        settings.save()
        _sync_extra_skills()
        return jsonify(packs_payload())

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
        icon_mode = "custom" if data.get("icon_mode") == "custom" else "ea"
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
                    known_skills=_known_skills(content(), enabled_packs()),
                )
                spec = result.spec
                # Icon style is the player's choice, never the model's.
                spec.icon_mode = icon_mode

                icons: dict[str, str] = {}
                if icon_mode == "custom":
                    icons = generate_icons(spec, job)

                session = new_session(spec, result.messages, icon_mode, icons)
                job.spec = _spec_for_ui(spec)
                job.icons = icons
                job.session_id = session.id
                job.add(
                    f"Drafted {spec.name} in "
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
                result = build_career(
                    spec, output, on_progress=lambda m: job.add(m),
                    api_key=settings.resolved_api_key,
                    image_model=settings.image_model,
                    game_dir=settings.game_dir or None,
                    content=content(),
                    enabled_packs=enabled_packs(),
                )
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

    # -- refine ------------------------------------------------------------

    @app.post("/api/refine")
    def start_refine():
        """
        Apply one free-text instruction to the career in a session and hand
        back the revised spec, keeping the whole conversation alive so the
        next instruction builds on this one.
        """
        data = request.get_json(silent=True) or {}
        instruction = str(data.get("instruction", "")).strip()
        if not instruction:
            return jsonify({"error": "Say what you want changed."}), 400
        with SESSIONS_LOCK:
            session = SESSIONS.get(str(data.get("session_id", "")))
        if session is None:
            return jsonify({
                "error": "That session is gone. Draft the career again."
            }), 404
        if not settings.resolved_api_key:
            return jsonify({"error": "No OpenRouter key set."}), 400

        model = str(data.get("model") or settings.model).strip()
        try:
            temperature = float(data.get("temperature", 0.6))
            attempts = int(data.get("max_attempts", settings.max_attempts))
        except (TypeError, ValueError):
            return jsonify({"error": "Temperature and attempts must be numbers."}), 400

        job = new_job("refine")

        def work() -> None:
            try:
                spec = CareerSpec.from_dict(session.spec)
                client = OpenRouterClient(settings.resolved_api_key)
                result = refine_career(
                    client, spec, instruction, model,
                    messages=session.messages,
                    max_attempts=attempts, temperature=temperature,
                    on_progress=lambda m: job.add(m),
                    known_skills=_known_skills(content(), enabled_packs()),
                )
                new_spec = result.spec
                # Identity and icon style are the player's, not the model's.
                new_spec.mod_key = spec.mod_key
                new_spec.icon_mode = session.icon_mode

                icons = session.icons
                if session.icon_mode == "custom":
                    # The cache makes unchanged branches free; only branches
                    # whose art prompt changed are billed again.
                    icons = generate_icons(new_spec, job)

                session.spec = new_spec.to_dict()
                session.messages = result.messages
                session.icons = icons
                session.chat.append({"role": "user", "text": instruction})
                session.chat.append({
                    "role": "assistant",
                    "text": result.change_summary or "Career updated.",
                })

                job.spec = _spec_for_ui(new_spec)
                job.icons = icons
                job.session_id = session.id
                job.summary = result.change_summary
                job.add(f"Revised in {result.attempts} attempt(s).", "good")
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

    @app.get("/api/session/<session_id>")
    def session_state(session_id: str):
        """The current spec, chat and icons, so a refresh loses nothing."""
        with SESSIONS_LOCK:
            session = SESSIONS.get(session_id)
        if session is None:
            return jsonify({"error": "No such session."}), 404
        return jsonify({
            "id": session.id,
            "spec": _spec_for_ui(CareerSpec.from_dict(session.spec)),
            "chat": session.chat,
            "icons": session.icons,
            "icon_mode": session.icon_mode,
        })

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
