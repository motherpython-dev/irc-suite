; Inno Setup — инсталлятор iRC
; Компиляция:  "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" build\installer.iss
; Требует предварительной сборки PyInstaller (dist\iRC\).

#define AppName        "iRC"
#define AppShortName   "iRC"
#define AppVersion     "2.14.3"
#define AppPublisher   "iRChamps"
#define AppExe         "iRC.exe"

[Setup]
AppId={{2B7E4A91-6C3D-4F52-9A18-7D5E0C4B3A26}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppShortName}
DefaultGroupName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
OutputDir=..\dist\installer
OutputBaseFilename={#AppShortName}-setup-{#AppVersion}
SetupIconFile=..\assets\icon.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; ставим для текущего пользователя — не требует прав администратора
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon";  Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "startupicon";  Description: "Запускать вместе с Windows"; GroupDescription: "Автозапуск:"; Flags: unchecked

[Files]
Source: "..\dist\iRC\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}";              Filename: "{app}\{#AppExe}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}";        Filename: "{app}\{#AppExe}"; Tasks: desktopicon
Name: "{userstartup}\{#AppName}";        Filename: "{app}\{#AppExe}"; Tasks: startupicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; версии бандлов и БД лежат в LOCALAPPDATA и НЕ удаляются автоматически:
; там история инцидентов. Чистка — вручную или через отдельный пункт ниже.
Type: filesandordirs; Name: "{app}"

[Code]
// Не даём ставить поверх запущенного приложения
function InitializeSetup(): Boolean;
var
  ResultCode: Integer;
begin
  Exec('taskkill.exe', '/F /IM {#AppExe}', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := True;
end;




