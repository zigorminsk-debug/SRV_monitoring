#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

#define AppName "SRV Monitoring Server"
#define AppExe "SRVMonitoringServer.exe"

[Setup]
AppId={{0E2E38A2-7AD7-4F62-90AB-7010B6E87020}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=SRV Monitoring
DefaultDirName={autopf}\SRV Monitoring Server
DefaultGroupName=SRV Monitoring Server
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
OutputDir=..\build\installers
OutputBaseFilename=SRV-Monitoring-Server-Setup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
SetupLogging=yes
UninstallDisplayName=SRV Monitoring Server
UninstallDisplayIcon={app}\{#AppExe}
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Dirs]
Name: "{commonappdata}\SRV Monitoring"; Permissions: admins-full system-full
Name: "{commonappdata}\SRV Monitoring\data"; Permissions: admins-full system-full
Name: "{commonappdata}\SRV Monitoring\certs"; Permissions: admins-full system-full
Name: "{commonappdata}\SRV Monitoring\logs"; Permissions: admins-full system-full

[Files]
Source: "..\build\server\SRVMonitoringServer\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Открыть панель SRV Monitoring"; Filename: "{code:GetDashboardUrl}"; IconFilename: "{app}\{#AppExe}"
Name: "{group}\Сервер: конфигурация"; Filename: "{sys}\notepad.exe"; Parameters: """{commonappdata}\SRV Monitoring\.env"""
Name: "{group}\Удалить SRV Monitoring Server"; Filename: "{uninstallexe}"

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""SRV Monitoring HTTPS"""; Flags: runhidden waituntilterminated; RunOnceId: "DeleteServerFirewallRule"

[Code]
var
  ServerPage: TInputQueryWizardPage;
  ExistingConfig: Boolean;

function ReadExistingSetting(Key: String): String;
var
  Lines: TArrayOfString;
  Index: Integer;
  Prefix: String;
begin
  Result := '';
  Prefix := Key + '=';
  if LoadStringsFromFile(ExpandConstant('{commonappdata}\SRV Monitoring\.env'), Lines) then
    for Index := 0 to GetArrayLength(Lines) - 1 do
      if Copy(Lines[Index], 1, Length(Prefix)) = Prefix then begin
        Result := Copy(Lines[Index], Length(Prefix) + 1, MaxInt);
        exit;
      end;
end;

function GetDashboardUrl(Param: String): String;
begin
  if ExistingConfig then
    Result := ReadExistingSetting('PUBLIC_BASE_URL')
  else
    Result := Trim(ServerPage.Values[0]);
end;

function ContainsUnsafeLineBreak(Value: String): Boolean;
begin
  Result := (Pos(#13, Value) > 0) or (Pos(#10, Value) > 0);
end;

function InitializeUninstall(): Boolean;
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\schtasks.exe'), '/End /TN "SRV Monitoring Server"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(ExpandConstant('{sys}\schtasks.exe'), '/Delete /TN "SRV Monitoring Server" /F', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := True;
end;

procedure InitializeWizard;
begin
  ExistingConfig := FileExists(ExpandConstant('{commonappdata}\SRV Monitoring\.env'));
  ServerPage := CreateInputQueryPage(wpSelectDir,
    'Параметры сервера',
    'Настройте адрес панели и TLS-сертификат',
    'Укажите публичный HTTPS URL, порт и PEM-сертификат. Ключ и конфигурация будут сохранены в ProgramData с ACL для администраторов и SYSTEM.');
  ServerPage.Add('Публичный URL панели (например, https://monitor.example.com)', False);
  ServerPage.Add('Порт HTTPS на этом сервере', False);
  ServerPage.Add('Допустимые Host через запятую (FQDN и/или публичный IP)', False);
  ServerPage.Add('Путь к TLS certificate PEM-файлу', False);
  ServerPage.Add('Путь к TLS private key PEM-файлу', False);
  ServerPage.Values[0] := 'https://monitor.example.com';
  ServerPage.Values[1] := '443';
  ServerPage.Values[2] := 'monitor.example.com';
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := ExistingConfig and (PageID = ServerPage.ID);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  PortNumber: Integer;
  PublicUrl: String;
begin
  Result := True;
  if (CurPageID = ServerPage.ID) and not ExistingConfig then begin
    PublicUrl := Trim(ServerPage.Values[0]);
    if (Copy(Lowercase(PublicUrl), 1, 8) <> 'https://') or ContainsUnsafeLineBreak(PublicUrl) or (Pos('"', PublicUrl) > 0) then begin
      MsgBox('Укажите корректный публичный URL, начинающийся с https://.', mbError, MB_OK);
      Result := False;
      exit;
    end;
    PortNumber := StrToIntDef(Trim(ServerPage.Values[1]), 0);
    if (PortNumber < 1) or (PortNumber > 65535) then begin
      MsgBox('Порт должен быть числом от 1 до 65535.', mbError, MB_OK);
      Result := False;
      exit;
    end;
    if (Trim(ServerPage.Values[2]) = '') or ContainsUnsafeLineBreak(ServerPage.Values[2]) or (Pos('"', ServerPage.Values[2]) > 0) then begin
      MsgBox('Укажите корректное имя Host и/или IP без кавычек.', mbError, MB_OK);
      Result := False;
      exit;
    end;
    if not FileExists(Trim(ServerPage.Values[3])) then begin
      MsgBox('Не найден TLS certificate PEM-файл.', mbError, MB_OK);
      Result := False;
      exit;
    end;
    if not FileExists(Trim(ServerPage.Values[4])) then begin
      MsgBox('Не найден TLS private key PEM-файл.', mbError, MB_OK);
      Result := False;
      exit;
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  AppData: String;
  CertTarget: String;
  KeyTarget: String;
  EnvFile: String;
  EnvContents: String;
  ResultCode: Integer;
  TaskCommand: String;
  PortNumber: String;
begin
  if CurStep = ssPostInstall then begin
    if not ExistingConfig then begin
      AppData := ExpandConstant('{commonappdata}\SRV Monitoring');
      CertTarget := AppData + '\certs\server.crt';
      KeyTarget := AppData + '\certs\server.key';
      if not FileCopy(Trim(ServerPage.Values[3]), CertTarget, False) then
        RaiseException('Не удалось скопировать TLS certificate PEM-файл.');
      if not FileCopy(Trim(ServerPage.Values[4]), KeyTarget, False) then
        RaiseException('Не удалось скопировать TLS private key PEM-файл.');

      PortNumber := Trim(ServerPage.Values[1]);
      EnvFile := AppData + '\.env';
      EnvContents :=
        'APP_HOST=0.0.0.0' + #13#10 +
        'APP_PORT=' + PortNumber + #13#10 +
        'PUBLIC_BASE_URL=' + Trim(ServerPage.Values[0]) + #13#10 +
        'TRUSTED_HOSTS=' + Trim(ServerPage.Values[2]) + #13#10 +
        'REQUIRE_HTTPS=true' + #13#10 +
        'SECURE_COOKIES=true' + #13#10 +
        'FORWARDED_ALLOW_IPS=127.0.0.1' + #13#10 +
        'SSL_CERTFILE=' + CertTarget + #13#10 +
        'SSL_KEYFILE=' + KeyTarget + #13#10 +
        'MONITORING_DATA_DIR=' + AppData + '\data' + #13#10 +
        'ADMIN_USER=admin' + #13#10 +
        'ADMIN_PASSWORD=' + #13#10;
      if not SaveStringToFile(EnvFile, EnvContents, False) then
        RaiseException('Не удалось создать конфигурационный файл сервера.');

    end;

    if ExistingConfig then
      PortNumber := ReadExistingSetting('APP_PORT');
    Exec(ExpandConstant('{sys}\netsh.exe'), 'advfirewall firewall delete rule name="SRV Monitoring HTTPS"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    if not Exec(ExpandConstant('{sys}\netsh.exe'), 'advfirewall firewall add rule name="SRV Monitoring HTTPS" dir=in action=allow protocol=TCP localport=' + PortNumber + ' profile=any', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
      RaiseException('Не удалось создать правило Windows Firewall.');

    TaskCommand := '/Create /F /SC ONSTART /TN "SRV Monitoring Server" /TR "' + ExpandConstant('{app}\{#AppExe}') + '" /RU SYSTEM /RL HIGHEST';
    if not Exec(ExpandConstant('{sys}\schtasks.exe'), TaskCommand, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
      RaiseException('Не удалось зарегистрировать задачу Windows для запуска сервера.');
    Exec(ExpandConstant('{sys}\schtasks.exe'), '/Run /TN "SRV Monitoring Server"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  end;
end;
