# Project Guidelines

## Documentation

- Write all project documentation in English, including the root README and the shared assets README.
- Keep READMEs focused on what the project does, how to install and use it, the interfaces it provides, and limitations that affect users.
- Link only to general modules or asset categories from top-level READMEs. Document each asset's paths and usage in its own directory.
- Keep conversation context, development progress, demo sequencing, temporary plans, and internal decisions out of READMEs.
- Keep development constraints in this file. Omit sections with no content.

## Development Scope

- The long-term goal is a shared asset system for high-energy physics. The project is currently at the demo stage.
- Keep analysis workspaces, data transfer, and execution tools in `analysis/` for use by specific analyses. Keep scientific workflows separate from these general-purpose tools.
- Organize shared assets under `assets/` in five categories: knowledge (`knowledge/`), tools (`tools/`), models (`models/`), data (`data/`), and workflows or skills (`workflows/`). The data category can contain datasets or data catalogs. Clients upload tasks, and shared assets are read-only. Keep completed solutions out of shared assets.
- Each scientific tool or example manages its own dependencies and compatibility. Keep environment management out of `analysis/`. Distribute installation materials as read-only tool assets and keep complete examples outside the shared asset tree.
- The bundled Hub is optional. Keep its code and documentation in `hub/` and its dependencies outside the default dependency group. The analysis service always registers with the selected Hub, and clients access analysis tools through that Hub.
- Target Linux for server deployments. Keep public interfaces and application logic platform-independent, and centralize Linux and macOS isolation code. Support service startup on both platforms.
- Use uv to manage the environment. Make dependencies, paths, and installation steps reproducible without relying on private files or a maintainer's preinstalled environment.
- Keep the implementation and file count small. Obtain approval before introducing Docker, a packaging system, or an additional platform.
- Group `.gitignore` rules by purpose and give each group a short comment. Follow the existing grouping style when adding rules.
- Obtain the user's review and explicit approval before each commit.

## Checks

Put module tests in the corresponding module's `tests/` directory and cross-module integration tests in the repository's root `tests/` directory. Each module's tests must run independently with the dependencies they need.

Until the refactor is complete, run these commands from the repository root after changing code or dependencies.

```sh
uv lock --check
uv check --group hub --locked
ruff check --select E,F,I,B,UP hub/
uv format --check
```

- `uv lock --check` verifies that the lockfile matches the dependency declarations without updating the lockfile or installing dependencies.
- `uv check --group hub --locked` runs ty with the Hub dependency group and prevents automatic lockfile updates. It may sync the virtual environment.
- `ruff check --select E,F,I,B,UP hub/` checks for common errors, import ordering, bug-prone patterns, selected style issues, and Python version compatibility.
- `uv format --check` checks formatting without modifying files.

Report specific failures and explain why any check was skipped. Fix failures without weakening rules, suppressing errors, or changing test expectations to hide a problem. Move these checks into CI once the refactor is complete.
