# Development

This project is managed using [UV](https://github.com/astral-sh/uv), a fast Python package and project manager.
Install the latest version of UV before continuing. Development currently requires Python 3.9+.

## Set up

Starting from Zero? Not sure where to begin? Here's steps on setting up this Python project using UV. Note that
UV installation instructions should be followed from the UV Docs: https://docs.astral.sh/uv/getting-started/installation/

1. Clone the Repository:

   ```shell
   git clone https://github.com/unshackle-dl/unshackle
   cd unshackle
   ```

2. Install the Project with UV:

   ```shell
   uv sync
   ```

   This creates a Virtual environment and then installs all project dependencies and executables into the Virtual
   environment. Your System Python environment is not affected at all.

3. Run commands in the Virtual environment:

   ```shell
   uv run unshackle
   ```

   Note:

   - UV automatically manages the virtual environment for you - no need to manually activate it
   - You can use `uv run` to prefix any command you wish to run under the Virtual environment
   - For example: `uv run unshackle --help` to run the main application
   - JetBrains PyCharm and Visual Studio Code will automatically detect the UV-managed virtual environment
   - For more information, see: https://docs.astral.sh/uv/concepts/projects/

4. Install Pre-commit tooling to ensure safe and quality commits:

   ```shell
   uv run pre-commit install
   ```
