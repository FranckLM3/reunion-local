@echo off
rem Reunion sous Windows : icone dans la zone de notification, sans fenetre noire (zone.py).
rem Sans la bibliotheque de l'icone, repli sur la fenetre de console (la fermer arrete l'outil).
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Reunion est incomplete : une partie de l'installation manque. Reinstallez Reunion.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -c "import pystray" >nul 2>&1
if not errorlevel 1 if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" zone.py
  exit /b 0
)
title Reunion - ne pas fermer pendant un enregistrement
".venv\Scripts\python.exe" serveur.py
if errorlevel 1 pause
