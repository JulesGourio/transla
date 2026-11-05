"""Translation engine — the document-translation pipeline modules owned by
the app (extract, audit, langdetect, glossary_io, fit_check, length_adapt,
rebuild, validate).

The command-line `main()` functions and `__main__` blocks of these modules
are inert here: their job_utils imports are lazy and the app never calls them.

server/services/processors/translation.py is the in-memory adapter the
translate router calls; it composes these modules against uploaded bytes
instead of CLI file paths.
"""
