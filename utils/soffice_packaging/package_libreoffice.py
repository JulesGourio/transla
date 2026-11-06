"""Build a portable LibreOffice Linux x86_64 tarball for Databricks Apps.

Databricks Apps has no system package manager, so the app provisions
LibreOffice at runtime from a plain tar.gz stored in a UC Volume (see
server/services/soffice.py). This script produces that tar.gz from the
official TDF "Linux x86-64 (deb)" bundle, without needing dpkg/ar — .deb
files are `ar` archives (a trivial fixed-header format parsed here by hand)
wrapping a data.tar.{xz,zst,gz} whose contents Python's tarfile can read.

Steps (all pure Python, runs fine on Windows):
  1. (caller) download LibreOffice_<ver>_Linux_x86-64_deb.tar.gz
  2. extract every bundled .deb's data tarball
  3. merge their ./opt/libreoffice<ver>/ trees into one directory
  4. re-tar with normalized exec permissions (Windows drops them otherwise)

Usage:
  uv run --no-project --with zstandard utils/soffice_packaging/package_libreoffice.py <downloaded.tar.gz> <output.tar.gz>

The archive goes to the `binaries` volume; its path is set in
utils/deploy/target_env.json.
"""

import gzip
import io
import logging
import lzma
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

# System libraries the TDF LibreOffice build links against that slim container images do not ship. Their .so files
# are merged into program/ (covered by the executables' $ORIGIN rpath and LD_LIBRARY_PATH). They come from the
# Ubuntu jammy (22.04) archive, the Databricks Apps base image generation; their glibc requirements are old enough
# for a slightly different container release.
_SYSLIB_PACKAGES = [
    'libx11-6', 'libxcb1', 'libxau6', 'libxdmcp6', 'libxext6', 'libxinerama1',
    'libxrender1', 'libxrandr2', 'libxcursor1', 'libxfixes3', 'libxi6',
    'libxcomposite1', 'libxdamage1', 'libxtst6', 'libsm6', 'libice6',
    'libfontconfig1', 'libfreetype6', 'libpng16-16', 'libexpat1',
    'libcups2', 'libdbus-1-3', 'libglib2.0-0', 'libcairo2', 'libpixman-1-0',
    'libxcb-shm0', 'libxcb-render0', 'libavahi-client3', 'libavahi-common3',
    'libgssapi-krb5-2', 'libkrb5-3', 'libk5crypto3', 'libkrb5support0',
    'libcom-err2', 'libkeyutils1', 'libbrotli1',
    # NSS crypto stack: soffice.bin links libnss3 directly and TDF does not bundle it.
    'libnss3', 'libnspr4', 'libsqlite3-0',
    # Transitive closure of the scan_needed_libs.py output, minus GUI-plugin-only deps
    # (Qt5/KF5/GTK3/GStreamer are never loaded with SAL_USE_VCLPLUGIN=svp):
    'libssl3', 'libcrypt1', 'libbsd0', 'libmd0', 'libx11-xcb1', 'libffi8',
    'libmount1', 'libblkid1', 'libselinux1', 'libpcre3', 'libpcre2-8-0', 'libuuid1',
    'libgnutls30', 'libidn2-0', 'libunistring2', 'libtasn1-6', 'libnettle8',
    'libhogweed6', 'libp11-kit0', 'libgmp10',
    'libsystemd0', 'liblzma5', 'libzstd1', 'liblz4-1', 'libcap2',
    'libgcrypt20', 'libgpg-error0',
]
_UBUNTU_MIRROR = 'http://archive.ubuntu.com/ubuntu'


def iter_ar_members(data: bytes) -> "Iterator[tuple[str, bytes]]":
    """Read the members of an ar(1) archive.

    :param data: Whole archive content, starting with the ``!<arch>`` magic.
    :return: Iterator of ``(member name without trailing slash, member payload)``.
    """
    assert data[:8] == b'!<arch>\n', 'not an ar archive'
    off = 8
    while off < len(data):
        header = data[off:off + 60]
        if len(header) < 60:
            break
        name = header[0:16].decode('ascii').strip().rstrip('/')
        size = int(header[48:58].decode('ascii').strip())
        payload = data[off + 60:off + 60 + size]
        yield name, payload
        off += 60 + size + (size % 2)  # members are 2-byte aligned


def extract_deb(deb_bytes: bytes, dest: Path) -> None:
    """Extract the data tarball of a .deb into a directory.

    :param deb_bytes: Content of the .deb file.
    :param dest: Directory receiving the package files.
    :raises RuntimeError: if the data member uses an unsupported compression or is missing.
    """
    for name, payload in iter_ar_members(deb_bytes):
        if not name.startswith('data.tar'):
            continue
        if name.endswith('.xz'):
            stream = io.BytesIO(lzma.decompress(payload))
        elif name.endswith('.gz') or name.endswith('.tar'):
            stream = io.BytesIO(payload)
        elif name.endswith('.zst'):
            import zstandard  # jammy-updates debs are zstd-compressed
            stream = io.BytesIO(zstandard.ZstdDecompressor().decompress(payload, max_output_size=2**33))
        else:
            raise RuntimeError(f'unsupported data member {name}')
        with tarfile.open(fileobj=stream) as tar:
            tar.extractall(dest)
        return
    raise RuntimeError('no data.tar member found in .deb')


