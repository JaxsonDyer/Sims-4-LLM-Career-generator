"""
DearPyGui front end.

Generation runs on a worker thread and pushes log lines into a queue that the
render loop drains. DearPyGui is not thread safe for arbitrary calls from
background threads, so the worker never touches widgets directly.
"""

from __future__ import annotations

import queue
import shutil
import threading
import traceback
from pathlib import Path

import dearpygui.dearpygui as dpg

from forge.builder import build_career, BuildError
from forge.config import Settings, default_mods_folder, DEFAULT_MODELS
from forge.llm import OpenRouterClient, generate_career, LLMError
from forge.schema import CareerSpec, SpecError

EXAMPLES = [
    "A cryptid hunter who chases things that probably do not exist",
    "Competitive sandwich artist, deadly serious about bread",
    "Deep sea welder with a fear of fish",
    "Ghost lawyer representing the recently deceased in property disputes",
    "Volcano insurance adjuster",
]


def _fmt_hour(hour: int) -> str:
    hour %= 24
    label = hour % 12 or 12
    return f"{label}{'am' if hour < 12 else 'pm'}"


def _schedule_text(level) -> str:
    days = "/".join(d[:3] for d in level.work_days)
    return (f"{days} {_fmt_hour(level.start_hour)}-"
            f"{_fmt_hour(level.start_hour + level.hours_per_day)}")


