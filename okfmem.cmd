@echo off
setlocal
where py >nul 2>nul
if %ERRORLEVEL%==0 (
    py "%~dp0okfmem" %*
) else (
    python "%~dp0okfmem" %*
)
