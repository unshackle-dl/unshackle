# Development

This project is managed using [UV](https://github.com/astral-sh/uv), a fast Python package and project manager.
Install the latest version of UV before continuing. Development requires Python 3.11 to 3.14.

## Set up

Starting from Zero? Not sure where to begin? Here are the steps to set up this Python project using UV. Note that
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

4. Install the pre-commit hooks, which check each commit:

   ```shell
   uv run pre-commit install
   ```

## Checks

CI runs these checks on each pull request and on each push to `dev` and `main`. Run them before you open a pull request.

1. Install the test dependencies. `uv sync` alone installs only the `dev` group:

   ```shell
   uv sync --all-groups
   ```

2. Run the unit tests. The markers skip the tests that need a live server or the network:

   ```shell
   uv run pytest -n auto --dist worksteal -m "not live and not slow"
   ```

3. Run the linters, the type check and the security lint:

   ```shell
   uv run ruff check .
   uv run ruff format --check .
   uv run isort --check-only unshackle tests --skip-glob "*/services/*"
   uv run mypy unshackle
   uv run bandit -q -c pyproject.toml -r unshackle --severity-level medium --confidence-level medium
   ```

To get a coverage report, run the tests with the coverage option:

```shell
uv run pytest -n auto --dist worksteal -m "not live and not slow" --cov
```
