@echo off
REM Builds a single self-contained netplot-agent.exe (no Python install needed).
REM Run once on any Windows PC after installing Python 3.10+ (check "Add to PATH").
setlocal
where python >nul 2>nul || (echo Python not found on PATH & exit /b 1)
python -m pip install --upgrade pyinstaller
python -m PyInstaller --onefile --name netplot-agent ^
  --collect-submodules netplot ^
  -c ^
  -p "%~dp0netplot" -m netplot.__main__
echo.
echo Built: dist\netplot-agent.exe
echo Usage: netplot-agent.exe agent --server https://netplot.pdulab.org --token YOUR_TOKEN
pause
