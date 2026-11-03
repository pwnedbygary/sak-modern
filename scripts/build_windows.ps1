$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
py -3.12 -m pip install --upgrade pip pyinstaller cryptography
py -3.12 -m PyInstaller --noconfirm --clean --onefile --windowed `
  --name SAK-Modern `
  --icon "$Root/resources/sak-modern.ico" `
  --paths "$Root/src" `
  --add-data "$Root/src/sak_modern/resources;sak_modern/resources" `
  --collect-all cryptography `
  "$Root/windows_launcher.py"
New-Item -ItemType Directory -Force -Path "$Root/dist" | Out-Null
Copy-Item "$Root/dist/SAK-Modern.exe" "$Root/dist/SAK-Modern.exe" -Force
Write-Host "Built $Root/dist/SAK-Modern.exe"
