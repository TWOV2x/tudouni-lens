@echo off
setlocal
py -3 --version >nul 2>&1
if errorlevel 1 goto python_fallback
py -3 "%~dp0tudouni.py" %*
exit /b %ERRORLEVEL%
:python_fallback
python "%~dp0tudouni.py" %*
exit /b %ERRORLEVEL%
