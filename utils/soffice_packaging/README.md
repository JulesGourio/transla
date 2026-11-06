# soffice_packaging

Builds the portable LibreOffice archive that the application extracts and runs to convert documents to PDF.

Parent: [utils](../README.md). The consumer is [services/soffice.py](../../server/services/README.md#sofficepy).

## Why an archive

Databricks Apps cannot install system packages. The application instead downloads a plain `tar.gz` from a Unity Catalog
volume, extracts it into its temporary disk once per process, and runs `program/soffice` headless as a subprocess.

## Files

| File | Content |
|---|---|
| `package_libreoffice.py` | Builds the archive from the official LibreOffice "Linux x86-64 (deb)" bundle |
| `scan_needed_libs.py` | Lists the shared libraries a LibreOffice tree needs but does not ship |

## package_libreoffice.py

Pure Python, runs on Windows. It does not need `dpkg` or `ar`.

1. Extract the bundle and read each `.deb` (an `ar` archive of fixed-header members wrapping a `data.tar.xz`, `.zst` or
   `.gz`).
2. Merge their `opt/libreoffice<version>` trees into one.
3. Download the Ubuntu 22.04 (jammy and jammy-updates) packages named in `_SYSLIB_PACKAGES` (X11, NSS, glib, cups, ...)
   and add their shared objects to `program/`, because the container image does not have them.
4. Write a `tar.gz` with execution permissions set (Windows drops them).

```powershell
uv run --no-project --with zstandard utils/soffice_packaging/package_libreoffice.py <LibreOffice_x.y.z_Linux_x86-64_deb.tar.gz> <output.tar.gz>
```

The result is about 317 MB, so it is not committed. Download the bundle from the Document Foundation, check its published
SHA-256, build, and upload the archive to the `binaries` volume. The application finds it through
`SOFFICE_ARCHIVE_VOLUME_PATH`. The extraction folder is named after the archive file, so publishing a fix means uploading
under a new file name and updating that path in `utils/deploy/target_env.json`.

## scan_needed_libs.py

```bash
python utils/soffice_packaging/scan_needed_libs.py <extracted_tree_dir>
```

Reads the `DT_NEEDED` entries of every ELF file in the tree without `ldd` (so it runs on Windows), removes what the tree
provides and the libraries every glibc container has, and logs the rest. Whatever it prints must be in
`_SYSLIB_PACKAGES` or in the container.

The archive of the application carries Liberation, Carlito, Caladea, DejaVu and Noto fonts, Amiri and Noto Arabic
included. The Qt, GTK and Java plugins are absent because headless mode never loads them.

## See also

- [services](../../server/services/README.md): how the archive is found and used.
- [deploy](../deploy/README.md): where the archive path is configured.
