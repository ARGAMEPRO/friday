@echo off
chcp 65001 >nul
title F.R.I.D.A.Y.
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo  Python не найден в PATH.
  echo  Установи Python 3.10+ с python.org и запусти этот файл снова.
  echo.
  pause
  exit /b 1
)

echo.
echo   F.R.I.D.A.Y. — Female Replacement Intelligent Digital Assistant Youth
echo   Локальный ассистент: голос, память, правки, живой разговор.
echo   Запускаю сервер и открываю интерфейс...
echo   Закрой это окно, чтобы выключить Пятницу.
echo.

python server.py
if errorlevel 1 (
  echo.
  echo  Сервер завершился с ошибкой. Скопируй текст выше.
  pause
)
