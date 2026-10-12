@echo off
rem ============================================================================
rem deploy.bat - скрипт развёртывания сервера Poem (poem_sim) для Windows
rem
rem Назначение:
rem   1. Проверяет чек-лист аппаратных и программных требований
rem      (см. guide_server.md, разделы 2 и 3).
rem   2. Разворачивает сервер: venv + пакет poem-sim + конфигурация + тесты.
rem
rem Использование (cmd.exe, из каталога репозитория):
rem   deploy.bat [команда] [опции]
rem
rem Команды:
rem   check       только проверка чек-листа требований (по умолчанию)
rem   deploy      чек-лист + развёртывание (venv, pip install -e ".[test]")
rem   test        прогнать pytest в развёрнутом окружении .venv
rem   help        краткая справка
rem
rem Опции:
rem   /profile:min | /profile:recommended | /profile:prod
rem                 режим чек-листа (по умолчанию min)
rem   /dir:<путь>   каталог poem_sim (по умолчанию рядом со скриптом)
rem   /python:<имя> явный интерпретатор Python (по умолчанию поиск py/3.13..3.11)
rem   /skip-gpu     не проверять GPU
rem   /force        продолжать развёртывание даже при FAIL-пунктах чек-листа
rem
rem Коды выхода: 0 - успех; 1 - есть FAIL-пункты или ошибка установки;
rem              2 - ошибка использования (нет каталога poem_sim и т.п.).
rem ============================================================================

setlocal EnableExtensions EnableDelayedExpansion

set "PROFILE=min"
set "CMD=check"
set "SIM_DIR="
set "PY_CANDIDATE="
set "SKIP_GPU=0"
set "FORCE=0"
set "PASS_CNT=0"
set "WARN_CNT=0"
set "FAIL_CNT=0"

rem --------------------------- разбор аргументов ------------------------------
:parse_args
if "%~1"=="" goto args_done
set "ARG=%~1"
for /f "tokens=1 delims==" %%A in ("!ARG!") do set "KEY=%%A"
for /f "tokens=1,* delims==" %%A in ("!ARG!") do set "VAL=%%B"
if /i "!KEY!"=="!ARG!" (
    rem позиционный аргумент - команда
    if /i "!ARG!"=="check"    ( set "CMD=check"    & shift & goto parse_args )
    if /i "!ARG!"=="deploy"   ( set "CMD=deploy"   & shift & goto parse_args )
    if /i "!ARG!"=="test"     ( set "CMD=test"     & shift & goto parse_args )
    if /i "!ARG!"=="help"     ( set "CMD=help"     & shift & goto parse_args )
    if /i "!ARG!"=="/?"       ( set "CMD=help"     & shift & goto parse_args )
    if /i "!ARG!"=="-h"       ( set "CMD=help"     & shift & goto parse_args )
    if /i "!ARG!"=="--help"   ( set "CMD=help"     & shift & goto parse_args )
    if /i "!ARG!"=="/skip-gpu" ( set "SKIP_GPU=1"  & shift & goto parse_args )
    if /i "!ARG!"=="--skip-gpu" ( set "SKIP_GPU=1" & shift & goto parse_args )
    if /i "!ARG!"=="/force"    ( set "FORCE=1"     & shift & goto parse_args )
    if /i "!ARG!"=="--force"   ( set "FORCE=1"     & shift & goto parse_args )
    echo Неизвестный аргумент: !ARG!
    set "CMD=help"
    shift & goto parse_args
)
if /i "!KEY!"=="/profile" (
    if /i "!VAL!"=="min"         ( set "PROFILE=min"         ) else ^
    if /i "!VAL!"=="recommended" ( set "PROFILE=recommended" ) else ^
    if /i "!VAL!"=="prod"        ( set "PROFILE=prod"        ) else ^
    ( echo Неверный /profile: !VAL! ^(min^|recommended^|prod^) & exit /b 2 )
    shift & goto parse_args
)
if /i "!KEY!"=="/dir"    ( set "SIM_DIR=!VAL!"    & shift & goto parse_args )
if /i "!KEY!"=="/python" ( set "PY_CANDIDATE=!VAL!" & shift & goto parse_args )
echo Неизвестная опция: !ARG!
exit /b 2
:args_done

set "SCRIPT_DIR=%~dp0"
if "%SIM_DIR%"=="" set "SIM_DIR=%SCRIPT_DIR%poem_sim"
if not exist "%SIM_DIR%\" (
    echo [FAIL] Каталог poem_sim не найден: %SIM_DIR%  ^(укажите /dir:^\<путь^>^)
    exit /b 2
)

if /i "%CMD%"=="help" goto show_help

