"""Build one native offline package; run this on Windows x64 or Linux x64.

Only the build environment needs PyInstaller. Stage an explicit set of files so
local datasets, output databases and keys can never be swept into the archive.
The hospital runs the same CLI, using the bundled Python runtime.
"""
import argparse
import hashlib
from importlib.metadata import distribution, version
import json
from pathlib import Path
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def native_target():
    if platform.machine().lower() not in {'amd64', 'x86_64'} or sys.maxsize <= 2**32:
        raise ValueError('Build with a 64-bit x86 Python on Windows or Linux.')
    if sys.platform == 'win32':
        return 'windows-x64'
    if sys.platform == 'linux':
        return 'linux-x64'
    raise ValueError('Build each package on its target OS: Windows or Linux.')


def checksum(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def archive_package(folder, destination, target):
    """Use tar on Linux to retain executable permissions and runtime symlinks."""
    kind = 'zip' if target == 'windows-x64' else 'gztar'
    archive = Path(shutil.make_archive(str(destination / folder.name), kind,
                                      root_dir=folder.parent, base_dir=folder.name))
    archive.with_name(archive.name + '.sha256').write_text(
        f'{checksum(archive)}  {archive.name}\n', encoding='ascii')
    return archive


def copy_licenses(folder):
    licenses = folder / 'licenses'
    shutil.copytree(ROOT / 'packaging/licenses', licenses)
    shutil.copy2(ROOT / 'packaging/THIRD-PARTY-NOTICES.txt', licenses)
    # Windows Python includes notices for its external libraries in LICENSE.txt.
    # On Linux also retain the distro's notices for the shared libraries we ship.
    python_license = Path(sys.base_prefix) / 'LICENSE.txt'
    if python_license.is_file():
        shutil.copy2(python_license, licenses / 'Python-runtime.txt')
    pyinstaller = distribution('pyinstaller')
    copying = next(path for path in pyinstaller.files if path.name == 'COPYING.txt')
    shutil.copy2(pyinstaller.locate_file(copying), licenses / 'PyInstaller.txt')
    if sys.platform == 'linux':
        for package in ('libbz2-1.0', 'liblzma5', 'zlib1g', 'libffi8', 'libssl3', 'libsqlite3-0'):
            notice = Path('/usr/share/doc') / package / 'copyright'
            if notice.is_file():
                shutil.copy2(notice, licenses / f'{package}.txt')
        for name in ('Apache-2.0', 'GPL-2', 'LGPL-2.1'):
            notice = Path('/usr/share/common-licenses') / name
            if notice.is_file():
                shutil.copy2(notice, licenses / f'{name}.txt')


def build(output):
    target = native_target()
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))['project']
    name = f"{project['name']}-{project['version']}-{target}"
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.glob(name + '*')):
        raise ValueError('Package output already exists; choose a fresh build destination.')
    work = ROOT / 'build'
    work.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='package-', dir=work) as temporary:
        staging = Path(temporary)
        # Explicit data inclusion makes importlib.resources work when frozen.
        # UTF-8 mode also makes the Windows runtime independent of locale settings.
        subprocess.run([
            sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
            '--onedir', '--console', '--noupx', '--name', 'fhir-cohort-synth',
            '--python-option', 'X utf8', '--collect-data', 'fhir_cohort_synth',
            '--distpath', str(staging / 'dist'), '--workpath', str(staging / 'work'),
            '--specpath', str(staging), str(ROOT / 'fhir_synth.py'),
        ], cwd=ROOT, check=True)
        folder = staging / name
        (staging / 'dist/fhir-cohort-synth').rename(folder)
        shutil.copy2(ROOT / 'packaging/QUICKSTART.txt', folder)
        shutil.copy2(ROOT / 'LICENSE', folder)
        (folder / 'examples').mkdir()
        shutil.copy2(ROOT / 'examples/mii-demo-bundle.json', folder / 'examples')
        copy_licenses(folder)
        metadata = {
            'application_version': project['version'], 'target': target,
            'application_license': project['license'],
            'python_version': platform.python_version(),
            'sqlite_version': sqlite3.sqlite_version, 'pyinstaller_version': version('pyinstaller'),
            'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
            'data_sha256': {path.name: checksum(path) for path in sorted((ROOT / 'fhir_cohort_synth/data').glob('*.json'))},
        }
        (folder / 'build-info.json').write_text(json.dumps(metadata, indent=2) + '\n', encoding='utf-8')
        archive = archive_package(folder, output, target)
    print(f'Built {archive.name} and {archive.name}.sha256')
    return archive


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    args = parser.parse_args()
    build(args.output)
