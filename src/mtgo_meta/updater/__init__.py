"""In-app auto-updater.

Periodically checks GitHub Releases for a newer Metahunter, downloads
the installer in the background, and on user confirmation runs it
with /SILENT so the new version replaces the current one with one
click. Compatible with the Inno Setup installer (installer.iss) —
plain zip-distribution users have to download the installer once,
then auto-updates work going forward.

Architecture:

    client.py   talks to the GitHub Releases API
    download.py streams the installer asset to a temp file
    worker.py   the background thread + the install-trigger
"""
