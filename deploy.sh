#!/usr/bin/env bash
# =============================================================================
# deploy.sh — скрипт развёртывания сервера Poem (poem_sim) для Linux
#
# Назначение:
#   1. Проверяет чек-лист аппаратных и программных требований
#      (см. guide_server.md, разделы 2 и 3).
#   2. Разворачивает сервер: venv + пакет poem-sim + конфигурация + тесты.
#
# Использование:
#   ./deploy.sh [команда]
#
# Команды:
#   check       только проверка чек-листа требований (по умолчанию)
#   deploy      чек-лист + развёртывание (venv, pip install -e ".[test]")
#   test        развёрнутым окружением прогнать pytest
#   help        краткая справка
#
# Режимы проверки требований (--profile):
#   min         минимальные требования (разработка/тесты, без генерации картинок)
#   recommended рекомендуемые (эксплуатация, несколько миров)
#   prod        продактив: рекомендуемые + PostgreSQL + GPU для воркера рендера
#
# Флаги:
#   --profile=<min|recommended|prod>   режим чек-листа (по умолчанию min)
#   --dir=<каталог>                    каталог poem_sim (по умолчанию рядом со скриптом)
#   --python=<интерпретатор>           явный интерпретатор Python (по умолчанию поиск 3.13..3.11)
#   --skip-gpu                         не требовать GPU (для профилей recommended/prod)
#   --force                            продолжать даже при FAIL-пунктах чек-листа
#
# Коды выхода:
#   0 — успех; 1 — есть невыполненные обязательные пункты (FAIL);
#   2 — ошибка использования/окружения (например, нет каталога poem_sim).
# =============================================================================

set -u

# ----------------------------- утилиты вывода --------------------------------
if [ -t 1 ] && command -v tput >/dev/null 2>&1 && [ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ]; then
    C_RED=$'\e[31m'; C_GRN=$'\e[32m'; C_YLW=$'\e[33m'; C_BLU=$'\e[36m'; C_BLD=$'\e[1m'; C_RST=$'\e[0m'
else
    C_RED=""; C_GRN=""; C_YLW=""; C_BLU=""; C_BLD=""; C_RST=""
fi

say()  { printf '%s\n' "$*"; }
hdr()  { printf '\n%s%s%s\n' "$C_BLD$C_BLU" "== $* ==" "$C_RST"; }

PASS_CNT=0; WARN_CNT=0; FAIL_CNT=0

# ok <пункт> <детали>
ok()   { PASS_CNT=$((PASS_CNT+1)); printf '  %s[PASS]%s %s — %s\n' "$C_GRN" "$C_RST" "$1" "${2:-}"; }
# warn <пункт> <детали>  — необязательное требование не выполнено
warn() { WARN_CNT=$((WARN_CNT+1)); printf '  %s[WARN]%s %s — %s\n' "$C_YLW" "$C_RST" "$1" "${2:-}"; }
# fail <пункт> <детали>  — обязательное требование не выполнено
fail() { FAIL_CNT=$((FAIL_CNT+1)); printf '  %s[FAIL]%s %s — %s\n' "$C_RED" "$C_RST" "$1" "${2:-}"; }

usage() { sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; }

# ------------------------------- аргументы -----------------------------------
PROFILE="min"
CMD="check"
SIM_DIR=""
PY_CANDIDATE=""
SKIP_GPU=0
FORCE=0

for a in "$@"; do
    case "$a" in
        check|deploy|test|help) CMD="$a" ;;
        --profile=*) PROFILE="${a#*=}" ;;
        --dir=*)     SIM_DIR="${a#*=}" ;;
        --python=*)  PY_CANDIDATE="${a#*=}" ;;
        --skip-gpu)  SKIP_GPU=1 ;;
        --force)     FORCE=1 ;;
        -h|--help)   CMD="help" ;;
        *) say "Неизвестный аргумент: $a"; CMD="help"; exit 2 ;;
    esac
