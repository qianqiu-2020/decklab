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
#ifdef LocalFast
Compression=zip/1
SolidCompression=no
#else
Compression=lzma2
SolidCompression=yes
#endif
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
#if FileExists(BundleDir + "\usb-driver\spacemit_usb.inf")
Name: "usbdriver"; Description: "Install USB flashing driver / 安装 USB 刷机驱动（可能需要管理员确认）"; Check: UsbDriverNeeded
#endif

[Files]
Source: "windows-upgrade-mode.ps1"; Flags: dontcopy
#if FileExists(BundleDir + "\usb-driver\spacemit_usb.inf")
Source: "{#BundleDir}\Install-seriald-USB-Driver.ps1"; Flags: dontcopy
#endif
Source: "{#BundleDir}\*"; DestDir: "{app}"; Excludes: "data\*"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
Type: files; Name: "{app}\lan-firewall.json"

[Dirs]
; Runtime data belongs to the user: upgrades and uninstall leave it intact.
Name: "{app}\data"; Flags: uninsneveruninstall

[Icons]
Name: "{group}\DeckLab - 本机访问"; Filename: "{app}\Start-seriald.cmd"; WorkingDir: "{app}"; IconFilename: "{app}\{#AppIcon}"
Name: "{group}\DeckLab - 局域网无密码"; Filename: "{app}\Start-seriald-LAN-Open.cmd"; WorkingDir: "{app}"; IconFilename: "{app}\{#AppIcon}"
Name: "{group}\DeckLab - 局域网有密码"; Filename: "{app}\Start-seriald-LAN.cmd"; WorkingDir: "{app}"; IconFilename: "{app}\{#AppIcon}"
#if FileExists(BundleDir + "\usb-driver\spacemit_usb.inf")
Name: "{group}\DeckLab - 安装 USB 刷机驱动"; Filename: "{app}\Install-seriald-USB-Driver.cmd"; WorkingDir: "{app}"; IconFilename: "{app}\{#AppIcon}"
#endif
Name: "{group}\卸载 DeckLab"; Filename: "{uninstallexe}"
Name: "{userdesktop}\DeckLab"; Filename: "{app}\Start-seriald-LAN.cmd"; WorkingDir: "{app}"; IconFilename: "{app}\{#AppIcon}"; Tasks: desktopicon

[CustomMessages]
english.LaunchProtectedLAN=Start DeckLab with password-protected LAN sharing
chinesesimplified.LaunchProtectedLAN=启动 DeckLab（局域网有密码）

[Run]
Filename: "{code:HostProgram}"; Parameters: "{code:HostArguments}"; WorkingDir: "{app}"; Check: ResumeHost; Flags: nowait runasoriginaluser
Filename: "{sysnative}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\Start-seriald-LAN.ps1"""; WorkingDir: "{app}"; Description: "{cm:LaunchProtectedLAN}"; Check: OfferDefaultStart; Flags: postinstall nowait skipifsilent runasoriginaluser

[Code]
var
  PreviousMode, PreviousPort: Integer;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  Result := '';
  if (PreviousMode <> 0) or not FileExists(ExpandConstant('{app}\seriald.exe')) then Exit;
  ExtractTemporaryFile('windows-upgrade-mode.ps1');
  if Exec(ExpandConstant('{sysnative}\WindowsPowerShell\v1.0\powershell.exe'),
    '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
    ExpandConstant('{tmp}\windows-upgrade-mode.ps1') + '" -InstallDir "' + ExpandConstant('{app}') + '"',
    ExpandConstant('{tmp}'), SW_HIDE, ewWaitUntilTerminated, Code) then
  begin
    if (Code div 65536 >= 1) and (Code div 65536 <= 3) and (Code mod 65536 > 0) then
    begin
      PreviousMode := Code div 65536;
      PreviousPort := Code mod 65536;
      Log('Will resume the installed DeckLab host on port ' + IntToStr(PreviousPort));
    end;
  end;
end;

function ResumeHost: Boolean;
begin
  Result := PreviousMode <> 0;
end;

function OfferDefaultStart: Boolean;
begin
  Result := not ResumeHost;
end;

function HostProgram(Param: String): String;
begin
  if PreviousMode > 1 then
    Result := ExpandConstant('{sysnative}\WindowsPowerShell\v1.0\powershell.exe')
  else Result := ExpandConstant('{app}\seriald.exe');
end;

function HostArguments(Param: String): String;
var
  Launcher: String;
begin
  if PreviousMode > 1 then
  begin
    if PreviousMode = 2 then Launcher := 'Start-seriald-LAN.ps1'
    else Launcher := 'Start-seriald-LAN-Open.ps1';
    Result := '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\') +
      Launcher + '" -Port ' + IntToStr(PreviousPort);
  end else Result := '--bind 127.0.0.1 --port ' + IntToStr(PreviousPort);
end;

#if FileExists(BundleDir + "\usb-driver\spacemit_usb.inf")
var
  DriverProbeDone, CompatibleDriverInstalled: Boolean;

function UsbDriverNeeded: Boolean;
var
  ProbeResult: Integer;
begin
  if not DriverProbeDone then
  begin
    ExtractTemporaryFile('Install-seriald-USB-Driver.ps1');
    CompatibleDriverInstalled :=
      Exec(ExpandConstant('{sysnative}\WindowsPowerShell\v1.0\powershell.exe'),
        '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
        ExpandConstant('{tmp}\Install-seriald-USB-Driver.ps1') + '" -DetectOnly',
        ExpandConstant('{tmp}'), SW_HIDE, ewWaitUntilTerminated, ProbeResult) and (ProbeResult = 0);
    DriverProbeDone := True;
    if CompatibleDriverInstalled then
      Log('Compatible USB flashing driver detected; skipping driver task.');
  end;
  Result := not CompatibleDriverInstalled;
end;
#endif

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
  DriverResult: Integer;
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
#if FileExists(BundleDir + "\usb-driver\spacemit_usb.inf")
    if UsbDriverNeeded and WizardIsTaskSelected('usbdriver') then
    begin
      DriverResult := -1;
      if not Exec(ExpandConstant('{sysnative}\WindowsPowerShell\v1.0\powershell.exe'),
        '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\Install-seriald-USB-Driver.ps1') + '"',
        ExpandConstant('{app}'), SW_SHOW, ewWaitUntilTerminated, DriverResult) or (DriverResult <> 0) then
      begin
        Log('USB driver installation did not complete: ' + IntToStr(DriverResult));
        if WizardSilent then RaiseException('USB driver installation did not complete. Run Install-seriald-USB-Driver.cmd.');
        MsgBox('USB driver installation did not complete. Run Install-seriald-USB-Driver.cmd from the Start menu before flashing.', mbError, MB_OK);
      end;
    end;
#endif
  end;
end;
