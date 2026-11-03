@echo off
rem Run valet straight from this clone (Windows cmd / PowerShell). Add this bin\ folder to PATH.
rem Set VALET_LAUNCH_PYTHON to choose the interpreter that runs valet itself (any Python 3.9+).
if defined VALET_LAUNCH_PYTHON (
  "%VALET_LAUNCH_PYTHON%" "%~dp0_launch.py" %*
) else (
  python "%~dp0_launch.py" %*
)
exit /b %ERRORLEVEL%
