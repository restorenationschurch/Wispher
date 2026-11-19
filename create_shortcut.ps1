# Creates a "Whisper Video Translator" icon on the Desktop and in the Start menu.
# Run once:  powershell -ExecutionPolicy Bypass -File create_shortcut.ps1
# The shortcut starts whisper_app.pyw with the project's own pythonw (no console window).
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$pyw = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pyw)) { Write-Host "Create the .venv first (see SETUP_GPU_PC.md)."; exit 1 }
$ws = New-Object -ComObject WScript.Shell
foreach ($dir in [Environment]::GetFolderPath("Desktop"), (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs")) {
    $lnk = $ws.CreateShortcut((Join-Path $dir "Whisper Video Translator.lnk"))
    $lnk.TargetPath = $pyw
    $lnk.Arguments = "`"$root\whisper_app.pyw`""
    $lnk.WorkingDirectory = $root
    $lnk.IconLocation = "$root\whisper.ico"
    $lnk.Description = "Whisper Video Translator"
    $lnk.Save()
    "Created: " + $lnk.FullName
}
