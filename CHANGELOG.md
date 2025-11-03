# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.0] - Unreleased

### Breaking Changes

- **REST API Integration**: Core architecture modified to support REST API functionality
  - Changes to internal APIs for download management and tracking
  - Title and track classes updated with API integration points
  - Core component interfaces modified for queue management support
- **Configuration Changes**: New required configuration options for API and enhanced features
  - Added `simkl_client_id` now required for Simkl functionality
  - Service-specific configuration override structure introduced
  - Debug logging configuration options added
- **Forced Subtitles**: Behavior change for forced subtitle inclusion
  - Forced subs no longer auto-included, requires explicit `--forced-subs` flag

### Added

- **REST API Server**: Complete download management via REST API (early development)
  - Implemented download queue management and worker system
  - Added OpenAPI/Swagger documentation for easy API exploration
  - Included download progress tracking and status endpoints
  - API authentication and comprehensive error handling
  - Updated core components to support API integration
  - Early development work with more changes planned
- **CustomRemoteCDM**: Highly configurable custom CDM API support
  - Support for third-party CDM API providers with maximum configurability
  - Full configuration through YAML without code changes
  - Addresses GitHub issue #26 for flexible CDM integration
- **WindscribeVPN Proxy Provider**: New VPN provider support
  - Added WindscribeVPN following NordVPN and SurfsharkVPN patterns
  - Fixes GitHub issue #29
- **Latest Episode Download**: New `--latest-episode` CLI option
  - `-le, --latest-episode` flag to download only the most recent episode
  - Automatically selects the single most recent episode regardless of season
  - Fixes GitHub issue #28
- **Service-Specific Configuration Overrides**: Per-service fine-tuned control
  - Support for per-service configuration overrides in YAML
  - Fine-tuned control of downloader and command options per service
  - Fixes GitHub issue #13
- **Comprehensive JSON Debug Logging**: Structured logging for troubleshooting
  - Binary toggle via `--debug` flag or `debug: true` in config
  - JSON Lines (.jsonl) format for easy parsing and analysis
  - Comprehensive logging of all operations (session info, CLI params, CDM details, auth status, title/track metadata, DRM operations, vault queries)
  - Configurable key logging via `debug_keys` option with smart redaction
  - Error logging for all critical operations
  - Removed old text logging system
- **curl_cffi Retry Handler**: Enhanced session reliability
  - Added automatic retry mechanism to curl_cffi Session
  - Improved download reliability with configurable retries
- **Simkl API Configuration**: New API key support
  - Added `simkl_client_id` configuration option
  - Simkl now requires client_id from https://simkl.com/settings/developer/
- **Custom Session Fingerprints**: Enhanced browser impersonation capabilities
  - Added custom fingerprint and preset support for better service compatibility
  - Configurable fingerprint presets for different device types
  - Improved success rate with services using advanced bot detection
- **TMDB and Simkl Metadata Caching**: Enhanced title cache system
  - Added metadata caching to title cache to reduce API calls
  - Caches movie/show metadata alongside title information
  - Improves performance for repeated title lookups and reduces API rate limiting
- **API Enhancements**: Improved REST API functionality
  - Added default parameter handling for better request processing
  - Added URL field to services endpoint response for easier service identification
  - Complete API enhancements for production readiness
  - Improved error responses with better detail and debugging information

### Changed

- **Binary Search Enhancement**: Improved binary discovery
  - Refactored to search for binaries in root of binary folder or subfolder named after the binary
  - Better organization of binary dependencies
- **Type Annotations**: Modernized to PEP 604 syntax
  - Updated session.py type annotations to use modern Python syntax
  - Improved code readability and type checking

### Fixed

- **Audio Description Track Support**: Added option to download audio description tracks
  - Added `--audio-description/-ad` flag to optionally include descriptive audio tracks
  - Previously, audio description tracks were always filtered out
  - Users can now choose to download AD tracks when needed
  - Fixes GitHub issue #33
- **Config Directory Support**: Cross-platform user config directory support
  - Fixed config loading to properly support user config directories across all platforms
  - Fixes GitHub issue #23
