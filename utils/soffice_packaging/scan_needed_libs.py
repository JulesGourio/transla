"""List shared-library sonames a portable LibreOffice tree needs but doesn't ship.

Pure-Python ELF DT_NEEDED reader (no ldd — runs on Windows against Linux
binaries): scans every ELF file in the tree, unions their NEEDED entries,
subtracts what the tree itself provides and the glibc-family libs any Linux
container has. What remains must be bundled (see _SYSLIB_PACKAGES in
package_libreoffice.py) or present in the target container.

Usage: python scan_needed_libs.py <extracted_tree_dir>
"""

import logging
import struct
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Provided by any glibc-based image — never bundle these.
_BASELINE = {
    'libc.so.6', 'libm.so.6', 'libdl.so.2', 'libpthread.so.0', 'librt.so.1',
    'ld-linux-x86-64.so.2', 'libresolv.so.2', 'libutil.so.1', 'libnsl.so.1',
    'libgcc_s.so.1', 'libstdc++.so.6', 'libz.so.1',
}


def elf_needed(path: Path) -> "list[str] | None":
    """Read the DT_NEEDED sonames of an ELF file.

    :param path: File to inspect.
    :return: Sonames in dynamic-section order (empty without a dynamic section), or None if the file is not ELF.
    """
    with open(path, 'rb') as f:
        ident = f.read(16)
        if len(ident) < 16 or ident[:4] != b'\x7fELF':
            return None
        is64 = ident[4] == 2
        endian = '<' if ident[5] == 1 else '>'
        f.seek(0)
        if is64:
            hdr = f.read(64)
            e_shoff, = struct.unpack_from(endian + 'Q', hdr, 0x28)
            e_shentsize, e_shnum = struct.unpack_from(endian + 'HH', hdr, 0x3A)
        else:
            hdr = f.read(52)
            e_shoff, = struct.unpack_from(endian + 'I', hdr, 0x20)
            e_shentsize, e_shnum = struct.unpack_from(endian + 'HH', hdr, 0x2E)

        f.seek(e_shoff)
        sections = [f.read(e_shentsize) for _ in range(e_shnum)]
        dynamic = dynstr = None
        for s in sections:
            if is64:
                sh_type, = struct.unpack_from(endian + 'I', s, 4)
                sh_offset, sh_size = struct.unpack_from(endian + 'QQ', s, 0x18)
                sh_link, = struct.unpack_from(endian + 'I', s, 0x28)
            else:
                sh_type, = struct.unpack_from(endian + 'I', s, 4)
                sh_offset, sh_size = struct.unpack_from(endian + 'II', s, 0x10)
                sh_link, = struct.unpack_from(endian + 'I', s, 0x18)
            if sh_type == 6:  # SHT_DYNAMIC
                dynamic = (sh_offset, sh_size, sh_link)
        if not dynamic:
            return []
        sh_offset, sh_size, sh_link = dynamic
        # The dynamic section links to its string table section.
        s = sections[sh_link]
        if is64:
            str_off, str_size = struct.unpack_from(endian + 'QQ', s, 0x18)
        else:
            str_off, str_size = struct.unpack_from(endian + 'II', s, 0x10)
        f.seek(str_off)
        strtab = f.read(str_size)
        f.seek(sh_offset)
        dyn = f.read(sh_size)
        needed = []
        step = 16 if is64 else 8
        fmt = endian + ('qQ' if is64 else 'iI')
        for off in range(0, len(dyn) - step + 1, step):
            d_tag, d_val = struct.unpack_from(fmt, dyn, off)
            if d_tag == 0:
                break
            if d_tag == 1:  # DT_NEEDED
                end = strtab.find(b'\0', d_val)
                needed.append(strtab[d_val:end].decode('ascii', errors='replace'))
        return needed


def main(tree: str) -> None:
    """Log the sonames the tree needs but neither provides nor inherits from glibc.

    :param tree: Root of the extracted LibreOffice tree.
    """
    root = Path(tree)
    provided, needed = set(), set()
    n_elf = 0
    for p in root.glob('**/*'):
        if not p.is_file():
            continue
        try:
            deps = elf_needed(p)
        except Exception:
            continue
        if deps is None:
            continue
        n_elf += 1
        provided.add(p.name)
        needed.update(deps)
    missing = sorted(needed - provided - _BASELINE)
    logger.info('%d ELF files, %d distinct NEEDED sonames', n_elf, len(needed))
    logger.info('missing (must exist in container or be bundled):')
    for so in missing:
        logger.info('  %s', so)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])
