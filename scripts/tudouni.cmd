@echo off
setlocal
python "%~dp0tudouni.py" %*
exit /b %ERRORLEVEL%
