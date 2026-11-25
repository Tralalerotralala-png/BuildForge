; Inno Setup 6 recipe. Hosted CI smoke-tested; independent clean-machine validation remains required.
[Setup]
AppId=BuildForge.Local.Workbench.Beta
AppName=BuildForge
AppVersion=0.7.0-rc.1
DefaultDirName={localappdata}\Programs\BuildForge
DefaultGroupName=BuildForge
PrivilegesRequired=lowest
OutputDir=installer
OutputBaseFilename=BuildForge-0.7.0-rc.1-Setup
Compression=lzma2
SolidCompression=yes
UninstallDisplayIcon={app}\BuildForge.exe
[Files]
Source: "dist\BuildForge\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
Name: "{group}\BuildForge"; Filename: "{app}\BuildForge.exe"
[Run]
Filename: "{app}\BuildForge.exe"; Description: "Launch BuildForge"; Flags: nowait postinstall skipifsilent