rem ============================= ЧЕК-ЛИСТ ======================================
call :hdr "Чек-лист требований сервера Poem (профиль: %PROFILE%)"
echo   Справочник требований: guide_server.md S2 (аппаратные), S3 (программные)

rem ---- пороги профиля (guide_server.md S2) ----
set "NEED_CPU=2"
set "NEED_RAM_MB=4096"
set "NEED_DISK_GB=10"
if /i "%PROFILE%"=="recommended" ( set "NEED_CPU=4" & set "NEED_RAM_MB=16384" & set "NEED_DISK_GB=50" )
if /i "%PROFILE%"=="prod"        ( set "NEED_CPU=4" & set "NEED_RAM_MB=16384" & set "NEED_DISK_GB=50" )

call :hdr "Аппаратные требования"

rem --- архитектура CPU ---
echo !PROCESSOR_ARCHITECTURE! | findstr /i /c:"AMD64" >nul && (
    call :ok "Архитектура CPU" "!PROCESSOR_ARCHITECTURE! (x86-64)"
) || (
    echo !PROCESSOR_ARCHITECTURE! | findstr /i /c:"ARM64" >nul && (
        call :ok "Архитектура CPU" "!PROCESSOR_ARCHITECTURE! (ARM64)"
    ) || call :warn "Архитектура CPU" "!PROCESSOR_ARCHITECTURE! - требуются x86-64 или ARM64"
)

rem --- количество логических ядер ---
set "CPU_CNT=0"
for /f "skip=1 delims=" %%C in ('wmic cpu get NumberOfLogicalProcessors ^| findstr /r "[0-9]"') do set "CPU_CNT=%%C"
if "!CPU_CNT!"=="0" for %%N in ("%NUMBER_OF_PROCESSORS%") do set "CPU_CNT=%%~N"
if !CPU_CNT! GEQ %NEED_CPU% (
    call :ok "Ядер CPU" "!CPU_CNT! логических (требуется \>= %NEED_CPU%)"
) else (
    call :fail "Ядер CPU" "!CPU_CNT! (требуется \>= %NEED_CPU%)"
)

rem --- свободная RAM (МБ) ---
set "FREE_RAM_MB="
for /f "skip=1 delims=" %%M in ('wmic OS get FreePhysicalMemory ^| findstr /r "[0-9]"') do set "FREE_RAM_KB=%%M"
if defined FREE_RAM_KB set /a "FREE_RAM_MB=FREE_RAM_KB/1024"
if defined FREE_RAM_MB if !FREE_RAM_MB! GEQ %NEED_RAM_MB% (
    call :ok "Свободная RAM" "!FREE_RAM_MB! МБ (требуется \>= %NEED_RAM_MB% МБ)"
) else (
    call :fail "Свободная RAM" "!FREE_RAM_MB! МБ (требуется \>= %NEED_RAM_MB% МБ)"
)

rem --- свободное место на диске каталога проекта (ГБ) ---
set "DRIVE_LETTER=%SIM_DIR:~0,1%"
set "FREE_DISK_GB="
set "FREE_DISK_BYTES="
for /f "skip=1 delims=" %%F in ('wmic logicaldisk where "DeviceID='!DRIVE_LETTER!:'" get FreeSpace /format:list ^| findstr /r "[0-9]"') do set "%%F"
if defined FreeSpace set "FREE_DISK_BYTES=!FreeSpace!"
if defined FREE_DISK_BYTES (
    for /f "tokens=1 delims=. " %%N in ("!FREE_DISK_BYTES!") do set "FREE_DISK_BYTES=%%N"
    set /a "FREE_DISK_GB=FREE_DISK_BYTES/1073741824"
)
if not defined FREE_DISK_GB (
    rem резервный способ: dir с меткой свободного места
    for %%d in ("%SIM_DIR%") do set "FREE_DISK_GB=-1"
)
if defined FREE_DISK_GB if !FREE_DISK_GB! GEQ %NEED_DISK_GB% (
    call :ok "Свободно на диске" "!FREE_DISK_GB! ГБ на !DRIVE_LETTER!: (требуется \>= %NEED_DISK_GB% ГБ + ~15 ГБ на веса моделей)"
) else (
    call :fail "Свободно на диске" "не удалось определить / меньше %NEED_DISK_GB% ГБ на !DRIVE_LETTER!:"
)