class ForgeApp:
    def __init__(self) -> None:
        self.settings = Settings.load()
        self.log_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.current_spec: CareerSpec | None = None
        self.last_build = None
        self._example_index = 0

    # -- logging ----------------------------------------------------------

    def log(self, message: str, level: str = "info") -> None:
        self.log_queue.put((level, message))

    def _drain_log(self) -> None:
        while True:
            try:
                level, message = self.log_queue.get_nowait()
            except queue.Empty:
                break
            colour = {
                "info": (200, 200, 200),
                "good": (120, 220, 140),
                "warn": (240, 200, 100),
                "error": (240, 120, 120),
                "step": (140, 190, 250),
            }.get(level, (200, 200, 200))
            dpg.add_text(message, color=colour, parent="log_area", wrap=560)
            dpg.set_y_scroll("log_window", -1.0)

    # -- worker -----------------------------------------------------------

    def _busy(self, busy: bool) -> None:
        dpg.configure_item("generate_btn", enabled=not busy)
        dpg.configure_item("build_btn", enabled=not busy and self.current_spec is not None)

    def on_generate(self) -> None:
        if self.worker and self.worker.is_alive():
            self.log("Already working. Wait for the current run to finish.", "warn")
            return

        idea = dpg.get_value("idea_input").strip()
        if not idea:
            self.log("Type a career idea first.", "warn")
            return

        self.settings.api_key = dpg.get_value("api_key_input").strip()
        self.settings.model = dpg.get_value("model_input").strip()
        self.settings.temperature = dpg.get_value("temp_input")
        self.settings.max_attempts = dpg.get_value("attempts_input")
        self.settings.output_dir = dpg.get_value("output_input").strip()
        self.settings.save()

        if not self.settings.resolved_api_key:
            self.log(
                "No OpenRouter API key. Paste one above, or set the "
                "OPENROUTER_API_KEY environment variable.", "error"
            )
            return

        self._busy(True)
        self.worker = threading.Thread(
            target=self._run_generate, args=(idea,), daemon=True
        )
        self.worker.start()

    def _run_generate(self, idea: str) -> None:
        try:
            self.log(f"Idea: {idea}", "step")
            client = OpenRouterClient(self.settings.resolved_api_key)
            result = generate_career(
                client, idea, self.settings.model,
                max_attempts=self.settings.max_attempts,
                temperature=self.settings.temperature,
                on_progress=lambda m: self.log(m, "info"),
            )
            self.current_spec = result.spec
            spec = result.spec

            self.log("", "info")
            self.log(f"{spec.name}", "good")
            self.log(spec.description, "info")
            total = sum(len(b.levels) for b in spec.branches)
            self.log(
                f"{len(spec.branches)} branch(es), {total} levels, "
                f"{result.attempts} attempt(s)", "good"
            )
            for branch in spec.branches:
                origin = ("base" if branch.branches_at is None
                          else f"splits at {branch.branches_at}")
                self.log(f"  {branch.name} ({origin})", "step")
                for lvl in sorted(branch.levels, key=lambda l: l.level):
                    self.log(
                        f"    {lvl.level:>2}. {lvl.title}  "
                        f"${lvl.pay_per_hour}/hr  {_schedule_text(lvl)}", "info"
                    )
            self.log("", "info")
            self.log("Review it, then press Build Mod.", "good")

        except (LLMError, SpecError) as exc:
            self.log(str(exc), "error")
        except Exception:
            self.log("Unexpected failure:\n" + traceback.format_exc(), "error")
        finally:
            self._busy(False)

    def on_build(self) -> None:
        if not self.current_spec:
            self.log("Generate a career first.", "warn")
            return
        if self.worker and self.worker.is_alive():
            self.log("Already working.", "warn")
            return
        self._busy(True)
        self.worker = threading.Thread(target=self._run_build, daemon=True)
        self.worker.start()

    def _run_build(self) -> None:
        try:
            out = dpg.get_value("output_input").strip() or self.settings.output_dir
            result = build_career(
                self.current_spec, out,
                on_progress=lambda m: self.log(m, "info"),
                api_key=self.settings.resolved_api_key,
                image_model=self.settings.image_model,
            )
            self.last_build = result
            self.log("", "info")
            self.log(f"Built to {result.output_dir}", "good")
            self.log(f"  {result.package_path.name}", "good")
            self.log(f"  {result.script_path.name}", "good")
            for warning in result.warnings:
                self.log(f"note: {warning}", "warn")
            self.log("", "info")
            self.log("Read BUILD_REPORT.txt before installing. It covers", "step")
            self.log("install steps and how to tell whether it loaded.", "step")
            dpg.configure_item("install_btn", enabled=True)
            dpg.configure_item("open_btn", enabled=True)
        except BuildError as exc:
            self.log(str(exc), "error")
        except Exception:
            self.log("Unexpected failure:\n" + traceback.format_exc(), "error")
        finally:
            self._busy(False)

    def on_install(self) -> None:
        """Copy the built files into the Mods folder, if we can find it."""
        if not self.last_build:
            return
        mods = Path(dpg.get_value("mods_input").strip() or default_mods_folder())
        if not mods.exists():
            self.log(
                f"Mods folder not found at {mods}. Set the correct path above, "
                f"or copy the files manually.", "error"
            )
            return
        target = mods / self.last_build.output_dir.name
        target.mkdir(parents=True, exist_ok=True)
        for src in (self.last_build.package_path, self.last_build.script_path):
            shutil.copy2(src, target / src.name)
            self.log(f"Copied {src.name} -> {target}", "good")
        self.log("", "info")
        self.log("Now: enable script mods in Game Options > Other, delete", "step")
        self.log("localthumbcache.package, and restart the game.", "step")

    def on_open_folder(self) -> None:
        if not self.last_build:
            return
        import subprocess, sys, os
        path = str(self.last_build.output_dir)
        try:
            if os.name == "nt":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.run(["open", path], check=False)
            else:
                subprocess.run(["xdg-open", path], check=False)
        except Exception as exc:
            self.log(f"Could not open folder: {exc}. Path is {path}", "warn")

    def on_example(self) -> None:
        dpg.set_value("idea_input", EXAMPLES[self._example_index])
        self._example_index = (self._example_index + 1) % len(EXAMPLES)

    def on_fetch_models(self) -> None:
        key = dpg.get_value("api_key_input").strip() or self.settings.resolved_api_key
        if not key:
            self.log("Need an API key to list models.", "warn")
            return

        def work() -> None:
            try:
                models = OpenRouterClient(key).list_models()
                self.settings.recent_models = models
                dpg.configure_item("model_input", items=models)
                self.log(f"Loaded {len(models)} models from OpenRouter.", "good")
            except LLMError as exc:
                self.log(str(exc), "error")

        threading.Thread(target=work, daemon=True).start()

    # -- layout -----------------------------------------------------------

    def build_ui(self) -> None:
        dpg.create_context()
        dpg.create_viewport(title="Sims 4 Career Forge", width=1180, height=800)

        with dpg.window(tag="root"):
            with dpg.group(horizontal=True):

                # ---- left column: inputs ----
                with dpg.child_window(width=540, border=False):
                    dpg.add_text("Sims 4 Career Forge", color=(140, 190, 250))
                    dpg.add_text(
                        "Describe a career. A model drafts it, this tool "
                        "validates and packages it.",
                        wrap=520, color=(160, 160, 160),
                    )
                    dpg.add_separator()

                    dpg.add_text("Career idea")
                    dpg.add_input_text(
                        tag="idea_input", multiline=True, width=-1, height=90,
                        hint="A cryptid hunter who chases things that may not exist",
                    )
                    with dpg.group(horizontal=True):
                        dpg.add_button(label="Shuffle example", callback=self.on_example)
                        dpg.add_button(
                            label="Generate", tag="generate_btn",
                            width=140, callback=self.on_generate,
                        )
                        dpg.add_button(
                            label="Build Mod", tag="build_btn", width=120,
                            enabled=False, callback=self.on_build,
                        )

                    dpg.add_separator()
                    with dpg.collapsing_header(label="Model", default_open=True):
                        dpg.add_input_text(
                            tag="api_key_input", label="OpenRouter key",
                            password=True, width=-140,
                            default_value=self.settings.api_key,
                            hint="sk-or-v1-...",
                        )
                        dpg.add_text(
                            "Stored in your config folder, or use the "
                            "OPENROUTER_API_KEY env var.",
                            wrap=500, color=(150, 150, 150),
                        )
                        dpg.add_combo(
                            DEFAULT_MODELS, tag="model_input", label="Model",
                            default_value=self.settings.model, width=-140,
                        )
                        dpg.add_button(
                            label="Fetch model list", callback=self.on_fetch_models
                        )
                        dpg.add_slider_float(
                            tag="temp_input", label="Temperature", width=-140,
                            default_value=self.settings.temperature,
                            min_value=0.0, max_value=1.5,
                        )
                        dpg.add_slider_int(
                            tag="attempts_input", label="Repair attempts",
                            width=-140, default_value=self.settings.max_attempts,
                            min_value=1, max_value=5,
                        )

                    with dpg.collapsing_header(label="Output", default_open=True):
                        dpg.add_input_text(
                            tag="output_input", label="Build folder", width=-140,
                            default_value=self.settings.output_dir,
                        )
                        dpg.add_input_text(
                            tag="mods_input", label="Mods folder", width=-140,
                            default_value=str(default_mods_folder()),
                        )
                        with dpg.group(horizontal=True):
                            dpg.add_button(
                                label="Copy to Mods folder", tag="install_btn",
                                enabled=False, callback=self.on_install,
                            )
                            dpg.add_button(
                                label="Open build folder", tag="open_btn",
                                enabled=False, callback=self.on_open_folder,
                            )

                # ---- right column: log ----
                with dpg.child_window(width=-1, border=False):
                    dpg.add_text("Activity")
                    with dpg.child_window(tag="log_window", width=-1, height=-60):
                        with dpg.group(tag="log_area"):
                            dpg.add_text(
                                "Ready. Paste an OpenRouter key, type an idea, "
                                "press Generate.",
                                color=(160, 160, 160), wrap=560,
                            )
                    dpg.add_text(
                        "Scratch-built tuning is not guaranteed to match your "
                        "game patch. Read BUILD_REPORT.txt.",
                        wrap=560, color=(240, 200, 100),
                    )

        dpg.setup_dearpygui()
        dpg.show_viewport()
        dpg.set_primary_window("root", True)

    def run(self) -> None:
        self.build_ui()
        while dpg.is_dearpygui_running():
            self._drain_log()
            dpg.render_dearpygui_frame()
        dpg.destroy_context()


def main() -> None:
    ForgeApp().run()
