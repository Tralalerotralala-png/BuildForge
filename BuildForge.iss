; Optional Inno Setup 6 recipe. Build dist\BuildForge first. Not Windows-tested.
[Setup]
AppId=BuildForge.Local.Workbench.Beta
AppName=BuildForge
AppVersion=0.4-beta
DefaultDirName={localappdata}\Programs\BuildForge
DefaultGroupName=BuildForge
PrivilegesRequired=lowest
OutputDir=installer
OutputBaseFilename=BuildForge-0.4-beta-Setup
Compression=lzma2
SolidCompression=yes
UninstallDisplayIcon={app}\BuildForge.exe
[Files]
Source: "dist\BuildForge\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
Name: "{group}\BuildForge"; Filename: "{app}\BuildForge.exe"
[Run]
Filename: "{app}\BuildForge.exe"; Description: "Launch BuildForge"; Flags: nowait postinstall skipifsilent
