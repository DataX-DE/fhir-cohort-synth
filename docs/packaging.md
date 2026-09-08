# Windows and Linux offline packages

The build produces `fhir-cohort-synth-<version>-windows-x64.zip` and
`fhir-cohort-synth-<version>-linux-x64.tar.gz`, each with a SHA256 sidecar file.
Each archive contains the executable, `_internal/` runtime folder, quick-start
guide, invented FHIR example, build information and component notices.

The hospital runs the executable with `run --input ... --output ...`.
CLI options, transformations, progress and output layout are the same as the
source version.

## Build and checks

GitHub Actions runs `.github/workflows/package.yml` on Windows Server 2022 x64
and Ubuntu 22.04 x64. Changes on `main` or `packaging/**` trigger builds; the
workflow can also be started manually. It runs the full test suite, builds the
native package, checks the extracted executable and uploads the archive plus
checksum only after successful checks. Artifacts are retained for 30 days.
Download them from the successful Actions run and extract the outer Actions ZIP
to obtain the actual distribution archive and checksum. No GitHub release is
published automatically.

`tools/build_package.py` is the only build script. It uses PyInstaller from
`packaging/requirements-build.txt`; no build dependencies are added to the
application's runtime dependencies. It bundles the two JSON definition files
explicitly and stages only the runtime, guide, example, build metadata and notices.
Local exports, databases, keys and development files are not copied.

`tools/check_package.py` extracts and relocates the deliverable to a path with
spaces and non-ASCII characters. It runs help/version, the invented example,
gzip exports, re-ingestion, no-overwrite and failure checks. It also compares
gzip bytes with an API run reusing the packaged run's key. Subprocesses have no
Python on PATH and invalid external Python paths. Linux additionally runs in
a clean Ubuntu container with no Python installed and networking disabled.
Windows testing uses the native runner with Python removed from PATH; it does
not claim that the runner has no Python installation.

Verified on 8 September 2026 in
[build 34175669073](https://github.com/DataX-DE/fhir-cohort-synth/actions/runs/34175669073):
all 137 tests passed on Linux; Windows passed with the two existing POSIX-only
tests skipped. Both extracted-package checks passed. The clean offline Linux
run completed with 23 invented resource roots. The archives use Python 3.13.15
and PyInstaller 6.22.2 and record source commit `c6b8473` in `build-info.json`.

To build manually, use a fresh Python 3.13 virtual environment on the target OS:

```sh
python -m pip install -r packaging/requirements-build.txt
python -m unittest discover -s tests -v
python tools/build_package.py
python tools/check_package.py dist/<archive-name>
```

Use `--output <new-directory>` on the build script for another build. This is
a developer option; the hospital CLI still exposes only input/output paths.

## Targets

- Windows x64: Windows 10/11 and Windows Server 2022 or newer; tested on the
  Windows Server 2022 runner. No installer or administrator request is added.
- Linux x64: glibc 2.35 or newer; built and tested on Ubuntu 22.04, also tested
  in a clean Ubuntu 22.04 container. ARM, Alpine/musl and older glibc require
  separate builds. Linux uses tar to preserve runtime symlinks and permissions.

Native builds are required because PyInstaller is not a cross-compiler.
Linux builds depend on the build machine's glibc baseline.
[PyInstaller platform guidance](https://pyinstaller.org/en/stable/usage.html#making-gnu-linux-apps-forward-compatible)

The executable is currently unsigned. Signing can be added when a certificate
and the hospital's deployment requirements are available. Packaging does not
change the existing reproduction API.
