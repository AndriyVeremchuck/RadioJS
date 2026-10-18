#!/usr/bin/env bash
# Компіляція radio_manager.py у нативний виконуваний файл ELF (Embedded Python + GCC)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "1/3 Перевірка синтаксису radio_manager.py..."
/usr/bin/python3 -m py_compile radio_manager.py

echo "2/3 Генерація та компіляція нативного бінарного файлу radio_manager..."
/usr/bin/python3 - << 'EOF'
with open("radio_manager.py", "rb") as f:
    src_bytes = f.read()

hex_array = ", ".join(f"0x{b:02x}" for b in src_bytes)

c_code = f"""
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdlib.h>

static const unsigned char PY_MAIN_SOURCE[] = {{
    {hex_array}, 0x00
}};

int main(int argc, char *argv[]) {{
    setenv("QT_MEDIA_BACKEND", "ffmpeg", 0);

    PyStatus status;
    PyConfig config;
    PyConfig_InitPythonConfig(&config);
    config.parse_argv = 0;

    status = PyConfig_SetBytesArgv(&config, argc, argv);
    if (PyStatus_Exception(status)) {{
        PyConfig_Clear(&config);
        Py_ExitStatusException(status);
    }}

    status = Py_InitializeFromConfig(&config);
    if (PyStatus_Exception(status)) {{
        PyConfig_Clear(&config);
        Py_ExitStatusException(status);
    }}
    PyConfig_Clear(&config);

    int ret = PyRun_SimpleString((const char *)PY_MAIN_SOURCE);
    if (Py_FinalizeEx() < 0) {{
        ret = 120;
    }}
    return ret;
}}
"""

with open("radio_manager_embed.c", "w", encoding="utf-8") as out:
    out.write(c_code)
EOF

gcc -O2 $(/usr/bin/python3-config --cflags) radio_manager_embed.c -o radio_manager $(/usr/bin/python3-config --ldflags --embed)
rm -f radio_manager_embed.c
strip radio_manager

if [[ -d "/home/dusha/RadioJS" && "$SCRIPT_DIR" != "/home/dusha/RadioJS" ]]; then
    echo "3/3 Копіювання бінарного файлу в /home/dusha/RadioJS/radio_manager..."
    cp -f radio_manager /home/dusha/RadioJS/radio_manager
fi

echo "Готово! Розмір файлу: $(du -h radio_manager | cut -f1)"
