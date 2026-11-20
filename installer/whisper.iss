; Inno Setup script for the Whisper Video Translator Windows installer.
; Build:  "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" installer\whisper.iss
; Packages the tested Python environment (.venv packages + the base Python 3.12), the app, FFmpeg and fonts.
; The AI models are NOT included: they live in the user's Hugging Face cache (accepted licences + login needed once).
#define AppName "Whisper Video Translator"
#define AppVer "1.0.0"
#define Root "C:\Wispher"
#define PyDir GetEnv("LOCALAPPDATA") + "\Programs\Python\Python312"
#define FfDir GetEnv("LOCALAPPDATA") + "\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0.2-full_build\bin"

[Setup]
AppId={{B7E2C0A4-6D1F-4C57-9B5A-3F0E8A1D2C41}
AppName={#AppName}
AppVersion={#AppVer}
AppPublisher=Abhishek Surya Doppalapudi
DefaultDirName={localappdata}\Programs\Whisper
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#Root}\installer_out
OutputBaseFilename=Whisper-Setup
SetupIconFile={#Root}\whisper.ico
UninstallDisplayIcon={app}\whisper.ico
Compression=lzma2/fast
SolidCompression=no
DiskSpanning=yes
DiskSliceSize=2000000000
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "Create a &Desktop icon"; GroupDescription: "Icons:"

[Files]
Source: "{#Root}\app.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Root}\access_log.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Root}\scripture.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Root}\fetch_bible.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Root}\whisper_app.pyw"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Root}\whisper.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Root}\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Root}\fonts\*"; DestDir: "{app}\fonts"; Flags: ignoreversion recursesubdirs
Source: "{#FfDir}\ffmpeg.exe"; DestDir: "{app}\bin"; Flags: ignoreversion
Source: "{#FfDir}\ffprobe.exe"; DestDir: "{app}\bin"; Flags: ignoreversion
; bundled Python 3.12 (its own empty site-packages is replaced by the tested packages below)
Source: "{#PyDir}\*"; DestDir: "{app}\runtime"; Excludes: "Lib\site-packages\*"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#Root}\.venv\Lib\site-packages\*"; DestDir: "{app}\runtime\Lib\site-packages"; Flags: ignoreversion recursesubdirs createallsubdirs


[Run]
Filename: "{app}\runtime\python.exe"; Parameters: "fetch_bible.py"; WorkingDir: "{app}"; Description: "Download the Telugu Bible texts for scripture matching (needs internet)"; Flags: postinstall unchecked skipifsilent
Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\whisper_app.pyw"""; WorkingDir: "{app}"; Description: "Start {#AppName}"; Flags: postinstall nowait skipifsilent

[UninstallDelete]
; files created after install (compiled caches, downloaded Bible texts); the "records" log is kept on purpose
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\__pycache__"
Type: filesandordirs; Name: "{app}\bible"

[Code]
var
  ShortcutsBlocked: Boolean;

// Security software (for example 360 Total Security) can refuse to let programs save .lnk files.
// Try, and carry on without failing the whole install if it is blocked.
procedure MakeShortcut(Path: String);
var
  Shell, Link: Variant;
begin
  try
    Shell := CreateOleObject('WScript.Shell');
    Link := Shell.CreateShortcut(Path);
    Link.TargetPath := ExpandConstant('{app}\runtime\pythonw.exe');
    Link.Arguments := '"' + ExpandConstant('{app}\whisper_app.pyw') + '"';
    Link.WorkingDirectory := ExpandConstant('{app}');
    Link.IconLocation := ExpandConstant('{app}\whisper.ico');
    Link.Description := '{#AppName}';
    Link.Save;
    if not FileExists(Path) then
      ShortcutsBlocked := True;
  except
    Log('Shortcut blocked: ' + Path + ' - ' + GetExceptionMessage);
    ShortcutsBlocked := True;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    MakeShortcut(ExpandConstant('{autoprograms}\{#AppName}.lnk'));
    if WizardIsTaskSelected('desktopicon') then
      MakeShortcut(ExpandConstant('{autodesktop}\{#AppName}.lnk'));
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if (CurPageID = wpFinished) and ShortcutsBlocked and not WizardSilent then
    MsgBox('The app is installed, but your security software blocked the shortcut.' + #13#10 + #13#10 +
           'To make the icon yourself: right-click the Desktop > New > Shortcut. Target:' + #13#10 +
           ExpandConstant('"{app}\runtime\pythonw.exe" "{app}\whisper_app.pyw"') + #13#10 +
           'Name it "{#AppName}", then Properties > Change Icon > ' + ExpandConstant('{app}\whisper.ico') + '.',
           mbInformation, MB_OK);
end;