done

case "$PROFILE" in min|recommended|prod) ;; *) say "Неверный --profile: $PROFILE (min|recommended|prod)"; exit 2 ;; esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -z "$SIM_DIR" ] && SIM_DIR="$SCRIPT_DIR/poem_sim"

if [ ! -d "$SIM_DIR" ]; then
    say "${C_RED}Каталог poem_sim не найден: $SIM_DIR${C_RST} (укажите --dir=<путь>)"
    exit 2
fi

# --------------------------- вспомогательные функции ---------------------------

# версия ядра в виде числа для сравнений
kernel_version_num() { uname -r | awk -F. '{printf "%d%03d%03d", $1, $2, $3}'; }

# свободная RAM в МБ (MemAvailable из /proc/meminfo)
mem_available_mb() { awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo 2>/dev/null; }

# свободно на разделе в ГБ
disk_free_gb() { df -BG --output=avail "$1" 2>/dev/null | tail -1 | tr -dc '0-9'; }

cpu_count() { nproc 2>/dev/null || grep -c ^processor /proc/cpuinfo; }

find_python() {
    if [ -n "$PY_CANDIDATE" ]; then printf '%s' "$PY_CANDIDATE"; return 0; fi
    for p in python3.13 python3.12 python3.11 python3 python; do
        if command -v "$p" >/dev/null 2>&1; then printf '%s' "$p"; return 0; fi
    done
    return 1
}

gpu_present() {
    command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1 && return 0
    # AMD ROCm
    [ -e /dev/kfd ] && command -v rocm-smi >/dev/null 2>&1 && return 0
    lspci 2>/dev/null | grep -Eiq 'NVIDIA|AMD/ATI.*Graphics' && return 0
    return 1
}

