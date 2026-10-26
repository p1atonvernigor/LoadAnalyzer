@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -3 -c "import psutil" 2>nul || (
    echo Устанавливаю недостающие пакеты, это нужно один раз...
    py -3 -m pip install -r requirements.txt
)
py -3 -m analyzer
if errorlevel 1 (
    echo.
    echo Приложение завершилось с ошибкой. Скопируйте текст выше и отправьте Claude.
)
pause