rem --- GPU (только для воркера генерации изображений, guide_server.md S2.2/S4.5) ---
if "%SKIP_GPU%"=="1" (
    call :warn "GPU" "проверка пропущена (/skip-gpu); генерация иллюстраций будет недоступна"
) else (
    set "GPU_FOUND=0"
    where nvidia-smi >nul 2>nul && ( nvidia-smi -L >nul 2>nul && set "GPU_FOUND=1" )
    if "!GPU_FOUND!"=="1" (
        call :ok "GPU" "nvidia-smi отвечает - доступен воркер генерации изображений"
    ) else (
        call :warn "GPU" "не обнаружен - допустимо: сервер работает без картинок (задачи остаются в queued); для генерации нужна NVIDIA 12+ ГБ VRAM"
    )
)

call :hdr "Программные требования"

rem --- Python >= 3.11 (tomllib из stdlib, guide_server.md S3) ---
set "PYEXE="
if not "%PY_CANDIDATE%"=="" (
    where %PY_CANDIDATE% >nul 2>nul && set "PYEXE=%PY_CANDIDATE%"
) else (
    rem сначала launcher py, затем конкретные версии, затем python3/python
    for %%P in (py -3.13 py -3.12 py -3.11 python3.13 python3.12 python3.11 python) do (
        if not defined PYEXE (
            if "%%P"=="py" (
                py -3 --version >nul 2>nul && set "PYEXE=py -3"
            ) else (
                where %%P >nul 2>nul && set "PYEXE=%%P"
            )
        )
    )
)
if not defined PYEXE (
    call :fail "Python" "интерпретатор не найден - установите Python \>= 3.11 (python.org или winget install Python.Python.3.12)"
    goto after_python
)
for /f "delims=" %%V in ('%PYEXE% -c "import sys;print('.'.join(map(str,sys.version_info[:3])))" 2^>nul') do set "PYVER=%%V"
%PYEXE% -c "import sys; sys.exit(0 if sys.version_info>=(3,11) else 1)" >nul 2>nul
if !errorlevel! equ 0 (
    call :ok "Python" "!PYVER! (!PYEXE!) - требуется \>= 3.11 (tomllib из stdlib)"
) else (
    call :fail "Python" "!PYVER! - требуется \>= 3.11 (используется tomllib)"
)
%PYEXE% -c "import tomllib" >nul 2>nul && (
    call :ok "tomllib" "доступен"
) || (
    call :fail "tomllib" "отсутствует - обновите Python до \>= 3.11"
)
%PYEXE% -m venv --help >nul 2>nul && (
    call :ok "venv" "модуль доступен"
) || call :fail "venv" "модуль venv недоступен - переустановите Python с компонентами"
%PYEXE% -m pip --version >nul 2>nul && (
    for /f "delims=" %%P in ('%PYEXE% -m pip --version 2^>nul') do call :ok "pip" "%%P"
) || call :fail "pip" "не найден - запустите: %PYEXE% -m ensurepip"
:after_python

where git >nul 2>nul && (
    for /f "delims=" %%G in ('git --version 2^>nul') do call :ok "git" "%%G"
) || call :warn "git" "не найден - нужен для клонирования репозитория (winget install Git.Git)"

rem --- setuptools >= 68 (build-system в pyproject.toml) ---
set "STVER="
if defined PYEXE for /f "delims=" %%S in ('%PYEXE% -c "import setuptools;print(setuptools.__version__)" 2^>nul') do set "STVER=%%S"
if defined STVER (
    for /f "delims=. tokens=1" %%A in ("!STVER!") do set "STMAJ=%%A"
    if !STMAJ! GEQ 68 (
        call :ok "setuptools" "!STVER! (\>= 68)"
    ) else (
        call :warn "setuptools" "!STVER! \< 68 - pip подтянет совместимую версию при сборке"
    )
) else (
    call :warn "setuptools" "не найден в системном Python - будет установлен в venv автоматически"
)

rem --- PostgreSQL: обязателен только профилю prod (schema.sql: UUID/JSONB, S4.3) ---
where psql >nul 2>nul && (
    for /f "delims=" %%P in ('psql --version 2^>nul') do call :ok "PostgreSQL" "%%P - схема schema.sql требует сервер 14+"
) || (
    if /i "%PROFILE%"=="prod" (
        call :fail "PostgreSQL" "не найден - продактив-развёртывание требует PostgreSQL 14+ (createdb/psql для schema.sql)"
    ) else (
        call :warn "PostgreSQL" "не найден - не требуется: ядро работает на in-memory LawStore (нужен только профилю prod)"
    )
)

rem --- ОС ---
ver | findstr /i "Windows" >nul && (
    for /f "delims=" %%O in ('ver') do call :ok "ОС" "Windows - переносимый сервер поддерживается ^(рекомендуемая платформа - Linux^)"
) || call :warn "ОС" "не Windows?"

