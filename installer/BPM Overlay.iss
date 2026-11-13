; Inno Setup script for BPM Overlay
; Don't compile this directly - run build.ps1 in the repo root. It builds the app
; with PyInstaller and passes the version (from APP_VERSION in bpm_overlay.py) and
; build folder in as /DAppVersion=... /DBuildDir=...

#define AppName "BPM Overlay"
#define AppExe "BPM Overlay.exe"
#ifndef AppVersion
  #error AppVersion not set - run build.ps1 instead of compiling this script directly
#endif
#ifndef BuildDir
  #define BuildDir GetEnv("TEMP") + "\bpmbuild\dist\BPM Overlay"
#endif

[Setup]
; Keep AppId the same for every future version so upgrades replace the old install
AppId={{583BCF13-2161-4BF2-BBE8-33494FD23F69}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
; Per-user install: no admin prompt. Users can still choose "all users" in the dialog.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=Output
OutputBaseFilename=BPM-Overlay-Setup-{#AppVersion}
SetupIconFile=..\bpm_overlay.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
; Matches the mutex the app creates, so setup/uninstall ask to close a running overlay
AppMutex=BPMOverlay_SingleInstance
CloseApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "dutch"; MessagesFile: "compiler:Languages\Dutch.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "startup"; Description: "Start BPM Overlay when Windows starts"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#BuildDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "THIRD_PARTY_NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "licenses\*"; DestDir: "{app}\licenses"; Flags: ignoreversion

[InstallDelete]
; Clear out old bundled libraries on upgrade so nothing stale is left behind
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: startup

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM ""{#AppExe}"""; Flags: runhidden; RunOnceId: "CloseOverlay"
