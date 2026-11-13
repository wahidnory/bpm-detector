<#
Builds BPM Overlay and its installer.

  .\build.ps1             build the app + installer\Output\BPM-Overlay-Setup-<version>.exe
  .\build.ps1 -SkipSetup  only build the app (into %TEMP%\bpmbuild\dist)

The version comes from APP_VERSION in bpm_overlay.py - change it there only.
Needs: Python 3.12 with `pip install -r requirements.txt -r requirements-build.txt`,
and Inno Setup 6 (winget install JRSoftware.InnoSetup).
#>
param([switch]$SkipSetup)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot

$m = Select-String -Path "$root\bpm_overlay.py" -Pattern '^APP_VERSION\s*=\s*"(\d+)\.(\d+)\.(\d+)"'
if (-not $m) { throw 'APP_VERSION not found in bpm_overlay.py' }
$g = $m.Matches[0].Groups
$version = "$($g[1].Value).$($g[2].Value).$($g[3].Value)"
$tuple = "$($g[1].Value), $($g[2].Value), $($g[3].Value), 0"
Write-Host "Building BPM Overlay $version"

# Short build path outside the repo: PyInstaller output can exceed Windows' path limit
$work = Join-Path $env:TEMP 'bpmbuild'
$dist = Join-Path $work 'dist'
if (Test-Path $dist) { Remove-Item -Recurse -Force $dist }
New-Item -ItemType Directory -Force $work | Out-Null

# Windows version resource for the exe
$versionFile = Join-Path $work 'version_info.txt'
@"
VSVersionInfo(
  ffi=FixedFileInfo(filevers=($tuple), prodvers=($tuple), mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('FileDescription', 'BPM Overlay'),
      StringStruct('FileVersion', '$version'),
      StringStruct('InternalName', 'BPM Overlay'),
      StringStruct('OriginalFilename', 'BPM Overlay.exe'),
      StringStruct('ProductName', 'BPM Overlay'),
      StringStruct('ProductVersion', '$version')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"@ | Set-Content -Path $versionFile -Encoding utf8

$ico = Join-Path $root 'bpm_overlay.ico'
python -m PyInstaller --noconfirm --clean --windowed --onedir --name 'BPM Overlay' `
    --icon $ico --add-data "$ico;." --version-file $versionFile `
    --distpath $dist --workpath (Join-Path $work 'work') --specpath $work `
    --log-level WARN (Join-Path $root 'bpm_overlay.py')
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed' }
$exe = Join-Path $dist 'BPM Overlay\BPM Overlay.exe'
Write-Host "App: $exe ($((Get-Item $exe).VersionInfo.FileVersion))"

if ($SkipSetup) { return }

$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
          "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
          "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw 'Inno Setup 6 not found (winget install JRSoftware.InnoSetup)' }
& $iscc /Q "/DAppVersion=$version" "/DBuildDir=$(Join-Path $dist 'BPM Overlay')" (Join-Path $root 'installer\BPM Overlay.iss')
if ($LASTEXITCODE -ne 0) { throw 'Inno Setup failed' }
Write-Host "Installer: $(Join-Path $root "installer\Output\BPM-Overlay-Setup-$version.exe")"
