; OwlMyPower 1.0 - Legacy edition installer (Windows 7, 32-bit)
; NOTE: current Inno Setup installers require Windows 7+. The app itself still
; runs on Vista SP2 - Vista users should use the portable OwlMyPower_1.0_Legacy.exe.
; Build: ISCC.exe OwlMyPower-Legacy.iss

#define MyAppName "OwlMyPower (Legacy)"
#define MyAppVersion "1.0"
#define MyAppPublisher "RoundDarkMatter"
#define MyAppURL "https://github.com/RoundDarkMatter/OwlMyPower"
#define MyAppExeName "OwlMyPower.exe"

[Setup]
AppId={{6B2D9E04-51C7-4A83-B9F2-CE7A14D5083A}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\OwlMyPower
DefaultGroupName=OwlMyPower
DisableProgramGroupPage=yes
LicenseFile=..\..\OwlMyPower-repo\LICENSE
PrivilegesRequired=admin
OutputDir=output
OutputBaseFilename=OwlMyPower-1.0-Legacy-Setup
SetupIconFile=..\owl_logo.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
MinVersion=6.1
AppMutex=OwlMyPower_SingleInstance_A9F3E1
UninstallDisplayIcon={app}\{#MyAppExeName}
VersionInfoVersion=1.0.0.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "startup"; Description: "Start OwlMyPower with Windows (minimized to tray)"; GroupDescription: "Startup:"

[Files]
Source: "..\dist\OwlMyPower_1.0_Legacy.exe"; DestDir: "{app}"; DestName: "{#MyAppExeName}"; Flags: ignoreversion
Source: "..\..\OwlMyPower-repo\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\OwlMyPower-repo\README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\OwlMyPower"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\OwlMyPower"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "OwlMyPower"; ValueData: """{app}\{#MyAppExeName}"" --min"; Tasks: startup; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,OwlMyPower}"; Flags: nowait postinstall skipifsilent
