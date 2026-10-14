@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Pulled Apart - Update Vehicle Database

rem ================================================================
rem  Pulled Apart - BreakerPro CSV -> Parts Logger vehicle database
rem
rem  HOW TO USE (either way works):
rem    1. Drop BreakerPro stock export CSV(s) into the "inbox" folder,
rem       then double-click this file.
rem    2. Or drag a CSV straight onto this file.
rem
rem  What it does for every CSV in inbox:
rem    - Adds any vehicle not already in the database
rem    - Fills in BLANK fields (year, VIN, paint, engine) on existing
rem      vehicles - never overwrites anything already set
rem    - Saves a full log to "logs"
rem    - Moves the CSV to "done" if it worked; leaves it in "inbox"
rem      if anything failed, so you can just run it again
rem ================================================================

set "HERE=%~dp0"
set "INBOX=%HERE%inbox"
set "DONE=%HERE%done"
set "LOGS=%HERE%logs"
set "IMPORTER=%HERE%..\scripts\import_breakerpro.py"
set "PYTHONIOENCODING=utf-8"

if not exist "%INBOX%" mkdir "%INBOX%"
if not exist "%DONE%"  mkdir "%DONE%"
if not exist "%LOGS%"  mkdir "%LOGS%"

if not exist "%IMPORTER%" (
  echo ERROR: importer not found at:
  echo   %IMPORTER%
  echo This folder must sit inside parts-logger-backend.
  goto finish
)

rem ---- find Python ----
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
  where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo ERROR: Python is not installed or not on PATH.
  goto finish
)

rem ---- drag-and-drop: copy any dropped CSVs into inbox ----
:dropped
if "%~1"=="" goto run
if /i "%~x1"==".csv" (
  copy /y "%~1" "%INBOX%\" >nul
  echo Added to inbox: %~nx1
) else (
  echo Ignored ^(not a .csv^): %~nx1
)
shift
goto dropped

:run
for /f %%T in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HHmmss"') do set "STAMP=%%T"

set /a COUNT=0
set /a FAILS=0

echo.
echo ================================================
echo   Updating Parts Logger vehicle database
echo ================================================

for %%F in ("%INBOX%\*.csv") do (
  set /a COUNT+=1
  set "LOG=%LOGS%\!STAMP!_%%~nF.log"
  echo.
  echo Importing: %%~nxF
  echo   ^(checks every vehicle against the live database - takes a minute or two^)
  %PY% "%IMPORTER%" "%%~fF" --fill-missing > "!LOG!" 2>&1
  if errorlevel 1 (
    set /a FAILS+=1
    echo.
    echo   ** PROBLEM - some rows failed or the database could not be reached.
    echo   ** CSV left in inbox - just run this again. Details in the log:
    echo   **   !LOG!
    echo.
    findstr /c:"FAIL " /c:"Error" /c:"Traceback" "!LOG!"
  ) else (
    move /y "%%~fF" "%DONE%\!STAMP!_%%~nxF" >nul
    echo.
    echo   New vehicles added:
    findstr /c:"] NEW " "!LOG!" || echo     none - all already in the database
    echo.
    findstr /c:"Created (new)" /c:"blanks filled" /c:"Already complete" /c:"Skipped:" /c:"Failed:" "!LOG!"
    echo.
    echo   Done - CSV moved to "done". Full log: !LOG!
  )
)

if !COUNT!==0 (
  echo.
  echo Nothing to import - the inbox folder is empty.
  echo Export the Stock Reference CSV from BreakerPro and put it in:
  echo   %INBOX%
)

if !COUNT! GTR 0 if !FAILS!==0 (
  echo.
  echo All done. !COUNT! file^(s^) imported successfully.
)

rem ── Website update step ──
set "WEBSITE_SCRIPT=%HERE%update_website.py"
if !FAILS!==0 if exist "%WEBSITE_SCRIPT%" (
  echo.
  echo ================================================
  echo   Updating pulledapart.co.uk website
  echo ================================================
  echo.
  %PY% "%WEBSITE_SCRIPT%"
  if errorlevel 1 (
    echo.
    echo   ** Website update had an issue - check the output above.
  )
)

rem -- Stock quick reference step --
set "STOCK_SCRIPT=%HERE%update_stock_sheets.py"
if !FAILS!==0 if exist "%STOCK_SCRIPT%" (
  %PY% "%STOCK_SCRIPT%"
  if errorlevel 1 (
    echo.
    echo   ** Quick reference update had an issue - check the output above.
  )
)

:finish
echo.
pause
endlocal
