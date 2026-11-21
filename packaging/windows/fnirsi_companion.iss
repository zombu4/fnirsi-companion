; Inno Setup script of the FNIRSI Companion installer.
;   ISCC /DAppVersion=1.2.3 /DSourceDir=<dist\fnirsi-companion> /DOutputDir=<dir> /DLicenseFile=<LICENSE> /DThirdParty=<THIRD_PARTY_LICENSES.txt> packaging\windows\fnirsi_companion.iss
; Installs the PyInstaller one-folder output (Python, PySide6 / Qt, pyqtgraph, numpy, pyserial) for all users in
; Program Files (administrator rights, asked by Windows), with a Start menu entry and an optional desktop icon.  The
; program writes nothing into its folder: settings, cache and recordings are per user (fnirsi_companion/core/paths.py).
; Without administrator rights, "/CURRENTUSER" on the command line installs for the current user only
; (%LOCALAPPDATA%\Programs).

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #error SourceDir (the PyInstaller folder dist\fnirsi-companion) is required
#endif
#ifndef OutputDir
  #define OutputDir "."
#endif
#ifndef LicenseFile
  #error LicenseFile (the project LICENSE) is required
#endif
#ifndef ThirdParty
  #error ThirdParty (THIRD_PARTY_LICENSES.txt) is required
#endif

[Setup]
AppId={{5435C065-5BC7-477C-B270-3CA68AD392EF}
AppName=FNIRSI Companion
AppVersion={#AppVersion}
AppPublisher=zombu4
AppPublisherURL=https://github.com/zombu4/fnirsi-companion
DefaultDirName={autopf}\FNIRSI Companion
DefaultGroupName=FNIRSI Companion
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=commandline
LicenseFile={#LicenseFile}
OutputDir={#OutputDir}
OutputBaseFilename=FNIRSI-Companion-{#AppVersion}-windows-x64
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName=FNIRSI Companion
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "Create a desktop icon"; GroupDescription: "Additional icons:"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#ThirdParty}"; DestDir: "{app}"; DestName: "THIRD_PARTY_LICENSES.txt"; Flags: ignoreversion

[Icons]
Name: "{group}\FNIRSI Companion"; Filename: "{app}\fnirsi-companion.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\FNIRSI Companion"; Filename: "{app}\fnirsi-companion.exe"; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{group}\Uninstall FNIRSI Companion"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\fnirsi-companion.exe"; Description: "Start the FNIRSI Companion now"; Flags: postinstall nowait skipifsilent
