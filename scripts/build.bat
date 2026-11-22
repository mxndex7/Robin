@echo off
chcp 65001 > nul
rem Roda a partir da raiz do projeto (o build.bat fica em scripts\): dist\ e build\ nascem lá.
cd /d "%~dp0.."

echo ========================================================
echo    ROBIN // BUILD (PyInstaller, onedir, sem console)
echo ========================================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [ERRO] .venv nao encontrado. Crie o ambiente antes:
    echo        py -3.13 -m venv .venv
    echo        .venv\Scripts\python -m pip install -r requirements-dev.txt
    goto :fim
)

rem Com o Robin aberto, o PyInstaller apaga parte do dist\Robin e para no primeiro DLL em uso.
rem Pergunta antes de tocar no dist. Caminhos completos: chamado pelo Git Bash, o "find" do PATH
rem seria o do Unix.
call :robin_aberto
if errorlevel 1 goto :build

set /a TENTATIVAS=0
:pergunta
set "RESPOSTA="
set /p "RESPOSTA=O Robin esta aberto. Fechar o Robin agora? (S/N) "
if /i "%RESPOSTA%"=="S" goto :fechar
if /i "%RESPOSTA%"=="N" goto :cancelado
set /a TENTATIVAS+=1
if %TENTATIVAS% geq 3 goto :cancelado
goto :pergunta

:cancelado
echo Build cancelado: o dist nao foi tocado.
goto :fim

:fechar
rem O X do Robin so esconde na bandeja, entao um taskkill sem /F nao fecha. Primeiro o pedido
rem educado (--quit: o Robin aberto sai e tira o icone da bandeja); se nao bastar em ~10s, /F.
echo Fechando o Robin...
if exist "dist\Robin\Robin.exe" "dist\Robin\Robin.exe" --quit
set /a ESPERA=0
:esperar
call :robin_aberto
if errorlevel 1 goto :build
set /a ESPERA+=1
if %ESPERA% equ 10 "%SystemRoot%\System32\taskkill.exe" /IM Robin.exe /T /F > nul 2>&1
if %ESPERA% geq 20 (
    echo [ERRO] O Robin continua aberto. Feche pela bandeja e rode o build de novo.
    goto :fim
)
"%SystemRoot%\System32\PING.EXE" -n 2 127.0.0.1 > nul
goto :esperar

:build
rem Os dados (memory.md, .env) ficam em %APPDATA%\Robin: o dist pode ser recriado do zero.
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean scripts\robin.spec
if errorlevel 1 (
    echo.
    echo [ERRO] O build falhou. Veja as mensagens acima.
    goto :fim
)

rem Atalho no menu Iniciar (criado ou atualizado), com o icone e o AppUserModelID da janela.
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "scripts\shortcut.ps1" -Target "dist\Robin\Robin.exe"
if errorlevel 1 echo [AVISO] Nao foi possivel criar o atalho no menu Iniciar.

echo.
echo ========================================================
echo  Pronto: dist\Robin\Robin.exe
echo  Dados (memoria e API Key) em %APPDATA%\Robin
echo ========================================================
goto :fim

rem errorlevel 0 se algum Robin.exe estiver aberto, 1 se nao.
:robin_aberto
"%SystemRoot%\System32\tasklist.exe" /FI "IMAGENAME eq Robin.exe" /NH 2>nul | "%SystemRoot%\System32\find.exe" /I "Robin.exe" > nul
exit /b %errorlevel%

:fim
echo.
pause
