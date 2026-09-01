; Inno Setup вЂ” РёРЅСЃС‚Р°Р»Р»СЏС‚РѕСЂ ichamps Race Control
; РљРѕРјРїРёР»СЏС†РёСЏ:  "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" build\installer.iss
; РўСЂРµР±СѓРµС‚ РїСЂРµРґРІР°СЂРёС‚РµР»СЊРЅРѕР№ СЃР±РѕСЂРєРё PyInstaller (dist\iRC\).

#define AppName        "iRC"
#define AppShortName   "iRC"
#define AppVersion     "2.4.1"
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
; СЃС‚Р°РІРёРј РґР»СЏ С‚РµРєСѓС‰РµРіРѕ РїРѕР»СЊР·РѕРІР°С‚РµР»СЏ вЂ” РЅРµ С‚СЂРµР±СѓРµС‚ РїСЂР°РІ Р°РґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂР°
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon";  Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "startupicon";  Description: "Р—Р°РїСѓСЃРєР°С‚СЊ РІРјРµСЃС‚Рµ СЃ Windows"; GroupDescription: "РђРІС‚РѕР·Р°РїСѓСЃРє:"; Flags: unchecked

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
; РІРµСЂСЃРёРё Р±Р°РЅРґР»РѕРІ Рё Р‘Р” Р»РµР¶Р°С‚ РІ LOCALAPPDATA Рё РќР• СѓРґР°Р»СЏСЋС‚СЃСЏ Р°РІС‚РѕРјР°С‚РёС‡РµСЃРєРё:
; С‚Р°Рј РёСЃС‚РѕСЂРёСЏ РёРЅС†РёРґРµРЅС‚РѕРІ. Р§РёСЃС‚РєР° вЂ” РІСЂСѓС‡РЅСѓСЋ РёР»Рё С‡РµСЂРµР· РѕС‚РґРµР»СЊРЅС‹Р№ РїСѓРЅРєС‚ РЅРёР¶Рµ.
Type: filesandordirs; Name: "{app}"

[Code]
// РќРµ РґР°С‘Рј СЃС‚Р°РІРёС‚СЊ РїРѕРІРµСЂС… Р·Р°РїСѓС‰РµРЅРЅРѕРіРѕ РїСЂРёР»РѕР¶РµРЅРёСЏ
function InitializeSetup(): Boolean;
var
  ResultCode: Integer;
begin
  Exec('taskkill.exe', '/F /IM {#AppExe}', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := True;
end;




