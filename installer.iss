; Inno Setup betiği — dist\MedQuiz klasöründen tek bir kurulum dosyası üretir.
#define AppName "MedQuiz"
#define AppVersion "1.3.0"

[Setup]
AppId={{6B1C2E7A-4D3F-4B8E-9A61-2F0C5D7E8A11}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=MedQuiz
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
OutputDir=installer_output
OutputBaseFilename=MedQuiz_Kurulum
SetupIconFile=assets\icon.ico
UninstallDisplayIcon={app}\MedQuiz.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "turkish"; MessagesFile: "compiler:Languages\Turkish.isl"

[Tasks]
Name: "desktopicon"; Description: "Masaüstüne kısayol oluştur"; GroupDescription: "Ek simgeler:"

[Files]
Source: "dist\MedQuiz\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\MedQuiz.exe"
Name: "{group}\{#AppName} Kaldır"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\MedQuiz.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\MedQuiz.exe"; Description: "MedQuiz'i başlat"; Flags: nowait postinstall skipifsilent
