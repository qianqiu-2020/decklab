; Compile with tools/build_windows_installer.ps1 after PyInstaller packaging.
#ifndef AppVersion
  #error "AppVersion must be supplied by the builder"
#endif
#ifndef BundleDir
  #error "BundleDir must be supplied by the builder"
#endif
#ifndef OutputDir
  #error "OutputDir must be supplied by the builder"
#endif

#ifndef AppIcon
  #error "AppIcon must be supplied by the builder"
#endif

[Setup]
AppId={{51AB737C-BCEF-4C09-98EE-47ACD6A2CBBA}
AppName=DeckLab
AppVersion={#AppVersion}
AppPublisher=seriald contributors
DefaultDirName={code:DefaultInstallDir}
DefaultGroupName=DeckLab
UsePreviousAppDir=yes
DisableDirPage=no
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=seriald-{#AppVersion}-windows-x64-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
SetupIconFile=decklab.ico
UninstallDisplayIcon={app}\{#AppIcon}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "chinesesimplified"; MessagesFile: "inno-languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut / 创建桌面快捷方式"; Flags: unchecked

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}"; Excludes: "data\*"; Flags: ignoreversion recursesubdirs createallsubdirs

[Dirs]
; Runtime data belongs to the user: upgrades and uninstall leave it intact.
Name: "{app}\data"; Flags: uninsneveruninstall

[Icons]
Name: "{group}\DeckLab - 本机访问"; Filename: "{app}\Start-seriald.cmd"; WorkingDir: "{app}"; IconFilename: "{app}\{#AppIcon}"
Name: "{group}\DeckLab - 局域网无密码"; Filename: "{app}\Start-seriald-LAN-Open.cmd"; WorkingDir: "{app}"; IconFilename: "{app}\{#AppIcon}"
Name: "{group}\DeckLab - 局域网有密码"; Filename: "{app}\Start-seriald-LAN.cmd"; WorkingDir: "{app}"; IconFilename: "{app}\{#AppIcon}"
Name: "{group}\卸载 DeckLab"; Filename: "{uninstallexe}"
Name: "{userdesktop}\DeckLab"; Filename: "{app}\Start-seriald.cmd"; WorkingDir: "{app}"; IconFilename: "{app}\{#AppIcon}"; Tasks: desktopicon

[Code]
function DefaultInstallDir(Param: String): String;
var
  SourceDir, ParentDir: String;
begin
  SourceDir := ExpandConstant('{src}');
  ParentDir := ExtractFileDir(SourceDir);
  { Keep a source checkout's runtime installation outside its build directory. }
  if (CompareText(ExtractFileName(SourceDir), 'dist') = 0) and
     FileExists(AddBackslash(ParentDir) + 'seriald_windows.py') then
    SourceDir := ParentDir;
  Result := AddBackslash(SourceDir) + 'seriald-installed';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  LanguageFile, LanguageCode: String;
begin
  if CurStep = ssPostInstall then
  begin
    LanguageFile := ExpandConstant('{app}\data\ui-language.txt');
    { Preserve existing defaults when upgrading or adopting a portable install. }
    if not FileExists(LanguageFile) then
    begin
      if ActiveLanguage = 'chinesesimplified' then
        LanguageCode := 'zh-CN'
      else
        LanguageCode := 'en';
      if not SaveStringToFile(LanguageFile, LanguageCode + #13#10, False) then
        RaiseException('Cannot save the application default language.');
    end;
  end;
end;
