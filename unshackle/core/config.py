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
        wvds = data / "WVDs"
        prds = data / "PRDs"
        dcsl = data / "DCSL"

    class _Filenames:
        # default filenames, do not modify here, set via config
        log = "unshackle_{name}_{time}.log"  # Directories.logs
        config = "config.yaml"  # Directories.services / tag
        root_config = "unshackle.yaml"  # Directories.user_configs
        chapters = "Chapters_{title}_{random}.txt"  # Directories.temp
        subtitle = "Subtitle_{id}_{language}.srt"  # Directories.temp

    def __init__(self, **kwargs: Any):
        self.dl: dict = kwargs.get("dl") or {}
        self.aria2c: dict = kwargs.get("aria2c") or {}
        self.n_m3u8dl_re: dict = kwargs.get("n_m3u8dl_re") or {}
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

        downloader_cfg = kwargs.get("downloader") or "requests"
        if isinstance(downloader_cfg, dict):
            self.downloader_map = {k.upper(): v for k, v in downloader_cfg.items()}
            self.downloader = self.downloader_map.get("DEFAULT", "requests")
        else:
            self.downloader_map = {}
            self.downloader = downloader_cfg

        self.filenames = self._Filenames()
        for name, filename in (kwargs.get("filenames") or {}).items():
            setattr(self.filenames, name, filename)

        self.headers: dict = kwargs.get("headers") or {}
        self.key_vaults: list[dict[str, Any]] = kwargs.get("key_vaults", [])
        self.muxing: dict = kwargs.get("muxing") or {}
        self.proxy_providers: dict = kwargs.get("proxy_providers") or {}
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
        self.update_checks: bool = kwargs.get("update_checks", True)
        self.update_check_interval: int = kwargs.get("update_check_interval", 24)

        # Handle backward compatibility for scene_naming option
        self.scene_naming: Optional[bool] = kwargs.get("scene_naming")
        self.output_template: dict = kwargs.get("output_template") or {}

        # Apply scene_naming compatibility if no output_template is defined
        self._apply_scene_naming_compatibility()

        # Validate output templates
        self._validate_output_templates()

        self.title_cache_time: int = kwargs.get("title_cache_time", 1800)  # 30 minutes default
        self.title_cache_max_retention: int = kwargs.get("title_cache_max_retention", 86400)  # 24 hours default
        self.title_cache_enabled: bool = kwargs.get("title_cache_enabled", True)

    def _apply_scene_naming_compatibility(self) -> None:
        """Apply backward compatibility for the old scene_naming option."""
        if self.scene_naming is not None:
            # Only apply if no output_template is already defined
            if not self.output_template.get("movies") and not self.output_template.get("series"):
                if self.scene_naming:
                    # scene_naming: true = scene-style templates
                    self.output_template.update(
                        {
                            "movies": "{title}.{year}.{quality}.{source}.WEB-DL.{dual?}.{multi?}.{audio_full}.{atmos?}.{hdr?}.{hfr?}.{video}-{tag}",
                            "series": "{title}.{year?}.{season_episode}.{episode_name?}.{quality}.{source}.WEB-DL.{dual?}.{multi?}.{audio_full}.{atmos?}.{hdr?}.{hfr?}.{video}-{tag}",
                            "songs": "{track_number}.{title}.{source?}.WEB-DL.{audio_full}.{atmos?}-{tag}",
                        }
                    )
                else:
                    # scene_naming: false = Plex-friendly templates
                    self.output_template.update(
                        {
                            "movies": "{title} ({year}) {quality}",
                            "series": "{title} {season_episode} {episode_name?}",
                            "songs": "{track_number}. {title}",
                        }
                    )

                # Warn about deprecated option
                warnings.warn(
                    "The 'scene_naming' option is deprecated. Please use 'output_template' instead. "
                    "Your current setting has been converted to equivalent templates.",
                    DeprecationWarning,
                    stacklevel=2,
                )

    def _validate_output_templates(self) -> None:
        """Validate output template configurations and warn about potential issues."""
        if not self.output_template:
            return

        # Known template variables for validation
        valid_variables = {
            # Basic variables
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
            # Audio variables
            "audio",
            "audio_channels",
            "audio_full",
            "atmos",
            "dual",
            "multi",
            # Video variables
            "video",
            "hdr",
            "hfr",
        }

        # Filesystem-unsafe characters that could cause issues
        unsafe_chars = r'[<>:"/\\|?*]'

        for template_type, template_str in self.output_template.items():
            if not isinstance(template_str, str):
                warnings.warn(f"Template '{template_type}' must be a string, got {type(template_str).__name__}")
                continue

            # Extract variables from template
            variables = re.findall(r"\{([^}]+)\}", template_str)

            # Check for unknown variables
            for var in variables:
                # Remove conditional suffix if present
                var_clean = var.rstrip("?")
                if var_clean not in valid_variables:
                    warnings.warn(f"Unknown template variable '{var}' in {template_type} template")

            # Check for filesystem-unsafe characters outside of variables
            # Replace variables with safe placeholders for testing
            test_template = re.sub(r"\{[^}]+\}", "TEST", template_str)
            if re.search(unsafe_chars, test_template):
                warnings.warn(f"Template '{template_type}' may contain filesystem-unsafe characters")

            # Check for empty template
            if not template_str.strip():
                warnings.warn(f"Template '{template_type}' is empty")

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
    # The AppDirs User Config Folder (e.g., %localappdata%/unshackle)
    Config._Directories.user_configs / Config._Filenames.root_config,
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