# ------------------------------ чек-лист ---------------------------------------
run_checklist() {
    local need_cpu=2 need_ram_min_mb=4096 need_disk_gb=10
    case "$PROFILE" in
        min)         need_cpu=2; need_ram_min_mb=4096;  need_disk_gb=10 ;;
        recommended) need_cpu=4; need_ram_min_mb=16384; need_disk_gb=50 ;;
        prod)        need_cpu=4; need_ram_min_mb=16384; need_disk_gb=50 ;;
    esac

    hdr "Чек-лист требований сервера Poem (профиль: $PROFILE)"
    say "  Справочник требований: guide_server.md §2 (аппаратные), §3 (программные)"

    # ---------- Аппаратные требования (guide_server.md §2) ----------
    hdr "Аппаратные требования"

    local arch cpu
    arch="$(uname -m)"
    case "$arch" in x86_64|amd64|arm64|aarch64) ok "Архитектура CPU" "$arch (x86-64/ARM64)" ;;
                   *) warn "Архитектура CPU" "$arch — не проверена (требуются x86-64 или ARM64)" ;; esac

    cpu="$(cpu_count)"
    if [ "${cpu:-0}" -ge "$need_cpu" ]; then ok "Ядер CPU" "$cpu (требуется >= $need_cpu)"; else fail "Ядер CPU" "$cpu (требуется >= $need_cpu)"; fi

    local ram
    ram="$(mem_available_mb)"
    if [ "${ram:-0}" -ge "$need_ram_min_mb" ]; then ok "Свободная RAM" "${ram} МБ (требуется >= ${need_ram_min_mb} МБ)"
    else fail "Свободная RAM" "${ram:-?} МБ (требуется >= ${need_ram_min_mb} МБ)"; fi

    local disk
    disk="$(disk_free_gb "$SIM_DIR")"
    if [ "${disk:-0}" -ge "$need_disk_gb" ]; then ok "Свободно на диске" "${disk} ГБ (требуется >= ${need_disk_gb} ГБ + ~15 ГБ на веса моделей)"
    else fail "Свободно на диске" "(не удалось определить; требуется >= ${need_disk_gb} ГБ)"; fi

    if [ "$SKIP_GPU" = "1" ]; then
        warn "GPU" "проверка пропущена (--skip-gpu); генерация иллюстраций будет недоступна"
    elif gpu_present; then
        ok "GPU" "видеокарта обнаружена (nvidia-smi/lspci) — доступен воркер генерации изображений"
    else
        case "$PROFILE" in
            min) warn "GPU" "не обнаружен — допустимо: сервер работает без генерации картинок (задачи остаются в queued)" ;;
            *)   warn "GPU" "не обнаружен — иллюстрации хроники генерироваться не будут (для профиля $PROFILE рекомендуется NVIDIA 12+ ГБ VRAM)" ;;
        esac
    fi

    # ---------- Программные требования (guide_server.md §3) ----------
    hdr "Программные требования"

    local PY
    if PY="$(find_python)"; then
        local pyver pymaj pymin
        pyver="$("$PY" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])' 2>/dev/null || echo 0)"
        pymaj="$("$PY" -c 'import sys;print(sys.version_info[0])' 2>/dev/null || echo 0)"
        pymin="$("$PY" -c 'import sys;print(sys.version_info[1])' 2>/dev/null || echo 0)"
        if [ "$pymaj" -gt 3 ] || { [ "$pymaj" -eq 3 ] && [ "$pymin" -ge 11 ]; }; then
            ok "Python" "$pyver ($PY) — требуется >= 3.11 (tomllib из stdlib)"
        else
            fail "Python" "$pyver — требуется >= 3.11 (используется tomllib)"
        fi
        if "$PY" -c 'import tomllib' 2>/dev/null; then ok "tomllib" "доступен"; else fail "tomllib" "отсутствует — обновите Python до >= 3.11"; fi
        if "$PY" -m venv --help >/dev/null 2>&1; then ok "venv" "модуль доступен"; else fail "venv" "нет python3-venv (Debian/Ubuntu: apt install python3-venv)"; fi
        if "$PY" -m pip --version >/dev/null 2>&1; then ok "pip" "$("$PY" -m pip --version | cut -d' ' -f1-2)"; else fail "pip" "не найден (apt install python3-pip)"; fi
    else
        fail "Python" "интерпретатор не найден — установите Python >= 3.11"
    fi

    if command -v git >/dev/null 2>&1; then ok "git" "$(git --version | head -1)"; else warn "git" "не найден — нужен для клонирования репозитория"; fi

    # setuptools >= 68 (build-system в pyproject.toml)
    if [ -n "${PY:-}" ] && "$PY" -c 'import setuptools' 2>/dev/null; then
        local stver; stver="$("$PY" -c 'import setuptools;print(setuptools.__version__)')"
        local stmaj="${stver%%.*}"
        if [ "${stmaj:-0}" -ge 68 ]; then ok "setuptools" "$stver (>= 68)"; else warn "setuptools" "$stver < 68 — pip подтянет совместимую версию при сборке"; fi
    else
        warn "setuptools" "не найден в системном Python — будет установлен в venv автоматически"
    fi

    # PostgreSQL — обязателен только для профиля prod (схема schema.sql, UUID/JSONB)
    if command -v psql >/dev/null 2>&1; then
        ok "PostgreSQL" "клиент $(psql --version | head -1 | awk '{print $3}') — схема schema.sql требует сервер 14+"
    elif [ "$PROFILE" = "prod" ]; then
        fail "PostgreSQL" "не найден — продактив-развёртывание требует PostgreSQL 14+ (createdb/psql для schema.sql)"
    else
        warn "PostgreSQL" "не найден — не требуется: ядро работает on in-memory LawStore (нужен только профилю prod)"
    fi

    # ОС
    if [ "$(uname)" = "Linux" ]; then ok "ОС" "Linux $(uname -r) — рекомендуемая платформа сервера"; else warn "ОС" "$(uname) — рекомендуется Linux"; fi

    # Структура проекта
    hdr "Структура проекта"
    for f in pyproject.toml schema.sql config/chronology.toml pytest.ini; do
        if [ -f "$SIM_DIR/$f" ]; then ok "Файл" "$SIM_DIR/$f"; else fail "Файл" "(отсутствует $f)"; fi
    done
    if [ -d "$SIM_DIR/poem" ] && [ -d "$SIM_DIR/tests" ]; then ok "Каталоги" "poem/ и tests/ на месте"; else fail "Каталоги" "(нет poem/ или tests/ — это не каталог проекта?)"; fi

    # Итог чек-листа
    hdr "Итог чек-листа"
    printf '  %sPASS: %d%s   %sWARN: %d%s   %sFAIL: %d%s\n' \
        "$C_GRN" "$PASS_CNT" "$C_RST" "$C_YLW" "$WARN_CNT" "$C_RST" "$C_RED" "$FAIL_CNT" "$C_RST"
    if [ "$FAIL_CNT" -gt 0 ]; then
        say "  ${C_RED}Есть невыполненные обязательные требования — устраните FAIL-пункты${C_RST}"
        return 1
    fi
    say "  ${C_GRN}Обязательные требования выполнены.${C_RST}${C_YLW} WARN-пункты — опциональные слои (GPU, PostgreSQL), см. guide_server.md §2–3.${C_RST}"
    return 0
}