def _load_ubuntu_index() -> dict:
    """Read the Ubuntu package indexes.

    :return: Mapping package name -> pool file name; jammy-updates is read last so patched versions win.
    """
    filenames: dict = {}
    for dist in ('jammy', 'jammy-updates'):
        url = f'{_UBUNTU_MIRROR}/dists/{dist}/main/binary-amd64/Packages.gz'
        logger.info('fetching package index %s …', url)
        with urllib.request.urlopen(url, timeout=120) as resp:
            data = gzip.decompress(resp.read()).decode('utf-8', errors='replace')
        name = None
        for line in data.splitlines():
            if line.startswith('Package: '):
                name = line[9:].strip()
            elif line.startswith('Filename: ') and name:
                filenames[name] = line[10:].strip()
    return filenames


def add_system_libs(rootfs_program_dir: Path) -> None:
    """Download the ``_SYSLIB_PACKAGES`` debs and merge their shared objects into the LibreOffice program/ directory.

    :param rootfs_program_dir: The ``program/`` directory of the LibreOffice tree; existing files are kept.
    :raises SystemExit: if a package is missing from the Ubuntu index.
    """
    index = _load_ubuntu_index()
    missing = [p for p in _SYSLIB_PACKAGES if p not in index]
    if missing:
        raise SystemExit(f'packages not found in Ubuntu index: {missing}')

    with tempfile.TemporaryDirectory(prefix='syslibs_') as tmp:
        tmp_path = Path(tmp)
        for pkg in _SYSLIB_PACKAGES:
            url = f'{_UBUNTU_MIRROR}/{index[pkg]}'
            logger.info('  syslib %s: %s', pkg, url.rsplit('/', 1)[-1])
            with urllib.request.urlopen(url, timeout=120) as resp:
                deb_bytes = resp.read()
            extract_deb(deb_bytes, tmp_path)

        count = 0
        for so in tmp_path.glob('**/*.so*'):
            dest = rootfs_program_dir / so.name
            if dest.exists():
                continue
            if so.is_symlink():
                target = so.resolve()
                if target.is_file():
                    shutil.copy2(target, dest)
                    count += 1
            else:
                shutil.copy2(so, dest)
                count += 1
        logger.info('merged %d system libraries into program/', count)


def main(bundle_path: str, output_path: str) -> None:
    """Build the portable LibreOffice archive.

    :param bundle_path: Downloaded TDF ``LibreOffice_<ver>_Linux_x86-64_deb.tar.gz``.
    :param output_path: Path of the ``.tar.gz`` to write.
    :raises SystemExit: if the bundle has no .deb, not exactly one ``opt/libreoffice*`` tree, or no soffice binary.
    """
    bundle = Path(bundle_path)
    output = Path(output_path)
    with tempfile.TemporaryDirectory(prefix='lo_pkg_') as tmp:
        tmp_path = Path(tmp)
        debs_dir = tmp_path / 'debs'
        rootfs = tmp_path / 'rootfs'
        debs_dir.mkdir()
        rootfs.mkdir()

        logger.info('extracting bundle %s …', bundle)
        with tarfile.open(bundle) as tar:
            tar.extractall(debs_dir)

        debs = sorted(debs_dir.glob('**/*.deb'))
        if not debs:
            raise SystemExit('no .deb files found in bundle')
        logger.info('unpacking %d debs …', len(debs))
        for deb in debs:
            extract_deb(deb.read_bytes(), rootfs)

        # The debs install into ./opt/libreofficeXX.Y — keep only that tree.
        opt_trees = list((rootfs / 'opt').glob('libreoffice*'))
        if len(opt_trees) != 1:
            raise SystemExit(f'expected exactly one opt/libreoffice* tree, got {opt_trees}')
        lo_root = opt_trees[0]
        soffice = lo_root / 'program' / 'soffice'
        if not soffice.is_file():
            raise SystemExit('program/soffice missing from merged tree')

        add_system_libs(lo_root / 'program')

        logger.info('building %s …', output)
        output.parent.mkdir(parents=True, exist_ok=True)

        def normalize(info: tarfile.TarInfo) -> tarfile.TarInfo:
            """Normalise the permissions and ownership of an archive member.

            :param info: Member being added.
            :return: The same member with mode 755 for files and directories and root ownership.
            """
            # Windows loses POSIX modes; exec is granted broadly on this conversion-only tree.
            info.mode = 0o755 if info.isdir() or info.isfile() else info.mode
            info.uname = info.gname = ''
            info.uid = info.gid = 0
            return info

        with tarfile.open(output, 'w:gz', compresslevel=6) as tar:
            tar.add(lo_root, arcname=lo_root.name, filter=normalize)

        logger.info('done: %s (%.0f MB)', output, output.stat().st_size / 1e6)
        logger.info('upload the archive to the binaries volume')


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    main(sys.argv[1], sys.argv[2])
