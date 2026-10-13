@echo off
rem ============================================================
rem  skm - Skill Manager launcher for Windows
rem  NOTE: keep this file pure ASCII. cmd.exe reads .bat files
rem        using the system ANSI code page (GBK on Chinese
rem        Windows), so any UTF-8 Chinese text here breaks it.
rem
rem  Usage:
rem    skm scan
rem    skm add <path>
rem    skm list
rem    skm <anything else>
rem
rem  Double-click this file with no arguments to get a menu.
rem ============================================================
setlocal EnableExtensions

set "SKM_HOME=%~dp0"
if "%SKM_HOME:~-1%"=="\" set "SKM_HOME=%SKM_HOME:~0,-1%"

where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo [X] Python not found in PATH.
    echo     Please install Python 3.9+ and add it to PATH.
    echo.
    if "%SKM_NO_PAUSE%"=="" pause
    exit /b 1
)

if "%~1"=="" goto menu

python "%SKM_HOME%\skm.py" %*
set "SKM_RC=%ERRORLEVEL%"
if not "%SKM_RC%"=="0" (
    echo.
    echo [X] skm exited with code %SKM_RC%
)
exit /b %SKM_RC%

:menu
echo.
echo   Skill Manager  ^-  step 1 collect / step 2 web UI
echo   =========================================================
echo.
echo   [1] Scan and collect skills found on this PC
echo   [2] Show collected skills
echo   [3] Show category tree
echo   [4] Show library info
echo   [5] Open the web UI ^(browser)
echo   [6] Export a single-file offline HTML page
echo   [Q] Quit
echo.
set "SKM_CHOICE="
set /p "SKM_CHOICE=Choose (1-6, Q): "
echo.
if /i "%SKM_CHOICE%"=="1" goto do_scan
if /i "%SKM_CHOICE%"=="2" goto do_list
if /i "%SKM_CHOICE%"=="3" goto do_cat
if /i "%SKM_CHOICE%"=="4" goto do_info
if /i "%SKM_CHOICE%"=="5" goto do_ui
if /i "%SKM_CHOICE%"=="6" goto do_export
if /i "%SKM_CHOICE%"=="Q" exit /b 0
echo [X] Unknown choice.
pause
exit /b 1

:do_scan
python "%SKM_HOME%\skm.py" scan
goto done
:do_list
python "%SKM_HOME%\skm.py" list
goto done
:do_cat
python "%SKM_HOME%\skm.py" cat --list
goto done
:do_info
python "%SKM_HOME%\skm.py" info
goto done
:do_ui
python "%SKM_HOME%\skm.py" ui
goto done
:do_export
set "SKM_OUT=%USERPROFILE%\Desktop\skills.html"
set /p "SKM_OUT=Save to (default %USERPROFILE%\Desktop\skills.html): "
python "%SKM_HOME%\skm.py" export --html "%SKM_OUT%"
goto done

:done
echo.
pause
endlocal
exit /b 0
