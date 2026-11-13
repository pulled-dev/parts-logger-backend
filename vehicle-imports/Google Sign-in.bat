@echo off
title Pulled Apart - Google Business Profile sign-in
rem One-time (or after "sign-in expired"): connects Update Vehicles.bat to the
rem Google Business Profile API. Sign in as nmabmm11@gmail.com and click Allow.
set "HERE=%~dp0"
set "PYTHONIOENCODING=utf-8"
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY where python >nul 2>&1 && set "PY=python"
if not defined PY (echo ERROR: Python is not installed or not on PATH.& goto end)
%PY% "%HERE%update_gbp.py" --authorize
if errorlevel 1 goto end
echo.
echo Checking the connection (nothing is uploaded)...
%PY% "%HERE%update_gbp.py" --api-check
:end
echo.
pause
