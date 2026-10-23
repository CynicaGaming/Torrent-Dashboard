# Windows executable build and rebuild guide

Torrent Dashboard's Windows executables are generated artifacts. The maintained application source remains under `src/torrent_dashboard/`, with browser assets under `static/`. Do not modify `Dashboard.exe`, `Recovery.exe`, `Updater.exe`, or `_internal/` directly; change the source and rebuild the package instead.

## What gets compiled

The Windows package is built from the same Python source used by the editable/source distribution:

- `src/torrent_dashboard/dashboard.py` → `Dashboard.exe` as a PyInstaller **onedir** application with `_internal/`
- `src/torrent_dashboard/recovery_tool.py` → standalone **onefile** `Recovery.exe`
- `src/torrent_dashboard/updater.py` → standalone **onefile** `Updater.exe`

Frontend files under `static/`, release notes, and selected documentation remain external to the executables and are copied into the packaged folder. Runtime `config.json` and `data/` are not compiled into the executables and are not part of the managed application payload.

## Local prerequisites

Build the Windows package on Windows with:

- Python 3.13
- PyInstaller 6.22.2
- Git/working checkout of the repository

Install the pinned packager:

```powershell
python -m pip install "pyinstaller==6.22.2"
```

## Validate before compiling

From the repository root:

```powershell
$env:PYTHONPATH = "src"
python release_tools/validate_source.py
python release_tools/validate_ui_strings.py
python -m unittest discover -s tests -p "test_*.py"
node --check static/app.js
node --check static/settings.js
```

The application version is defined in `src/torrent_dashboard/__init__.py` as `__version__`. The tag passed to the Windows build command must match that version.

## Build the Windows package

Resolve the version from the checkout so the example stays synchronized:

```powershell
$env:PYTHONPATH = "src"
$version = python -c "from torrent_dashboard import __version__; print(__version__)"
python release_tools/build_windows.py --repo "CynicaGaming/Torrent-Dashboard" --tag "v$version" --output dist-windows
```

Replace the tag when `__version__` changes. For fork builds, replace the repository argument with the fork's `owner/repository` value.

The build creates:

```text
dist-windows/
├── TorrentDashboard-Windows-X.Y.Z-x64/
│   ├── Dashboard.exe
│   ├── Recovery.exe
│   ├── Updater.exe
│   ├── _internal/
│   ├── static/
│   ├── release_notes/
│   ├── README.md
│   ├── CHANGELOG.md
│   └── package-info.json
├── TorrentDashboard-Windows-X.Y.Z-x64.zip
└── TorrentDashboard-Windows-X.Y.Z-x64.release.json
```

The build script verifies that all three executables exist before producing the ZIP and provenance sidecar.

## Normal modification/recompile loop

1. Modify Python code under `src/torrent_dashboard/` and/or browser assets under `static/`.
2. Add or update tests for the changed behavior.
3. Run the source/UI validation commands above.
4. For an official increment, update `src/torrent_dashboard/__init__.py` and synchronized frontend/release metadata to the new version.
5. Re-run the Windows build command with the matching tag.
6. Smoke-test `Dashboard.exe`, `Recovery.exe`, and `Updater.exe` from the generated package folder.
7. For updater work, install the generated package into a disposable test installation and exercise update, recovery reinstall, failed-health-check rollback, and preservation of `config.json`/`data/`.

For a quick local compile/smoke test, the current version can be rebuilt without changing the version number as long as the `--tag` value still matches `__version__`. Do not publish two materially different updater-visible builds under the same version.

## GitHub automation

Pushes to `main` automatically run both release workflows:

- `.github/workflows/release.yml` validates and refreshes the source prerelease assets.
- `.github/workflows/windows-preview.yml` builds the Windows x64 package on a native Windows runner, smoke-tests all three executables, and publishes the Windows preview assets to the same prerelease.

This means normal development remains source-first: edit the application, validate it, merge it, and let the Windows workflow rebuild the executable package from the same canonical source tree.

## Packaging decisions that must remain true

- `src/torrent_dashboard/` is the canonical Python application source.
- `Dashboard.exe` remains onedir so its bundled runtime is replaceable as a managed unit.
- `Recovery.exe` and `Updater.exe` remain standalone onefile programs so recovery and executable replacement do not depend on Dashboard's `_internal` runtime.
- `static/`, `config.json`, and `data/` remain external to the executables.
- Compiled update/rollback logic must preserve `config.json` and `data/`.
- Code signing is intentionally deferred until repeated compiled-update and rollback testing is stable.
