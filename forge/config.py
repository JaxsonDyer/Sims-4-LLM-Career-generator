"""
Settings persistence.

The API key is stored in a file in the user's config directory with
owner-only permissions. That is not encryption, and the docstring says so
plainly: anyone with access to the account can read it. It is the same
posture as a .env file. If you want real secret storage, use the
OPENROUTER_API_KEY environment variable instead, which this module prefers
when present.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, asdict, field
from pathlib import Path

from .env import load_env, find_env_file
from .imagegen import DEFAULT_IMAGE_MODEL

# Keys that came from a .env file, recorded so the interface can say where
# the active key was found without ever displaying the key itself.
_env_file_keys: set = set()

APP_NAME = "career_forge"

DEFAULT_MODELS = [
    "anthropic/claude-sonnet-4.5",
    "anthropic/claude-opus-4.1",
    "openai/gpt-4o",
    "google/gemini-2.5-pro",
    "meta-llama/llama-3.3-70b-instruct",
    "deepseek/deepseek-chat",
    "z-ai/glm-5.3-flash"
]


def config_dir() -> Path:
    """Per-platform config location."""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif os.sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    path = base / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_mods_folder() -> Path:
    """Best guess at the Sims 4 Mods folder for this platform."""
    docs = Path.home() / "Documents"
    return docs / "Electronic Arts" / "The Sims 4" / "Mods"


@dataclass
class Settings:
    api_key: str = ""
    model: str = "anthropic/claude-sonnet-4.5"
    temperature: float = 0.8
    max_attempts: int = 3
    output_dir: str = ""
    mods_dir: str = ""
    # Image model used when a career asks for custom AI-generated icons.
    image_model: str = DEFAULT_IMAGE_MODEL
    # Installed-game folder override; blank = auto-detect the usual paths.
    game_dir: str = ""
    # Pack folders (EP07, GP04, ...) whose content may be referenced.
    # None = never scanned; a list (possibly empty) = user has chosen.
    enabled_packs: list[str] | None = None
    recent_models: list[str] = field(default_factory=lambda: list(DEFAULT_MODELS))

    def __post_init__(self) -> None:
        if not self.output_dir:
            self.output_dir = str(Path.home() / "CareerForge")
        if not self.mods_dir:
            self.mods_dir = str(default_mods_folder())

    @property
    def resolved_api_key(self) -> str:
        """
        Where the key comes from, in priority order:
            1. OPENROUTER_API_KEY in the real environment
            2. OPENROUTER_API_KEY from a .env file
            3. the key saved through the interface

        A .env is the recommended spot: it keeps the key out of the settings
        file and out of any screenshot of the interface.
        """
        return os.environ.get("OPENROUTER_API_KEY", "").strip() or self.api_key

    @property
    def key_source(self) -> str:
        """Where the active key came from, for display. Never returns the key."""
        if os.environ.get("OPENROUTER_API_KEY", "").strip():
            env_file = find_env_file()
            if env_file and "OPENROUTER_API_KEY" in _env_file_keys:
                return f".env ({env_file})"
            return "environment variable"
        if self.api_key:
            return "saved in settings"
        return "not set"

    @classmethod
    def load(cls) -> "Settings":
        # Pull in .env before reading anything else, so env-provided defaults
        # are visible to the fields below.
        global _env_file_keys
        _env_file_keys = set(load_env().keys())

        path = config_dir() / "settings.json"
        data: dict = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                # A corrupt settings file should never block startup.
                data = {}

        known = {f for f in cls.__dataclass_fields__}
        settings = cls(**{k: v for k, v in data.items() if k in known})

        # Environment overrides anything saved, so a .env can pin the model
        # or output folder for a whole project.
        env_model = os.environ.get("CAREER_FORGE_MODEL", "").strip()
        if env_model:
            settings.model = env_model
        env_output = os.environ.get("CAREER_FORGE_OUTPUT", "").strip()
        if env_output:
            settings.output_dir = env_output
        env_mods = os.environ.get("CAREER_FORGE_MODS_DIR", "").strip()
        if env_mods:
            settings.mods_dir = env_mods
        env_temp = os.environ.get("CAREER_FORGE_TEMPERATURE", "").strip()
        if env_temp:
            try:
                settings.temperature = float(env_temp)
            except ValueError:
                pass
        env_image = os.environ.get("CAREER_FORGE_IMAGE_MODEL", "").strip()
        if env_image:
            settings.image_model = env_image

        return settings

    def save(self) -> Path:
        """
        Write settings to disk.

        The API key is only persisted if it was typed into the interface. A
        key coming from the environment or a .env is deliberately not copied
        into the settings file, so it stays in one place.
        """
        path = config_dir() / "settings.json"
        payload = asdict(self)
        if os.environ.get("OPENROUTER_API_KEY", "").strip():
            payload["api_key"] = ""
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        try:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600
        except OSError:
            pass  # Windows and some filesystems ignore this; not fatal.
        return path
