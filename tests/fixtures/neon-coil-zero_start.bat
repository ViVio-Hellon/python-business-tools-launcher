@echo off
rem ============================================================
rem  NEON COIL ZERO - start (development / test run)
rem  Runs main.py from the folder this file is in.
rem  Usage: start.bat [--debug] [--seed N] [--software-scaling]
rem ============================================================
setlocal EnableExtensions
cd /d "%~dp0"
title NEON COIL ZERO
set "PYGAME_HIDE_SUPPORT_PROMPT=1"
echo [INFO] Folder : %CD%

if not exist "main.py" goto :no_main
if not exist "game\bootstrap.py" goto :no_main

call :find_python
if errorlevel 1 goto :fail
call :check_version
if errorlevel 1 goto :fail

%PY% -c "import pygame" >nul 2>nul
if not errorlevel 1 goto :run

echo [WARN] pygame is not installed for: %PY%
choice /c YN /m "Install the required libraries now by running setup.bat"
if errorlevel 2 goto :need_setup
call setup.bat /nopause
if errorlevel 1 goto :fail
set "PY=.venv\Scripts\python.exe"
%PY% -c "import pygame" >nul 2>nul
if errorlevel 1 goto :need_setup

:run
echo [INFO] Python : %PY%
%PY% -c "import sys, pygame; print('[INFO] Version: Python ' + sys.version.split()[0] + ' / pygame ' + pygame.version.ver)"
echo [INFO] Starting NEON COIL ZERO ...
%PY% main.py %*
set "EXITCODE=%ERRORLEVEL%"
if "%EXITCODE%"=="0" exit /b 0
echo.
echo [ERROR] The game stopped with exit code %EXITCODE%.
echo         Check the messages above and logs\neon_coil_zero.log
goto :fail

:no_main
echo [ERROR] main.py or the game folder was not found next to start.bat.
echo         If you opened start.bat from inside a ZIP file, extract the whole ZIP first.
goto :fail

:need_setup
echo [ERROR] pygame is not available. Run setup.bat, then run start.bat again.
goto :fail

:fail
echo.
pause
exit /b 1

rem ------------------------------------------------------------
rem  Subroutines. Do not put paths inside ( ) blocks: a ")" in a
rem  folder name such as "Program Files (x86)" breaks cmd parsing.
rem ------------------------------------------------------------
:find_python
set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if defined PY exit /b 0
where py >nul 2>nul
if errorlevel 1 goto :find_python_path
for %%v in (3.13 3.12 3.11 3.10 3.14) do call :try_py %%v
if defined PY exit /b 0
:find_python_path
where python >nul 2>nul
if errorlevel 1 goto :no_python
rem The Microsoft Store shortcut "python" exists but does not run Python.
python -c "import sys" >nul 2>nul
if errorlevel 1 goto :no_python
set "PY=python"
exit /b 0
:no_python
echo [ERROR] Python was not found.
echo         Install Python 3.13 64-bit from https://www.python.org/downloads/windows/
echo         and turn on "Add python.exe to PATH" in the installer.
exit /b 1

:try_py
if defined PY exit /b 0
py -%1 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PY=py -%1"
exit /b 0

:check_version
%PY% -c "import sys, struct; ok = (3, 10) <= sys.version_info[:2] <= (3, 14) and struct.calcsize('P') == 8; sys.exit(0 if ok else 1)" >nul 2>nul
if not errorlevel 1 exit /b 0
echo [ERROR] This game needs Python 3.10 - 3.14, 64-bit. Found:
%PY% -c "import sys, platform; print('        Python ' + sys.version.split()[0] + ' ' + platform.architecture()[0] + ' at ' + sys.executable)"
if "%PY%"==".venv\Scripts\python.exe" echo         Delete the .venv folder and run setup.bat again.
exit /b 1