# ------------------------------ развёртывание ----------------------------------
run_deploy() {
    hdr "Развёртывание сервера Poem"
    local PY; PY="$(find_python)" || { say "${C_RED}Python не найден${C_RST}"; exit 1; }
    cd "$SIM_DIR" || exit 2

    say "-> Изолированное окружение .venv"
    "$PY" -m venv .venv || { say "${C_RED}Не удалось создать venv${C_RST}"; exit 1; }
    # shellcheck disable=SC1091
    . .venv/bin/activate

    say "-> pip install --upgrade pip"
    python -m pip install --upgrade pip

    say "-> pip install -e \".[test]\"  (пакет poem-sim + pytest)"
    pip install -e ".[test]" || { say "${C_RED}Ошибка установки пакета${C_RST}"; exit 1; }

    say "-> Проверка установки: импорт пакета и загрузка chronology.toml"
    python -c "import poem; print('   import poem: ok')" || exit 1
    python -c "from poem.config import load_chronology_config; load_chronology_config('config/chronology.toml'); print('   chronology.toml: ok')" || exit 1

    say ""
    say "${C_GRN}Развёртывание завершено.$C_RST Дальнейшие шаги:"
    say "  1) Прогнать тесты:              ./deploy.sh test"
    say "  2) Продактив: PostgreSQL + schema.sql, POEM_ADMIN_TOKEN, HTTP-обвязка — guide_server.md §4.3–§5.2"
    say "  3) Воркер генерации (опция):    отдельное venv + CUDA PyTorch — guide_server.md §4.5"
}

run_test() {
    hdr "Прогон автотестов (pytest)"
    cd "$SIM_DIR" || exit 2
    if [ -x .venv/bin/python ]; then
        # shellcheck disable=SC1091
        . .venv/bin/activate
    fi
    python -m pytest -q
}

# -------------------------------- main -----------------------------------------
case "$CMD" in
    help) usage ;;
    check) run_checklist; rc=$?; [ $rc -ne 0 ] && [ "$FORCE" = "1" ] && exit 0; exit $rc ;;
    deploy)
        run_checklist; rc=$?
        if [ $rc -ne 0 ] && [ "$FORCE" != "1" ]; then
            say ""; say "${C_RED}Развёртывание прервано: чек-лист не пройден (используйте --force для принудительного запуска).${C_RST}"
            exit 1
        fi
        run_deploy ;;
    test) run_test ;;
esac
