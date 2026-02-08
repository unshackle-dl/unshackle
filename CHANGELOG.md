# Changelog

All notable changes to this project will be documented in this file.

This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This changelog is automatically generated using [git-cliff](https://git-cliff.org).

## [Unreleased]

### Features

- *titles*: Use track source attribute for service name in filenames
- *debug*: Add download output verification logging
- Gluetun VPN integration and remote service enhancements
- *gluetun*: Improve VPN connection display and Windscribe support
- *serve*: Add PlayReady CDM support alongside Widevine
- *cdm*: Add remote PlayReady CDM support via pyplayready RemoteCdm
- *env*: Add ML-Worker binary for DRM licensing
- *video*: Detect interlaced scan type from MPD manifests
- *drm*: Add MonaLisa DRM support to core infrastructure
- *audio*: Codec lists and split muxing
- *proxy*: Add specific server selection for WindscribeVPN

### Bug Fixes

- *subs*: Update SubtitleEdit CLI syntax and respect conversion_method
- *n_m3u8dl_re*: Include language in DASH audio track selection
- *hls*: Prefer media playlist keys over session keys for accurate KID matching
- *deps*: Upgrade vulnerable dependencies for security alerts
- *serve*: Use correct pywidevine users config format
- *cdm*: Correct error key casing in Decrypt Labs API response parsing
- *api*: Validate Bearer prefix before extracting API key
- *serve*: Correct PlayReady RemoteCDM server validation
- *n_m3u8dl_re*: Remove duplicate --write-meta-json argument causing download failures
- *manifests*: Correct DRM type selection for remote PlayReady CDMs
- *proxies*: Fixes WindscribeVPN server authentication
- *subs*: Route pycaption-unsupported formats to pysubs2 in auto mode
- *proxy*: Remove regional restrictions from WindscribeVPN
- *proxy*: Collect servers from all locations in WindscribeVPN
- *downloader*: Correct progress bar tracking for segmented downloads
- *binaries*: Search subdirectories for binary files
- *dash*: Handle high startNumber in SegmentTimeline for DVR manifests
- *drm*: Hide Shaka Packager message for MonaLisa decryption
- *dash*: Add CENC namespace support for PSSH extraction
- *dash*: Preserve MPD DRM instead of overwriting from init segment
- *subtitles*: Preserve sidecar originals
- *mux*: Avoid audio codec suffix on split-audio outputs
- *dl*: Prevent attachment downloads during --skip-dl
- *progress*: Force track bar completion on terminal states
- *progress*: Bind per-track bars and force terminal completion
- *dl*: Keep descriptive and standard audio for requested langs
- *dl*: Always clean up hybrid temp hevc outputs
- *hls*: Finalize n_m3u8dl_re outputs
- *downloader*: Restore requests progress for single-url downloads

### Documentation

- Add configuration documentation WIP
- *changelog*: Add 2.4.0 release notes

### Performance Improvements

- *aria2c*: Improve download performance with singleton manager

### Changes

- *remote_auth*: Remove unused requests.Session
- Remove remote-service code until feature is more complete

### Maintenance

- *api*: Remove remote services

## [2.3.0] - 2026-01-18

### Features

- *config*: Add unicode_filenames option to preserve native characters

### Bug Fixes

- *drm*: Correct PSSH system ID comparison in PlayReady
- *dash*: Handle placeholder KIDs and improve DRM init from segments
- *dash*: Handle N_m3u8DL-RE merge and decryption
- *drm*: Include shaka-packager binary in error messages
- *subs*: Strip whitespace from ASS font names
- *subs*: Handle negative TTML values in multi-value attributes
- *drm*: Filter Widevine PSSH by system ID instead of sorting
- *subs*: Handle WebVTT cue identifiers and overlapping multi-line cues

## [2.2.0] - 2026-01-15

### Features

- *debug*: Add comprehensive debug logging for downloaders and muxing
- *drm*: Add CDM-aware PlayReady fallback detection

### Bug Fixes

- *util*: Improve test command error detection and add natural sorting
- *vaults*: Batch bulk key operations to avoid query limits
- *titles*: Detect HDR10 in hybrid DV filenames correctly
- *vaults*: Adaptive batch sizing for bulk key operations

## [2.1.0] - 2025-11-27

### Features

- *export*: Enhance track export with URL, descriptor, and hex-formatted keys
- *cdm*: Add per-track quality-based CDM selection during runtime DRM switching
- Merge upstream dev branch

### Bug Fixes

- *deps*: Pin pyplayready to <0.7 to avoid KID extraction bug
- *hls*: Convert range_offset to int to prevent TypeError
- *video*: Correct CICP enum values to match ITU-T H.273 specification
- *utilities*: Handle space-hyphen-space separators in sanitize_filename
- *utilities*: Make space-hyphen-space handling conditional on scene_naming
- *windscribevpn*: Add error handling for unsupported regions in get_proxy method
- Restrict WindscribeVPN to supported regions
- *dash*: Add AdaptationSet-level BaseURL resolution
- *dl*: Preserve attachments when rebuilding track list

## [2.0.0] - 2025-11-10

### Features

- Add REST API server with download management
- Add comprehensive JSON debug logging system
- *cdm*: Add highly configurable CustomRemoteCDM for flexible API support
- *proxies*: Add WindscribeVPN proxy provider support
- *dl*: Add --latest-episode option to download only the most recent episode
- Add service-specific configuration overrides
- Add retry handler to curl_cffi Session
- *dl*: Add --audio-description flag to download AD tracks
- *api*: Add url field to services endpoint response
- *api*: Complete API enhancements for v2.0.0
- *api*: Add default parameter handling and improved error responses
- *cache*: Add TMDB and Simkl metadata caching to title cache
- *session*: Add custom fingerprint and preset support
- *fonts*: Add Linux font support for ASS/SSA subtitles
- *subtitle*: Preserve original formatting when no conversion requested
- *dl*: Add --no-video flag to skip video track downloads

### Bug Fixes

- Use keyword arguments for Attachment constructor in font attachment
- Only exclude forced subs when --forced-subs flag is not set
- Update lxml constraint and pyplayready import path
- *tags*: Gracefully handle missing TMDB/Simkl API keys
- *config*: Support config in user config directory across platforms
- *dl*: Validate HYBRID mode requirements before download
- *drm*: Add explicit UTF-8 encoding to mp4decrypt subprocess calls
- *subtitle*: Resolve SDH stripping crash with VTT files
- *naming*: Improve HDR detection with comprehensive transfer checks and hybrid DV+HDR10 support
- *dash*: Correct segment count calculation for startNumber=0
- *session*: Update OkHttp fingerprint presets
- *session*: Remove padding extension from OkHttp JA3 fingerprints
- *dl*: Prevent vault loading when --cdm-only flag is set
- *cdm*: Resolve session key handling for partial cached keys
- *cdm*: Apply session key fix to custom_remote_cdm
- *n_m3u8dl_re*: Read lang attribute from DASH manifests correctly
- *subtitles*: Fix closure bug preventing SDH subtitle stripping
- Ensure subtitles use requests downloader instead of n_m3u8dl_re if Descriptor.URL
- Suppress verbose fontTools logging when scanning system fonts
- *tags*: Skip metadata lookup when API keys not configured

### Documentation

- Improve GitHub issue templates for better bug reports and feature requests
- Add dev branch and update README
- Update CHANGELOG for audio description feature
- *changelog*: Complete v2.0.0 release documentation
- *changelog*: Add --no-video flag and PR #38 credit
- *changelog*: Set release date for version 2.0.0
- *readme*: Remove dev branch warning for main merge

### Changes

- *session*: Modernize type annotations to PEP 604 syntax
- *binaries*: Remove unused mypy import
- Remove unnecessary underscore prefixes from function names
- *tags*: Remove environment variable fallbacks for API keys

### Maintenance

- *api*: Fix import ordering in download_manager and handlers
- Update CHANGELOG.md for version 2.0.0

## [1.4.8] - 2025-10-08

### Features

- Add AC4 codec support in Audio class and update mime/profile handling
- Add pysubs2 subtitle conversion with extended format support
- Add --no-mux flag to skip muxing tracks into container files
- *vaults*: Add DecryptLabs API support to HTTP vault
- Add --exact-lang flag for precise language matching

### Bug Fixes

- Optimize audio track sorting by grouping descriptive tracks and sorting by bitrate, fixes bug that does not identify ATMOS or DD+ as the highest quality available in filenaming.
- Update lxml constraint and pyplayready import path
- Dl.py
- Upgrade pyplayready to 0.6.3 and resolve import compatibility issues
- Suppress tinycss SyntaxWarning by initializing filter before imports
- (subtitle): Move pysubs2 to not be auto while in "testing" phase.

### Reverts

- Remove tinycss SyntaxWarning suppression and fix isort

### Documentation

- Add pysubs2 conversion_method to configuration documentation

### Maintenance

- Bump version to 1.4.8

## [1.4.7] - 2025-09-25

### Features

- Add options for required subtitles and best available quality in download command
- Add download retry count option to download function
- Add decrypt_labs_api_key to Config initialization and change duplicate track log level to debug
- Add curl_cffi session support with browser impersonation
- Update changelog for version 1.4.7

## [1.4.6] - 2025-09-13

### Features

- Automatic audio language metadata for embedded audio tracks
- Add quality-based CDM selection for dynamic CDM switching

### Bug Fixes

- Resolve service name transmission and vault case sensitivity issues
- Improve import ordering and code formatting

### Maintenance

- Bump version to 1.4.6 and update changelog

## [1.4.5] - 2025-09-09

### Features

- *changelog*: Update changelog for version 1.4.4 with enhanced CDM support, configuration options, and various improvements
- *cdm*: Enhance key retrieval logic and improve cached keys handling
- Implement intelligent caching system for CDM license requests
- *tags*: Enhance tag handling for TV shows and movies from Simkl data
- *kv*: Enhance vault loading and key copying logic
- *dl*: Truncate PSSH string for display in non-debug mode
- *cdm*: Add fallback to Widevine common cert for L1 devices
- *cdm*: Optimize get_cached_keys_if_exists for L1/L2 devices
- *cdm*: Update User-Agent to use dynamic version

### Bug Fixes

- *tags*: Fix import order.
- *cdm*: Add error message for missing service certificate in CDM session
- *tags*: Fix formatting issues

### Maintenance

- Bump version to 1.4.5 and update changelog

## [1.4.4] - 2025-09-02

### Features

- *ip-info*: Add cached IP info retrieval with fallback tester to avoid rate limiting
- *ip-info*: Fix few more issues with the get_ip_info make sure we failover to different provider on 429 errors and allow future for more API providers to be added later.
- *release*: Bump version to 1.4.3 and update changelog with new features and improvements
- *config*: Add new configuration options for device certificate status list and language preferences
- *cdm*: Enhance DecryptLabsRemoteCDM to support cached keys and improve license handling
- *cdm*: Enhance DecryptLabsRemoteCDM with improved session management and caching support and better support for remote WV/PR
- *cdm*: Add DecryptLabs CDM configurations for Chrome and PlayReady devices with updated User-Agent and service certificate
- *cdm*: Refactor DecryptLabsRemoteCDM full support for Widevine/Playready and ChromeCDM

### Bug Fixes

- *dependencies*: Remove unnecessary data extra requirement from langcodes
- *main*: As requested old devine version removed from banner to avoid any confusion the developer of this software. Original GNU is still applys.
- *tags*: Fix Matroska tag compliance with official specification

### Changes

- *drm*: Simplify decrypt method by removing unused parameter and streamline logic

## [1.4.2] - 2025-08-14

### Features

- *dl*: Add audio language option to override language for audio tracks
- *vault*: Add no_push option to Vault and its subclasses to control key reception
- *hls*: Enhance segment merging with recursive file search and fallback to binary concatenation
- *hls*: Enhance segment retrieval by allowing all file types and clean up empty segment directories. Fixes issues with VTT files from HLS not being found correctly due to new HLS "changes"
- *config*: Add series_year option to control year inclusion in titles and YAML configuration
- *tags*: Implement session management for API requests with retry logic
- *release*: Bump version to 1.4.2 and update changelog with new features and fixes

### Bug Fixes

- *dl*: Adjust per_language logic to ensure correct audio track selection and not download all tracks for selected language.

## [1.4.1] - 2025-08-08

### Features

- Implement title caching system to reduce API calls
- *dl*: Update language option default to 'orig' if no -l is set, avoids hardcoded en
- *config*: Add options for tagging with group name and IMDB/TMDB details and new API endpoint of simkl if no tmdb api key is added.
- *tags*: Enhance tag_file function to prioritize provided TMDB ID if --tmdb is used
- *changelog*: Update changelog with enhanced tagging configuration and improvements

### Bug Fixes

- *subtitle*: Handle ValueError in subtitle filtering for multiple colons in time references fixes issues with subtitles that contain multiple colons

### Changes

- Remove Dockerfile and .dockerignore from the repository
- *tags*: Simplify Simkl search logic and soft-fail when no results found

## [1.4.0] - 2025-08-05

### Features

- *update_checker*: Enhance update checking logic and cache handling
- *dl*: Add option to include forced subtitle tracks
- *subtitle*: Add filtering for unwanted cues in WebVTT subtitles
- *tracks*: Add support for HLG color transfer characteristics in video arguments
- *dl*: Enhance language selection for video and audio tracks, including original language support
- *dl*: Improve DRM track decryption handling
- *series*: Enhance tree representation with season breakdown
- *hybrid*: Enhance extraction and conversion processes with dymanic spinning bars to follow the rest of the codebase.
- *dl*: Fix track selection to support combining -V, -A, -S flags
- *titles*: Better detection of DV across all codecs in Episode and Movie classes dvhe.05.06 was not being detected correctly.
- *dl*: Add support for services that do not support subtitle downloads
- *playready*: Enhance KID extraction from PSSH with base64 support and XML parsing
- Bump version to 1.4.0 and update changelog with new features and fixes

### Maintenance

- Update changelog with new features, enhancements, and fixes for version 1.3.0
- Bump unshackle version to 1.3.0 in uv.lock

## [1.3.0] - 2025-08-03

### Features

- Add update check interval configuration and implement rate limiting for update checks
- Implement terminal cleanup on exit and signal handling in ComfyConsole
- Add Unspecified_Image option to Transfer enum in Video class.
- Enhance credential management and CDM configuration in unshackle.yaml
- Update path of update_check.json to .gitignore
- Add scene naming option to configuration and update naming logic in titles
- Add unshackle-example.yaml to replace the unshackle.yaml file, you can now make changes to the unshackle.yaml file and pull from the the repo without issues.
- *drm*: Add support for mp4decrypt as a decryption method

### Bug Fixes

- Correct URL handling and improve key retrieval logic in HTTP vault
- Rename 'servers' to 'server_map' for proxy configuration in unshackle.yaml to resolve nord/surfshark incorrect named config

### Changes

- Replace log.exit calls with ValueError exceptions for error handling in Hybrid class

### Maintenance

- Bump version to 1.3.0 and update changelog with mp4decrypt support and enhancements

## [1.2.0] - 2025-07-30

### Features

- *dl*: Enhance hybrid processing to handle HDR10 and DV tracks separately by resolution, Hotfix for -q 2160,1080 both tracks will have Hybrid correctly now.
- *hybrid*: Display resolution of HDR10 track in hybrid mode console output and clean up unused code
- *subtitle*: Add information into unshackle.yaml on how to use new Subby subtitle conversion.
- *vaults*: Enhance vault loading with success status
- *attachment*: Ensure temporary directory is created for downloads
- *tracks*: Add duration fix handling for video and hybrid tracks
- *hybrid*: Add HDR10+ support for conversion to Dolby Vision and enhance metadata extraction
- Update version to 1.1.1 and add update checking functionality
- Bump version to 1.2.0 and update changelog, I'll eventually learn symantic versioning.

### Changes

- *env*: Enhance dependency check with detailed categorization and status summary

### Maintenance

- *workflow*: Remove Docker build and publish workflow, its too messy at the moment doing manual builds for now.

## [1.1.0] - 2025-07-30

### Features

- Update version display in main.py
- *proxies*: Add SurfsharkVPN support
- *binaries*: Add support for `MKVToolNix` and `mkvpropedit`
- *subtitles*: Integrate `subby` library for enhanced subtitle processing and conversion methods
- *hybrid*: Implement HDR10+DV hybrid processing and injection support
- *EXAMPLE*: Add support for HDR10 and DV tracks in hybrid mode

### Bug Fixes

- *cfg*: Update services directory handling
- *binaries*: Improve local binary search functionality
- *env*: Update binary search functionality to use `binaries.find`
- *env*: Update `Shaka-Packager` binary retrieval method
- *env*: Improve handling of directory paths in `info` command
- *install*: Improve UV installation process and error handling
- *download*: Skip Content-Length validation for compressed responses in curl_impersonate and requests. The fix ensures that when Content-Encoding indicates compression, we skip the validation by setting content_length = 0, allowing the downloads to complete successfully.
- *dl*: Check for dovi_tool availability in hybrid mode
- *download*: Skip Content-Length validation for compressed responses in curl_impersonate and requests

### Maintenance

- Bump version to 1.1.0 in pyproject.toml, __init__.py, and uv.lock to follow correct Semantic Versioning.
- Add CHANGELOG.md to document notable changes and version history

## [1.0.1] - 2025-07-20

### Features

- Enhance CONFIG.md with new configuration options for curl_impersonate, filenames, n_m3u8dl_re, and nordvpn
- Update .gitignore and enhance README with planned features
- Add .github/ to .gitignore to exclude GitHub-related files
- Implement VideoCodecChoice for enhanced codec selection
- Add Dockerfile and GitHub Actions workflow for building and publishing Docker image
- Update GitHub Actions workflow for Docker image build and add Docker installation instructions to README

### Bug Fixes

- Change default value of set_terminal_bg to False
- Add video_only condition to subtitle track selection logic fixes issues where ccextractor would run even with -V enabled
- Add SubtitleCodecChoice for resolving issues with config clicktype selection, using names like VTT or SRT was not working as expected
- Update shaka packager version and enhance Docker run command with additional volume mounts

### Changes

- Streamline README by removing outdated service and legal information and moved it directly to the WIKI
- Reorganize Planned Features section in README for clarity
- Improve track selection logic in dl.py

[unreleased]: https://github.com/unshackle-dl/unshackle/compare/2.3.0..HEAD
[2.3.0]: https://github.com/unshackle-dl/unshackle/compare/2.2.0..2.3.0
[2.2.0]: https://github.com/unshackle-dl/unshackle/compare/2.1.0..2.2.0
[2.1.0]: https://github.com/unshackle-dl/unshackle/compare/2.0.0..2.1.0
[2.0.0]: https://github.com/unshackle-dl/unshackle/compare/1.4.8..2.0.0
[1.4.8]: https://github.com/unshackle-dl/unshackle/compare/1.4.7..1.4.8
[1.4.7]: https://github.com/unshackle-dl/unshackle/compare/1.4.6..1.4.7
[1.4.6]: https://github.com/unshackle-dl/unshackle/compare/1.4.5..1.4.6
[1.4.5]: https://github.com/unshackle-dl/unshackle/compare/1.4.4..1.4.5
[1.4.4]: https://github.com/unshackle-dl/unshackle/compare/1.4.2..1.4.4
[1.4.2]: https://github.com/unshackle-dl/unshackle/compare/1.4.1..1.4.2
[1.4.1]: https://github.com/unshackle-dl/unshackle/compare/1.4.0..1.4.1
[1.4.0]: https://github.com/unshackle-dl/unshackle/compare/1.3.0..1.4.0
[1.3.0]: https://github.com/unshackle-dl/unshackle/compare/1.2.0..1.3.0
[1.2.0]: https://github.com/unshackle-dl/unshackle/compare/1.1.0..1.2.0
[1.1.0]: https://github.com/unshackle-dl/unshackle/compare/1.0.1..1.1.0
