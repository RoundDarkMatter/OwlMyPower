; OwlMyPower 1.0 - Modern edition installer (Windows 8.1 / 10 / 11, 64-bit)
; Build: ISCC.exe OwlMyPower-Modern.iss

#define MyAppName "OwlMyPower"
#define MyAppVersion "1.0"
#define MyAppPublisher "RoundDarkMatter"
#define MyAppURL "https://github.com/RoundDarkMatter/OwlMyPower"
#define MyAppExeName "OwlMyPower.exe"

[Setup]
AppId={{3F8A2C61-7D94-4B0E-A1C5-92E48B6D0F27}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
LicenseFile=..\..\OwlMyPower-repo\LICENSE
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=output
OutputBaseFilename=OwlMyPower-1.0-Setup
SetupIconFile=..\owl_logo.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
MinVersion=6.3
ArchitecturesInstallIn64BitMode=x64
AppMutex=OwlMyPower_SingleInstance_A9F3E1
UninstallDisplayIcon={app}\{#MyAppExeName}
VersionInfoVersion=1.0.0.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "startup"; Description: "Start {#MyAppName} with Windows (minimized to tray)"; GroupDescription: "Startup:"

[Files]
Source: "..\dist\OwlMyPower_1.0.exe"; DestDir: "{app}"; DestName: "{#MyAppExeName}"; Flags: ignoreversion
Source: "..\..\OwlMyPower-repo\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\OwlMyPower-repo\README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "OwlMyPower"; ValueData: """{app}\{#MyAppExeName}"" --min"; Tasks: startup; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent
