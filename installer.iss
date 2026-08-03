; Inno Setup script for Metahunter.
;
; Produces a per-user Windows installer at:
;     dist\Metahunter-Setup-vX.Y.Z.exe
; plus a stable-named copy used by the in-app updater:
;     dist\Metahunter-Setup.exe       (renamed in build.ps1)
;
; Install layout:
;     %LOCALAPPDATA%\Programs\Metahunter\           (binaries)
;     %LOCALAPPDATA%\Metahunter\                    (data -- created
;                                                    on first run by
;                                                    paths.py)
;
; No admin (UAC) prompt. The downside: every Windows user on the same
; machine has to install for themselves. For a hobby-scale app shipped
; to friends that's the right tradeoff.
;
; Compile manually with the Inno Setup IDE, or via CLI:
;     iscc installer.iss
;
; Both produce dist\Metahunter-Setup-<version>.exe (see OutputBaseFilename).

#define AppName        "Metahunter"
#define AppVersion     "0.1.0"
#define AppPublisher   "AFKatta"
#define AppURL         "https://metahunter-web.vercel.app"
#define AppExe         "Metahunter.exe"
; Stable AppId so subsequent installers UPGRADE in place rather than
; installing a parallel copy. NEVER change this between versions --
; it's the join key Windows uses for "already installed?" checks.
#define AppId          "{{8B5C4E2A-3F1D-4A6E-9E2A-2E9D7C3F5A41}}"

[Setup]
AppId={#AppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}
VersionInfoVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=yes
UninstallDisplayIcon={app}\{#AppExe}
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
Compression=lzma2/ultra64
SolidCompression=yes
OutputDir=dist
OutputBaseFilename=Metahunter-Setup-v{#AppVersion}
WizardStyle=modern
; Signing is wired up by build.ps1's -Sign flag, which prepends an
; iscc /S"metahunter=<cmd>" registration AND sets SignTool=metahunter
; below in a preprocessor block. Default (unsigned) builds skip both.
#ifdef SIGN
SignTool=metahunter
SignedUninstaller=yes
#endif

; CloseApplications + RestartApplications: if the user has Metahunter
; running, Inno Setup closes it cleanly before replacing files. The
; in-app updater triggers /SILENT installs, so the user never sees
; the close dialog.
CloseApplications=force
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; \
    GroupDescription: "Additional shortcuts:"; Flags: unchecked
Name: "launchAtStart"; Description: "Launch Metahunter when Windows starts"; \
    GroupDescription: "Startup:"; Flags: unchecked

[Files]
; Every file PyInstaller produced under dist\Metahunter\. recursesubdirs
; pulls in _internal/* automatically.
Source: "dist\Metahunter\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: launchAtStart

[Run]
Filename: "{app}\{#AppExe}"; \
    Description: "Launch {#AppName} now"; \
    Flags: nowait postinstall skipifsilent
