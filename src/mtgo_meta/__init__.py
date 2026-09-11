"""metahunter-app — local MTGO match analyser + meta uploader.

Single source of truth for the build version. The PyInstaller spec
file imports it for VS_VERSIONINFO metadata; the runtime updater
compares it against the latest GitHub release.

Bump per release:
  patch — bug fixes, no schema or wire-format change
  minor — features, additive wire-format changes
  major — breaking changes (DB schema migration, parser_version bump
          that requires a re-ingest, etc.)
"""

__version__ = "0.7.1"
