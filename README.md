<p align="center">
    <img width="16" height="16" alt="no_encryption" src="https://github.com/user-attachments/assets/6ff88473-0dd2-4bbc-b1ea-c683d5d7a134" /> unshackle
    <br/>
    <sup><em>Movie, TV, and Music Archival Software</em></sup>
    <br/>
      <a href="https://discord.gg/mHYyPaCbFK">
        <img src="https://img.shields.io/discord/1395571732001325127?label=&logo=discord&logoColor=ffffff&color=7289DA&labelColor=7289DA" alt="Discord">
    </a>
</p>

## What is unshackle?

unshackle is a fork of [Devine](https://github.com/devine-dl/devine/), a powerful archival tool for downloading movies, TV shows, and music from streaming services. Built with a focus on modularity and extensibility, it provides a robust framework for content acquisition with support for DRM-protected content.

## Key Features

- 🚀 **Easy Installation** - Simple UV installation
- 🎥 **Multi-Media Support** - Movies, TV episodes, and music
- 🛠️ **Built-in Parsers** - DASH/HLS and ISM manifest support
- 🔒 **DRM Support** - Widevine and PlayReady integration
- 🌈 **HDR10+DV Hybrid** - Hybrid Dolby Vision injection via [dovi_tool](https://github.com/quietvoid/dovi_tool)
- 💾 **Flexible Storage** - Local and remote key vaults
- 👥 **Multi-Profile Auth** - Support for cookies and credentials
- 🤖 **Smart Naming** - Automatic P2P-style filename structure
- ⚙️ **Configurable** - YAML-based configuration
- ❤️ **Open Source** - Fully open-source with community contributions welcome

## Quick Start

### Installation

This installs the latest version directly from the GitHub repository:

```shell
git clone https://github.com/unshackle-dl/unshackle.git
cd unshackle
uv sync
uv run unshackle --help
```

### Install unshackle as a global (per-user) tool

```bash
uv tool install git+https://github.com/unshackle-dl/unshackle.git
# Then run:
uvx unshackle --help   # or just `unshackle` once PATH updated
```

> [!NOTE]
> After installation, you may need to add the installation path to your PATH environment variable if prompted.

> **Recommended:** Use `uv run unshackle` instead of direct command execution to ensure proper virtual environment activation.

## Planned Features

- 🖥️ **Web UI Access & Control** - Manage and control unshackle from a modern web interface.
- 🔄 **Sonarr/Radarr Interactivity** - Direct integration for automated personal downloads.
- ⚙️ **Better ISM Support** - Improve on ISM support for multiple services
- 🔉 **ATMOS** - Better Atmos Support/Selection
- 🎵 **Music** - Cleanup Audio Tagging using the [tags.py](unshackle/core/utils/tags.py) for artist/track name etc.

### Basic Usage

```shell
# Check available commands
uv run unshackle --help

# Configure your settings
git clone https://github.com/unshackle-dl/unshackle.git
cd unshackle
uv sync
uv run unshackle --help

# Download content (requires configured services)
uv run unshackle dl SERVICE_NAME CONTENT_ID
```

## Documentation

For comprehensive setup guides, configuration options, and advanced usage:

📖 **[Visit our WIKI](https://github.com/unshackle-dl/unshackle/wiki)**

The WIKI contains detailed information on:

- Service configuration
- DRM configuration
- Advanced features and troubleshooting

For guidance on creating services, see our [WIKI documentation](https://github.com/unshackle-dl/unshackle/wiki).

## End User License Agreement

unshackle and it's community pages should be treated with the same kindness as other projects.
Please refrain from spam or asking for questions that infringe upon a Service's End User License Agreement.

1. Do not use unshackle for any purposes of which you do not have the rights to do so.
2. Do not share or request infringing content; this includes widevine Provision Keys, Content Encryption Keys,
   or Service API Calls or Code.
3. The Core codebase is meant to stay Free and Open-Source while the Service code should be kept private.
4. Do not sell any part of this project, neither alone nor as part of a bundle.
   If you paid for this software or received it as part of a bundle following payment, you should demand your money
   back immediately.
5. Be kind to one another and do not single anyone out.

## Licensing

This software is licensed under the terms of [GNU General Public License, Version 3.0](LICENSE).  
You can find a copy of the license in the LICENSE file in the root folder.
