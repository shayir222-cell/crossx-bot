@echo off
chcp 65001 >nul
echo.
echo ╔══════════════════════════════════════╗
echo ║     CrossX Pro Bot — Установка      ║
echo ╚══════════════════════════════════════╝
echo.

:: Проверяем Python
py --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [!] Python не найден. Скачиваю и устанавливаю...
    powershell -Command "Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.12.4/python-3.12.4-amd64.exe' -OutFile 'python_installer.exe'"
    echo [*] Устанавливаю Python 3.12...
    python_installer.exe /quiet InstallAllUsers=0 PrependPath=1 Include_pip=1
    del python_installer.exe
    echo [OK] Python установлен. Перезапусти этот файл.
    pause
    exit
)

echo [OK] Python найден.
echo.

:: Устанавливаем зависимости
echo [*] Устанавливаю библиотеки...
py -m pip install flask requests gunicorn --quiet
echo [OK] Библиотеки установлены.
echo.

:: Создаём .env если нет
if not exist ".env" (
    copy .env.example .env >nul
    echo [!] Создан файл .env — ОТКРОЙ ЕГО И ВСТАВЬ СВОИ КЛЮЧИ!
    echo.
    notepad .env
) else (
    echo [OK] Файл .env уже существует.
)

echo.
echo ╔══════════════════════════════════════╗
echo ║         Запуск бота локально         ║
echo ╚══════════════════════════════════════╝
echo.
echo Бот запускается на http://localhost:8080
echo Для остановки нажми Ctrl+C
echo.

:: Загружаем .env переменные
for /f "tokens=1,2 delims==" %%a in (.env) do (
    if not "%%a"=="" if not "%%b"=="" set %%a=%%b
)

py bot.py
pause