call :hdr "Структура проекта"
for %%F in ("pyproject.toml" "schema.sql" "config\chronology.toml" "pytest.ini") do (
    if exist "%SIM_DIR%\%%~F" (
        call :ok "Файл" "%SIM_DIR%\%%~F"
    ) else (
        call :fail "Файл" "отсутствует %%~F"
    )
)
if exist "%SIM_DIR%\poem\" if exist "%SIM_DIR%\tests\" (
    call :ok "Каталоги" "poem\ и tests\ на месте"
) else (
    call :fail "Каталоги" "нет poem\ или tests\ - это не каталог проекта?"
)

call :hdr "Итог чек-листа"
echo   PASS: %PASS_CNT%   WARN: %WARN_CNT%   FAIL: %FAIL_CNT%
set "CHECK_RC=0"
if %FAIL_CNT% GTR 0 (
    echo   Есть невыполненные обязательные требования - устраните FAIL-пункты.
    set "CHECK_RC=1"
) else (
    echo   Обязательные требования выполнены. WARN-пункты - опциональные слои ^(GPU, PostgreSQL^), см. guide_server.md S2-S3.
)

if /i "%CMD%"=="check" exit /b %CHECK_RC%

if "%CHECK_RC%"=="1" if not "%FORCE%"=="1" (
    echo.
    echo Развёртывание прервано: чек-лист не пройден ^(используйте /force для принудительного запуска^).
    exit /b 1
)

call :hdr "Развёртывание сервера Poem"
if not defined PYEXE (
    echo [FAIL] Python не найден - развёртывание невозможно.
    exit /b 1
)

pushd "%SIM_DIR%"
echo -^> Изолированное окружение .venv
%PYEXE% -m venv .venv
if errorlevel 1 ( popd & echo [FAIL] Не удалось создать venv. & exit /b 1 )
set "VENV_PY=%CD%\.venv\Scripts\python.exe"

echo -^> pip install --upgrade pip
"%VENV_PY%" -m pip install --upgrade pip
if errorlevel 1 ( popd & echo [FAIL] Ошибка обновления pip. & exit /b 1 )

echo -^> pip install -e ".[test]"  (пакет poem-sim + pytest)
"%VENV_PY%" -m pip install -e ".[test]"
if errorlevel 1 ( popd & echo [FAIL] Ошибка установки пакета. & exit /b 1 )

echo -^> Проверка установки: импорт пакета и загрузка chronology.toml
"%VENV_PY%" -c "import poem; print('   import poem: ok')"
if errorlevel 1 ( popd & echo [FAIL] Импорт пакета poem не удался. & exit /b 1 )
"%VENV_PY%" -c "from poem.config import load_chronology_config; load_chronology_config('config/chronology.toml'); print('   chronology.toml: ok')"
if errorlevel 1 ( popd & echo [FAIL] Загрузка config/chronology.toml не удалась. & exit /b 1 )

if /i "%CMD%"=="test" goto run_tests

popd
echo.
echo Развёртывание завершено. Дальнейшие шаги:
echo   1^) Прогнать тесты:              deploy.bat test
echo   2^) Активировать окружение:       poem_sim\.venv\Scripts\activate
echo   3^) Продактив: PostgreSQL + schema.sql, POEM_ADMIN_TOKEN, HTTP-обвязка - guide_server.md S4.3-S5.2
echo   4^) Воркер генерации ^(опция^):    отдельное venv + CUDA PyTorch - guide_server.md S4.5
exit /b 0

:run_tests
echo -^> Прогон автотестов (pytest)
"%VENV_PY%" -m pytest -q
set "TEST_RC=%errorlevel%"
popd
exit /b %TEST_RC%

rem ------------------------------ подпрограммы --------------------------------
:ok
set /a PASS_CNT+=1
echo   [PASS] %~1 - %~2
goto :eof

:warn
set /a WARN_CNT+=1
echo   [WARN] %~1 - %~2
goto :eof

:fail
set /a FAIL_CNT+=1
echo   [FAIL] %~1 - %~2
goto :eof

:hdr
echo.
echo == %~1 ==
goto :eof

:show_help
echo Использование: deploy.bat [check^|deploy^|test^|help] [/profile:min^|recommended^|prod] [/dir:^<путь^>] [/python:^<интерпретатор^>] [/skip-gpu] [/force]
echo   check  - только чек-лист аппаратных и программных требований (по умолчанию)
echo   deploy - чек-лист + развёртывание: venv, pip install -e ".[test]", проверка импорта и chronology.toml
echo   test   - развернуть и прогнать pytest (172 автотеста Rules Rev 3)
echo   Требования - guide_server.md S2 (аппаратные) и S3 (программные).
exit /b 0
