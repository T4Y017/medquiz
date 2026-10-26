@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ==========================================
echo   MedQuiz - Windows paketleme
echo ==========================================

where python >nul 2>nul
if errorlevel 1 (
    echo [HATA] Python bulunamadi. https://www.python.org/downloads/ adresinden Python 3.11 veya 3.12 kurun
    echo        ve kurulumda "Add python.exe to PATH" kutusunu isaretleyin.
    pause
    exit /b 1
)

if not exist .venv (
    echo [1/4] Sanal ortam olusturuluyor...
    python -m venv .venv || goto :err
)
call .venv\Scripts\activate.bat

echo [2/4] Bagimliliklar kuruluyor (ilk seferde birkac dakika surer)...
python -m pip install --upgrade pip >nul
pip install -r requirements.txt pyinstaller || goto :err

echo [3/4] Uygulama paketleniyor...
pyinstaller --noconfirm --clean medquiz.spec || goto :err

echo [4/4] Kurulum dosyasi (varsa Inno Setup ile)...
set ISCC="%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist %ISCC% (
    %ISCC% installer.iss || goto :err
    echo.
    echo TAMAM: Kurulum dosyasi  -^>  installer_output\MedQuiz_Kurulum.exe
) else (
    echo Inno Setup bulunamadi; kurulum dosyasi atlandi.
    echo Istersen https://jrsoftware.org/isdl.php adresinden kurup bu betigi tekrar calistir.
)
echo.
echo Uygulama klasoru: dist\MedQuiz\MedQuiz.exe
echo (Bu klasoru zipleyip aile bilgisayarlarina kopyalayabilirsin.)
pause
exit /b 0

:err
echo.
echo [HATA] Islem basarisiz oldu. Yukaridaki mesajlari kontrol edin.
pause
exit /b 1
