#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

#define AppName "SRV Monitoring Agent"
#define AppExe "SRVMonitoringAgent.exe"

[Setup]
AppId={{236A5D6F-A3B2-43B3-935F-75F20E0BB89C}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=SRV Monitoring
DefaultDirName={autopf}\SRV Monitoring Agent
DefaultGroupName=SRV Monitoring Agent
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
OutputDir=..\build\installers
OutputBaseFilename=SRV-Monitoring-Agent-Setup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
SetupLogging=yes
UninstallDisplayName=SRV Monitoring Agent
UninstallDisplayIcon={app}\{#AppExe}
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Dirs]
Name: "{commonappdata}\SRV Monitoring Agent"; Permissions: admins-full system-full

[Files]
Source: "..\build\agent\SRVMonitoringAgent\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Конфигурация SRV Monitoring Agent"; Filename: "{sys}\notepad.exe"; Parameters: """{commonappdata}\SRV Monitoring Agent\agent.json"""
Name: "{group}\Удалить SRV Monitoring Agent"; Filename: "{uninstallexe}"

[Code]
var
  AgentPage: TInputQueryWizardPage;
  ExistingAgentConfig: Boolean;

function JsonEscape(Value: String): String;
begin
  StringChangeEx(Value, '\', '\\', True);
  StringChangeEx(Value, '"', '\"', True);
  StringChangeEx(Value, #13, '\r', True);
  StringChangeEx(Value, #10, '\n', True);
  Result := Value;
end;

function ContainsUnsafeLineBreak(Value: String): Boolean;
begin
  Result := (Pos(#13, Value) > 0) or (Pos(#10, Value) > 0);
end;

function InitializeUninstall(): Boolean;
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\schtasks.exe'), '/End /TN "SRV Monitoring Remote Agent"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(ExpandConstant('{sys}\schtasks.exe'), '/Delete /TN "SRV Monitoring Remote Agent" /F', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := True;
end;

procedure InitializeWizard;
begin
  ExistingAgentConfig := FileExists(ExpandConstant('{commonappdata}\SRV Monitoring Agent\agent.json'));
  AgentPage := CreateInputQueryPage(wpSelectDir,
    'Регистрация удалённого агента',
    'Укажите URL сервера и одноразовый токен',
    'Сначала создайте агента в веб-панели. Токен будет сохранён в ProgramData и доступен только администраторам и SYSTEM.');
  AgentPage.Add('Публичный HTTPS URL сервера (например, https://monitor.example.com)', False);
  AgentPage.Add('Agent ID из панели мониторинга', False);
  AgentPage.Add('Одноразовый Bearer token из панели', True);
  AgentPage.Values[0] := 'https://monitor.example.com';
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := ExistingAgentConfig and (PageID = AgentPage.ID);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  ServerUrl: String;
begin
  Result := True;
  if (CurPageID = AgentPage.ID) and not ExistingAgentConfig then begin
    ServerUrl := Trim(AgentPage.Values[0]);
    if (Copy(Lowercase(ServerUrl), 1, 8) <> 'https://') or ContainsUnsafeLineBreak(ServerUrl) or (Pos('"', ServerUrl) > 0) then begin
      MsgBox('Укажите публичный HTTPS URL сервера.', mbError, MB_OK);
      Result := False;
      exit;
    end;
    if (Trim(AgentPage.Values[1]) = '') or (Trim(AgentPage.Values[2]) = '') then begin
      MsgBox('Укажите Agent ID и token из веб-панели.', mbError, MB_OK);
      Result := False;
      exit;
    end;
    if ContainsUnsafeLineBreak(AgentPage.Values[1]) or ContainsUnsafeLineBreak(AgentPage.Values[2]) then begin
      MsgBox('Agent ID и token должны быть в одной строке.', mbError, MB_OK);
      Result := False;
      exit;
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  AgentData: String;
  ConfigFile: String;
  ConfigContents: String;
  ResultCode: Integer;
  TaskCommand: String;
begin
  if CurStep = ssPostInstall then begin
    if not ExistingAgentConfig then begin
      AgentData := ExpandConstant('{commonappdata}\SRV Monitoring Agent');
    ConfigFile := AgentData + '\agent.json';
    ConfigContents :=
      '{' + #13#10 +
      '  "server_url": "' + JsonEscape(Trim(AgentPage.Values[0])) + '",' + #13#10 +
      '  "agent_id": "' + JsonEscape(Trim(AgentPage.Values[1])) + '",' + #13#10 +
      '  "token": "' + JsonEscape(Trim(AgentPage.Values[2])) + '",' + #13#10 +
      '  "interval_seconds": 60,' + #13#10 +
      '  "verify_tls": true,' + #13#10 +
      '  "timeout_seconds": 15,' + #13#10 +
      '  "windows_event_minutes": 15,' + #13#10 +
      '  "disk_warning_free_percent": 15,' + #13#10 +
      '  "disk_critical_free_percent": 8,' + #13#10 +
      '  "monitored_services": [],' + #13#10 +
      '  "backup_status_file": "",' + #13#10 +
      '  "external_checks": []' + #13#10 +
      '}' + #13#10;
    if not SaveStringToFile(ConfigFile, ConfigContents, False) then
      RaiseException('Не удалось создать конфигурацию агента.');

    end;

    TaskCommand := '/Create /F /SC ONSTART /TN "SRV Monitoring Remote Agent" /TR "' + ExpandConstant('{app}\{#AppExe}') + '" /RU SYSTEM /RL HIGHEST';
    if not Exec(ExpandConstant('{sys}\schtasks.exe'), TaskCommand, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
      RaiseException('Не удалось зарегистрировать задачу Windows для удалённого агента.');
    Exec(ExpandConstant('{sys}\schtasks.exe'), '/Run /TN "SRV Monitoring Remote Agent"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  end;
end;
