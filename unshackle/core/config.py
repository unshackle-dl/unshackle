from __future__ import annotations

import re
import warnings
from pathlib import Path
from typing import Any, Optional

import yaml
from appdirs import AppDirs


class Config:
    class _Directories:
        # default directories, do not modify here, set via config
        app_dirs = AppDirs("unshackle", False)
        core_dir = Path(__file__).resolve().parent
        namespace_dir = core_dir.parent
        commands = namespace_dir / "commands"
        services = [namespace_dir / "services"]
        vaults = namespace_dir / "vaults"
        fonts = namespace_dir / "fonts"
        user_configs = core_dir.parent
        data = core_dir.parent
        downloads = core_dir.parent.parent / "downloads"
        temp = core_dir.parent.parent / "temp"
        cache = data / "cache"
        cookies = data / "cookies"
        logs = data / "logs"
        exports = data / "exports"
        wvds = data / "WVDs"
        prds = data / "PRDs"
        dcsl = data / "DCSL"

    class _Filenames:
        # default filenames, do not modify here, set via config
        log = "unshackle_{name}_{time}.log"  # Directories.logs
        debug_log = "unshackle_debug_{service}_{time}.jsonl"  # Directories.logs
        config = "config.yaml"  # Directories.services / tag
        root_config = "unshackle.yaml"  # Directories.user_configs
        chapters = "Chapters_{title}_{random}.txt"  # Directories.temp
        subtitle = "Subtitle_{id}_{language}.srt"  # Directories.temp

    def __init__(self, **kwargs: Any):
        self.dl: dict = kwargs.get("dl") or {}
        self.cdm: dict = kwargs.get("cdm") or {}
        self.chapter_fallback_name: str = kwargs.get("chapter_fallback_name") or ""
        self.curl_impersonate: dict = kwargs.get("curl_impersonate") or {}
        self.remote_cdm: list[dict] = kwargs.get("remote_cdm") or []
        self.credentials: dict = kwargs.get("credentials") or {}
        self.subtitle: dict = kwargs.get("subtitle") or {}

        self.directories = self._Directories()
        for name, path in (kwargs.get("directories") or {}).items():
            if name.lower() in ("app_dirs", "core_dir", "namespace_dir", "user_configs", "data"):
                # these must not be modified by the user
                continue
            if name == "services" and isinstance(path, list):
                setattr(self.directories, name, [Path(p).expanduser() for p in path])
            else:
                setattr(self.directories, name, Path(path).expanduser())

        downloader_cfg = kwargs.get("downloader")
        if downloader_cfg and downloader_cfg != "requests":
            warnings.warn(
                f"downloader '{downloader_cfg}' is deprecated. The unified requests downloader is now used.",
                DeprecationWarning,
                stacklevel=2,
            )

        self.filenames = self._Filenames()
        for name, filename in (kwargs.get("filenames") or {}).items():
            setattr(self.filenames, name, filename)

        self.headers: dict = kwargs.get("headers") or {}
        self.key_vaults: list[dict[str, Any]] = kwargs.get("key_vaults", [])
        self.muxing: dict = kwargs.get("muxing") or {}
        self.proxy_providers: dict = kwargs.get("proxy_providers") or {}
        self.remote_services: dict = kwargs.get("remote_services") or {}
        self.serve: dict = kwargs.get("serve") or {}
        self.services: dict = kwargs.get("services") or {}
        decryption_cfg = kwargs.get("decryption") or {}
        if isinstance(decryption_cfg, dict):
            self.decryption_map = {k.upper(): v for k, v in decryption_cfg.items()}
            self.decryption = self.decryption_map.get("DEFAULT", "shaka")
        else:
            self.decryption_map = {}
            self.decryption = decryption_cfg or "shaka"

        self.set_terminal_bg: bool = kwargs.get("set_terminal_bg", False)
        self.tag: str = kwargs.get("tag") or ""
        self.tag_group_name: bool = kwargs.get("tag_group_name", True)
        self.tag_imdb_tmdb: bool = kwargs.get("tag_imdb_tmdb", True)
        self.tmdb_api_key: str = kwargs.get("tmdb_api_key") or ""
        self.simkl_client_id: str = kwargs.get("simkl_client_id") or ""
        self.decrypt_labs_api_key: str = kwargs.get("decrypt_labs_api_key") or ""
        self.update_checks: bool = kwargs.get("update_checks", True)
        self.update_check_interval: int = kwargs.get("update_check_interval", 24)

        self.language_tags: dict = kwargs.get("language_tags") or {}
        self.output_template: dict = kwargs.get("output_template") or {}
        self.folder_template: str = self.output_template.pop("folder", "") or ""

        if kwargs.get("scene_naming") is not None:
            raise SystemExit(
                "ERROR: The 'scene_naming' option has been removed.\n"
                "Please configure 'output_template' in your unshackle.yaml instead.\n"
                "See unshackle-example.yaml for examples."
            )

        if self.output_template:
            self._validate_output_templates()

        self.unicode_filenames: bool = kwargs.get("unicode_filenames", False)

        self.title_cache_time: int = kwargs.get("title_cache_time", 1800)  # 30 minutes default
        self.title_cache_max_retention: int = kwargs.get("title_cache_max_retention", 86400)  # 24 hours default
        self.title_cache_enabled: bool = kwargs.get("title_cache_enabled", True)

        self.debug: bool = kwargs.get("debug", False)
        self.debug_keys: bool = kwargs.get("debug_keys", False)

    def _validate_output_templates(self) -> None:
        """Validate output template configurations and warn about potential issues."""
        if not self.output_template:
            return

        valid_variables = {
            "title",
            "year",
            "season",
            "episode",
            "season_episode",
            "episode_name",
            "quality",
            "resolution",
            "source",
            "tag",
            "track_number",
            "artist",
            "album",
            "disc",
            "audio",
            "audio_channels",
            "audio_full",
            "atmos",
            "dual",
            "multi",
            "video",
            "hdr",
            "hfr",
            "edition",
            "repack",
            "lang_tag",
        }

        unsafe_chars = r'[<>:"/\\|?*]'

        all_templates = dict(self.output_template)
        if self.folder_template:
            all_templates["folder"] = self.folder_template

        for template_type, template_str in all_templates.items():
            if not isinstance(template_str, str):
                warnings.warn(f"Template '{template_type}' must be a string, got {type(template_str).__name__}")
                continue

            variables = re.findall(r"\{([^}]+)\}", template_str)

            for var in variables:
                var_clean = var.rstrip("?")
                if var_clean not in valid_variables:
                    warnings.warn(f"Unknown template variable '{var}' in {template_type} template")

            test_template = re.sub(r"\{[^}]+\}", "TEST", template_str)
            if re.search(unsafe_chars, test_template):
                warnings.warn(f"Template '{template_type}' may contain filesystem-unsafe characters")

            if not template_str.strip():
                warnings.warn(f"Template '{template_type}' is empty")

    def get_template_separator(self, template_type: str = "movies") -> str:
        """Get the filename separator for the given template type.

        Analyzes the active template to determine whether it uses dots or spaces
        between variables. Falls back to dot separator (scene-style) by default.

        Args:
            template_type: One of "movies", "series", or "songs".
        """
        template = self.output_template[template_type]
        between_vars = re.findall(r"\}([^{]*)\{", template)
        separator_text = "".join(between_vars)
        dot_count = separator_text.count(".")
        space_count = separator_text.count(" ")

        return " " if space_count > dot_count else "."

    @classmethod
    def from_yaml(cls, path: Path) -> Config:
        if not path.exists():
            raise FileNotFoundError(f"Config file path ({path}) was not found")
        if not path.is_file():
            raise FileNotFoundError(f"Config file path ({path}) is not to a file.")
        return cls(**yaml.safe_load(path.read_text(encoding="utf8")) or {})


# noinspection PyProtectedMember
POSSIBLE_CONFIG_PATHS = (
    # The unshackle Namespace Folder (e.g., %appdata%/Python/Python311/site-packages/unshackle)
    Config._Directories.namespace_dir / Config._Filenames.root_config,
    # The Parent Folder to the unshackle Namespace Folder (e.g., %appdata%/Python/Python311/site-packages)
    Config._Directories.namespace_dir.parent / Config._Filenames.root_config,
    # The AppDirs User Config Folder (e.g., ~/.config/unshackle on Linux, %LOCALAPPDATA%\unshackle on Windows)
    Path(Config._Directories.app_dirs.user_config_dir) / Config._Filenames.root_config,
)


def get_config_path() -> Optional[Path]:
    """
    Get Path to Config from any one of the possible locations.

    Returns None if no config file could be found.
    """
    for path in POSSIBLE_CONFIG_PATHS:
        if path.exists():
            return path
    return None


config_path = get_config_path()
if config_path:
    config = Config.from_yaml(config_path)
else:
    config = Config()

__all__ = ("config",)