- **HYBRID Mode Validation**: Pre-download validation for hybrid processing
  - Added validation to check both HDR10 and DV tracks are available before download
  - Prevents wasted downloads when hybrid processing would fail
- **TMDB/Simkl API Keys**: Graceful handling of missing API keys
  - Improved error handling when TMDB or Simkl API keys are not configured
  - Better user messaging for API configuration requirements
- **Forced Subtitles Behavior**: Correct forced subtitle filtering
  - Fixed forced subtitles being incorrectly included without `--forced-subs` flag
  - Forced subs now only included when explicitly requested
- **Font Attachment Constructor**: Fixed ASS/SSA font attachment
  - Use keyword arguments for Attachment constructor in font attachment
  - Fixes "Invalid URL: No scheme supplied" error
  - Fixes GitHub issue #24
- **Binary Subdirectory Checking**: Enhanced binary location discovery (by @TPD94, PR #19)
  - Updated binaries.py to check subdirectories in binaries folders named after the binary
  - Improved binary detection and loading
- **HLS Manifest Processing**: Minor HLS parser fix (by @TPD94, PR #19)
- **lxml and pyplayready**: Updated dependencies (by @Sp5rky)
  - Updated lxml constraint and pyplayready import path for compatibility
- **DASH Segment Calculation**: Corrected segment handling
  - Fixed segment count calculation for DASH manifests with startNumber=0
  - Ensures accurate segment processing for all DASH manifest configurations
  - Prevents off-by-one errors in segment downloads
- **HDR Detection and Naming**: Comprehensive HDR format support
  - Improved HDR detection with comprehensive transfer characteristics checks
  - Added hybrid DV+HDR10 support for accurate file naming
  - Better identification of HDR formats across different streaming services
  - More accurate HDR/DV detection in filename generation
- **Subtitle Processing**: VTT subtitle handling improvements
  - Resolved SDH (Subtitles for Deaf and Hard of hearing) stripping crash when processing VTT files
  - More robust subtitle processing pipeline with better error handling
  - Fixes crashes when filtering specific VTT subtitle formats
- **DRM Processing**: Enhanced encoding handling
  - Added explicit UTF-8 encoding to mp4decrypt subprocess calls
  - Prevents encoding issues on systems with non-UTF-8 default encodings
  - Improves cross-platform compatibility for Windows and some Linux configurations
- **Session Fingerprints**: Updated OkHttp presets
  - Updated OkHttp fingerprint presets for better Android TV compatibility
  - Improved success rate with services using fingerprint-based detection

### Documentation

- **GitHub Issue Templates**: Enhanced issue reporting
  - Improved bug report template with better structure and required fields
  - Enhanced feature request template for clearer specifications
  - Added helpful guidance for contributors to provide complete information

### Refactored

- **Import Cleanup**: Removed unused imports
  - Removed unused mypy import from binaries.py
  - Fixed import ordering in API download_manager and handlers

### Contributors

This release includes contributions from:

- @Sp5rky - REST API server implementation, dependency updates
- @stabbedbybrick - curl_cffi retry handler (PR #31)
- @TPD94 - Binary search enhancements, manifest parser fixes (PR #19)
- @scene (Andy) - Core features, configuration system, bug fixes

## [1.4.8] - 2025-10-08

### Added

- **Exact Language Matching**: New `--exact-lang` flag for precise language matching
  - Enables strict language code matching without fallbacks
- **No-Mux Flag**: New `--no-mux` flag to skip muxing tracks into container files
  - Useful for keeping individual track files separate
- **DecryptLabs API Integration for HTTP Vault**: Enhanced vault support
  - Added DecryptLabs API support to HTTP vault for improved key retrieval
- **AC4 Audio Codec Support**: Enhanced audio format handling
  - Added AC4 codec support in Audio class with updated mime/profile handling
- **pysubs2 Subtitle Conversion**: Extended subtitle format support
  - Added pysubs2 subtitle conversion with extended format support
  - Configurable conversion method in configuration

### Changed

- **Audio Track Sorting**: Optimized audio track selection logic
  - Improved audio track sorting by grouping descriptive tracks and sorting by bitrate
  - Better identification of ATMOS and DD+ as highest quality for filenaming
- **pyplayready Update**: Upgraded to version 0.6.3
  - Updated import paths to resolve compatibility issues
  - Fixed lxml constraints for better dependency management
- **pysubs2 Conversion Method**: Moved from auto to manual configuration
  - pysubs2 no longer auto-selected during testing phase

### Fixed

- **Remote CDM**: Fixed curl_cffi compatibility
  - Added curl_cffi to instance checks in RemoteCDM
- **Temporary File Handling**: Improved encoding handling
  - Specified UTF-8 encoding when opening temporary files

### Reverted

- **tinycss SyntaxWarning Suppression**: Removed ineffective warning filter
  - Reverted warnings filter that didn't work as expected for suppressing tinycss warnings

## [1.4.7] - 2025-09-25

### Added

- **curl_cffi Session Support**: Enhanced anti-bot protection with browser impersonation
  - Added new session utility with curl_cffi support for bypassing anti-bot measures
  - Browser impersonation support for Chrome, Firefox, and Safari user agents
  - Full backward compatibility with requests.Session maintained
  - Suppressed HTTPS proxy warnings for improved user experience
- **Download Retry Functionality**: Configurable retry mechanism for failed downloads
  - Added retry count option to download function for improved reliability
- **Subtitle Requirements Options**: Enhanced subtitle download control
  - Added options for required subtitles in download command
  - Better control over subtitle track selection and requirements
- **Quality Selection Enhancement**: Improved quality selection options
  - Added best available quality option in download command for optimal track selection
- **DecryptLabs API Integration**: Enhanced remote CDM configuration
  - Added decrypt_labs_api_key to Config initialization for better API integration

### Changed

- **Manifest Parser Updates**: Enhanced compatibility across all parsers
  - Updated DASH, HLS, ISM, and M3U8 parsers to accept curl_cffi sessions
  - Improved cookie handling compatibility between requests and curl_cffi
- **Logging Improvements**: Reduced log verbosity for better user experience
  - Changed duplicate track log level to debug to reduce console noise
  - Dynamic CDM selection messages moved to debug-only output

### Fixed

- **Remote CDM Reuse**: Fixed KeyError in dynamic CDM selection
  - Prevents KeyError when reusing remote CDMs in dynamic selection process
  - Creates copy of CDM dictionary before modification to prevent configuration mutation
  - Allows same CDM to be selected multiple times within session without errors

## [1.4.6] - 2025-09-13

### Added

- **Quality-Based CDM Selection**: Dynamic CDM selection based on video resolution
  - Automatically selects appropriate CDM (L3/L1) based on video track quality
  - Supports quality thresholds in configuration (>=, >, <=, <, exact match)
  - Pre-selects optimal CDM based on highest quality across all video tracks
  - Maintains backward compatibility with existing CDM configurations
- **Automatic Audio Language Metadata**: Intelligent embedded audio language detection
  - Automatically sets audio language metadata when no separate audio tracks exist
  - Smart video track selection based on title language with fallbacks
  - Enhanced FFmpeg repackaging with audio stream metadata injection
- **Lazy DRM Loading**: Deferred DRM loading for multi-track key retrieval optimization
  - Add deferred DRM loading to M3U8 parser to mark tracks for later processing
  - Just-in-time DRM loading during download process for better performance

### Changed

- **Enhanced CDM Management**: Improved CDM switching logic for multi-quality downloads
  - CDM selection now based on highest quality track to avoid inefficient switching
  - Quality-based selection only within same DRM type (Widevine-to-Widevine, PlayReady-to-PlayReady)
  - Single CDM used per session for better performance and reliability

### Fixed

- **Vault Caching Issues**: Fixed vault count display and NoneType iteration errors
  - Fix 'NoneType' object is not iterable error in DecryptLabsRemoteCDM
  - Fix vault count display showing 0/3 instead of actual successful vault count
- **Service Name Transmission**: Resolved DecryptLabsRemoteCDM service name issues
  - Fixed DecryptLabsRemoteCDM sending 'generic' instead of proper service names
  - Added case-insensitive vault lookups for SQLite/MySQL vaults
  - Added local vault integration to DecryptLabsRemoteCDM
- **Import Organization**: Improved import ordering and code formatting
  - Reorder imports in decrypt_labs_remote_cdm.py for better organization
  - Clean up trailing whitespace in vault files

### Configuration

- **New CDM Configuration Format**: Extended `cdm:` section supports quality-based selection
  ```yaml
  cdm:
    SERVICE_NAME:
      "<=1080": l3_cdm_name
      ">1080": l1_cdm_name
      default: l3_cdm_name
  ```

## [1.4.5] - 2025-09-09

### Added

- **Enhanced CDM Key Caching**: Improved key caching and session management for L1/L2 devices
  - Optimized `get_cached_keys_if_exists` functionality for better performance with L1/L2 devices
  - Enhanced cached key retrieval logic with improved session handling
- **Widevine Common Certificate Fallback**: Added fallback to Widevine common certificate for L1 devices
  - Improved compatibility for L1 devices when service certificates are unavailable
- **Enhanced Vault Loading**: Improved vault loading and key copying logic
  - Better error handling and key management in vault operations
- **PSSH Display Optimization**: Truncated PSSH string display in non-debug mode for cleaner output
- **CDM Error Messaging**: Added error messages for missing service certificates in CDM sessions

### Changed

- **Dynamic Version Headers**: Updated User-Agent headers to use dynamic version strings
  - DecryptLabsRemoteCDM now uses dynamic version import instead of hardcoded version
- **Intelligent CDM Caching**: Implemented intelligent caching system for CDM license requests
  - Enhanced caching logic reduces redundant license requests and improves performance
- **Enhanced Tag Handling**: Improved tag handling for TV shows and movies from Simkl data
  - Better metadata processing and formatting for improved media tagging

### Fixed

- **CDM Session Management**: Clean up session data when retrieving cached keys
  - Remove decrypt_labs_session_id and challenge from session when cached keys exist but there are missing kids
  - Ensures clean state for subsequent requests and prevents session conflicts
- **Tag Formatting**: Fixed formatting issues in tag processing
- **Import Order**: Fixed import order issues in tags module

## [1.4.4] - 2025-09-02

### Added

- **Enhanced DecryptLabs CDM Support**: Comprehensive remote CDM functionality
  - Full support for Widevine, PlayReady, and ChromeCDM through DecryptLabsRemoteCDM
  - Enhanced session management and caching support for remote WV/PR operations
  - Support for cached keys and improved license handling
  - New CDM configurations for Chrome and PlayReady devices with updated User-Agent and service certificate
- **Advanced Configuration Options**: New device and language preferences
  - Added configuration options for device certificate status list
  - Enhanced language preference settings

### Changed

- **DRM Decryption Enhancements**: Streamlined decryption process
  - Simplified decrypt method by removing unused parameter and streamlined logic
  - Improved DecryptLabs CDM configurations with better device support

### Fixed

- **Matroska Tag Compliance**: Enhanced media container compatibility
  - Fixed Matroska tag compliance with official specification
- **Application Branding**: Cleaned up version display
  - Removed old devine version reference from banner to avoid developer confusion
  - Updated branding while maintaining original GNU license compliance
- **IP Information Handling**: Improved geolocation services
  - Enhanced get_ip_info functionality with better failover handling
  - Added support for 429 error handling and multiple API provider fallback
  - Implemented cached IP info retrieval with fallback tester to avoid rate limiting
- **Dependencies**: Streamlined package requirements
  - Removed unnecessary data extra requirement from langcodes

### Removed

- Deprecated version references in application banner for clarity

## [1.4.3] - 2025-08-20

### Added

- Cached IP info helper for region detection
  - New `get_cached_ip_info()` with 24h cache and provider rotation (ipinfo/ipapi) with 429 handling.
  - Reduces external calls and stabilizes non-proxy region lookups for caching/logging.

### Changed

- DRM decryption selection is fully configuration-driven
  - Widevine and PlayReady now select the decrypter based solely on `decryption` in YAML (including per-service mapping).
  - Shaka Packager remains the default decrypter when not specified.
  - `dl.py` logs the chosen tool based on the resolved configuration.
- Geofencing and proxy verification improvements
  - Safer geofence checks with error handling and clearer logs.
  - Always verify proxy exit region via live IP lookup; fallback to proxy parsing on failure.
- Example config updated to default to Shaka
  - `unshackle.yaml`/example now sets `decryption.default: shaka` (service overrides still supported).

### Removed

- Deprecated parameter `use_mp4decrypt`
  - Removed from `Widevine.decrypt()` and `PlayReady.decrypt()` and all callsites.
  - Internal naming switched from mp4decrypt-specific flags to generic `decrypter` selection.

## [1.4.2] - 2025-08-14

### Added

- **Session Management for API Requests**: Enhanced API reliability with retry logic
  - Implemented session management for tags functionality with automatic retry mechanisms
  - Improved API request stability and error handling
- **Series Year Configuration**: New `series_year` option for title naming control
  - Added configurable `series_year` option to control year inclusion in series titles
  - Enhanced YAML configuration with series year handling options
- **Audio Language Override**: New audio language selection option
  - Added `audio_language` option to override default language selection for audio tracks
  - Provides more granular control over audio track selection
- **Vault Key Reception Control**: Enhanced vault security options
  - Added `no_push` option to Vault and its subclasses to control key reception
  - Improved key management security and flexibility

### Changed

- **HLS Segment Processing**: Enhanced segment retrieval and merging capabilities
  - Enhanced segment retrieval to allow all file types for better compatibility
  - Improved segment merging with recursive file search and fallback to binary concatenation
  - Fixed issues with VTT files from HLS not being found correctly due to format changes
  - Added cleanup of empty segment directories after processing
- **Documentation**: Updated README.md with latest information

### Fixed

- **Audio Track Selection**: Improved per-language logic for audio tracks
  - Adjusted `per_language` logic to ensure correct audio track selection
  - Fixed issue where all tracks for selected language were being downloaded instead of just the intended ones

## [1.4.1] - 2025-08-08

### Added

- **Title Caching System**: Intelligent title caching to reduce redundant API calls
  - Configurable title caching with 30-minute default cache duration
  - 24-hour fallback cache on API failures for improved reliability
  - Region-aware caching to handle geo-restricted content properly
  - SHA256 hashing for cache keys to handle complex title IDs
  - Added `--no-cache` CLI flag to bypass caching when needed
  - Added `--reset-cache` CLI flag to clear existing cache data
  - New cache configuration variables in config system
  - Documented caching options in example configuration file
  - Significantly improves performance when debugging or modifying CLI parameters
- **Enhanced Tagging Configuration**: New options for customizing tag behavior
  - Added `tag_group_name` config option to control group name inclusion in tags
  - Added `tag_imdb_tmdb` config option to control IMDB/TMDB details in tags
  - Added Simkl API endpoint support as fallback when no TMDB API key is provided
  - Enhanced tag_file function to prioritize provided TMDB ID when `--tmdb` flag is used
  - Improved TMDB ID handling with better prioritization logic

### Changed

- **Language Selection Enhancement**: Improved default language handling
  - Updated language option default to 'orig' when no `-l` flag is set
  - Avoids hardcoded 'en' default and respects original content language
- **Tagging Logic Improvements**: Simplified and enhanced tagging functionality
  - Simplified Simkl search logic with soft-fail when no results found
  - Enhanced tag_file function with better TMDB ID prioritization
  - Improved error handling in tagging operations

### Fixed

- **Subtitle Processing**: Enhanced subtitle filtering for edge cases
  - Fixed ValueError in subtitle filtering for multiple colons in time references
  - Improved handling of subtitles containing complex time formatting
  - Better error handling for malformed subtitle timestamps

### Removed

- **Docker Support**: Removed Docker configuration from repository
  - Removed Dockerfile and .dockerignore files
  - Cleaned up README.md Docker-related documentation
  - Focuses on direct installation methods

## [1.4.0] - 2025-08-05

### Added

- **HLG Transfer Characteristics Preservation**: Enhanced video muxing to preserve HLG color metadata
  - Added automatic detection of HLG video tracks during muxing process
  - Implemented `--color-transfer-characteristics 0:18` argument for mkvmerge when processing HLG content
  - Prevents incorrect conversion from HLG (18) to BT.2020 (14) transfer characteristics
  - Ensures proper HLG playback support on compatible hardware without manual editing
- **Original Language Support**: Enhanced language selection with 'orig' keyword support
  - Added support for 'orig' language selector for both video and audio tracks
  - Automatically detects and uses the title's original language when 'orig' is specified
  - Improved language processing logic with better duplicate handling
  - Enhanced help text to document original language selection usage
- **Forced Subtitle Support**: Added option to include forced subtitle tracks
  - New functionality to download and include forced subtitle tracks alongside regular subtitles
- **WebVTT Subtitle Filtering**: Enhanced subtitle processing capabilities
  - Added filtering for unwanted cues in WebVTT subtitles
  - Improved subtitle quality by removing unnecessary metadata

### Changed

- **DRM Track Decryption**: Improved DRM decryption track selection logic
  - Enhanced `get_drm_for_cdm()` method usage for better DRM-CDM matching
  - Added warning messages when no matching DRM is found for tracks
  - Improved error handling and logging for DRM decryption failures
- **Series Tree Representation**: Enhanced episode tree display formatting
  - Updated series tree to show season breakdown with episode counts
  - Improved visual representation with "S{season}({count})" format
  - Better organization of series information in console output
- **Hybrid Processing UI**: Enhanced extraction and conversion processes
  - Added dynamic spinning bars to follow the rest of the codebase design
  - Improved visual feedback during hybrid HDR processing operations
- **Track Selection Logic**: Enhanced multi-track selection capabilities
  - Fixed track selection to support combining -V, -A, -S flags properly
  - Improved flexibility in selecting multiple track types simultaneously
- **Service Subtitle Support**: Added configuration for services without subtitle support
  - Services can now indicate if they don't support subtitle downloads
  - Prevents unnecessary subtitle download attempts for unsupported services
- **Update Checker**: Enhanced update checking logic and cache handling
  - Improved rate limiting and caching mechanisms for update checks
  - Better performance and reduced API calls to GitHub

### Fixed

- **PlayReady KID Extraction**: Enhanced KID extraction from PSSH data
  - Added base64 support and XML parsing for better KID detection
  - Fixed issue where only one KID was being extracted for certain services
  - Improved multi-KID support for PlayReady protected content
- **Dolby Vision Detection**: Improved DV codec detection across all formats
  - Fixed detection of dvhe.05.06 codec which was not being recognized correctly
  - Enhanced detection logic in Episode and Movie title classes
  - Better support for various Dolby Vision codec variants

## [1.3.0] - 2025-08-03

### Added

- **mp4decrypt Support**: Alternative DRM decryption method using mp4decrypt from Bento4
  - Added `mp4decrypt` binary detection and support in binaries module
  - New `decryption` configuration option in unshackle.yaml for service-specific decryption methods
  - Enhanced PlayReady and Widevine DRM classes with mp4decrypt decryption support
  - Service-specific decryption mapping allows choosing between `shaka` and `mp4decrypt` per service
  - Improved error handling and progress reporting for mp4decrypt operations
- **Scene Naming Configuration**: New `scene_naming` option for controlling file naming conventions
  - Added scene naming logic to movie, episode, and song title classes
  - Configurable through unshackle.yaml to enable/disable scene naming standards
- **Terminal Cleanup and Signal Handling**: Enhanced console management
  - Implemented proper terminal cleanup on application exit
  - Added signal handling for graceful shutdown in ComfyConsole
- **Configuration Template**: New `unshackle-example.yaml` template file
  - Replaced main `unshackle.yaml` with example template to prevent git conflicts
  - Users can now modify their local config without affecting repository updates
- **Enhanced Credential Management**: Improved CDM and vault configuration
  - Expanded credential management documentation in configuration
  - Enhanced CDM configuration examples and guidelines
- **Video Transfer Standards**: Added `Unspecified_Image` option to Transfer enum
  - Implements ITU-T H.Sup19 standard value 2 for image characteristics
  - Supports still image coding systems and unknown transfer characteristics
- **Update Check Rate Limiting**: Enhanced update checking system
  - Added configurable update check intervals to prevent excessive API calls
  - Improved rate limiting for GitHub API requests

### Changed

- **DRM Decryption Architecture**: Enhanced decryption system with dual method support
  - Updated `dl.py` to handle service-specific decryption method selection
  - Refactored `Config` class to manage decryption method mapping per service
  - Enhanced DRM decrypt methods with `use_mp4decrypt` parameter for method selection
- **Error Handling**: Improved exception handling in Hybrid class
  - Replaced log.exit calls with ValueError exceptions for better error propagation
  - Enhanced error handling consistency across hybrid processing

### Fixed

- **Proxy Configuration**: Fixed proxy server mapping in configuration
  - Renamed 'servers' to 'server_map' in proxy configuration to resolve Nord/Surfshark naming conflicts
  - Updated configuration structure for better compatibility with proxy providers
- **HTTP Vault**: Improved URL handling and key retrieval logic
  - Fixed URL processing issues in HTTP-based key vaults
  - Enhanced key retrieval reliability and error handling

## [1.2.0] - 2025-07-30

### Added

- **Update Checker**: Automatic GitHub release version checking on startup
  - Configurable update notifications via `update_checks` setting in unshackle.yaml
  - Non-blocking HTTP requests with 5-second timeout for performance
  - Smart semantic version comparison supporting all version formats (x.y.z, x.y, x)
  - Graceful error handling for network issues and API failures
  - User-friendly update notifications with current → latest version display
  - Direct links to GitHub releases page for easy updates
- **HDR10+ Support**: Enhanced HDR10+ metadata processing for hybrid tracks
  - HDR10+ tool binary support (`hdr10plus_tool`) added to binaries module
  - HDR10+ to Dolby Vision conversion capabilities in hybrid processing
  - Enhanced metadata extraction for HDR10+ content
- **Duration Fix Handling**: Added duration correction for video and hybrid tracks
- **Temporary Directory Management**: Automatic creation of temp directories for attachment downloads

### Changed

- Enhanced configuration system with new `update_checks` boolean option (defaults to true)
- Updated sample unshackle.yaml with update checker configuration documentation
- Improved console styling consistency using `bright_black` for dimmed text
- **Environment Dependency Check**: Complete overhaul with detailed categorization and status summary
  - Organized dependencies by category (Core, HDR, Download, Subtitle, Player, Network)
  - Enhanced status reporting with compact summary display
  - Improved tool requirement tracking and missing dependency alerts
- **Hybrid Track Processing**: Significant improvements to HDR10+ and Dolby Vision handling
  - Enhanced metadata extraction and processing workflows
  - Better integration with HDR processing tools

### Removed

- **Docker Workflow**: Removed Docker build and publish GitHub Actions workflow for manual builds

## [1.1.0] - 2025-07-29

### Added

- **HDR10+DV Hybrid Processing**: New `-r HYBRID` command for processing HDR10 and Dolby Vision tracks
  - Support for hybrid HDR processing and injection using dovi_tool
  - New hybrid track processing module for seamless HDR10/DV conversion
  - Automatic detection and handling of HDR10 and DV metadata
- Support for HDR10 and DV tracks in hybrid mode for EXAMPLE service
- Binary availability check for dovi_tool in hybrid mode operations
- Enhanced track processing capabilities for HDR content

### Fixed

- Import order issues and missing json import in hybrid processing
- UV installation process and error handling improvements
- Binary search functionality updated to use `binaries.find`

### Changed

- Updated package version from 1.0.2 to 1.1.0
- Enhanced dl.py command processing for hybrid mode support
- Improved core titles (episode/movie) processing for HDR content
- Extended tracks module with hybrid processing capabilities
