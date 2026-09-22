set -eo pipefail

export IDF_PATH="${IDF_PATH:-$HOME/esp/esp-idf-v5.5.5}"
export IDF_TOOLS_PATH="${IDF_TOOLS_PATH:-$HOME/.espressif}"
export IDF_PYTHON_ENV_PATH="$IDF_TOOLS_PATH/python_env/idf5.5_py3.12_env"

if [ "${1:-}" = setup ]; then
    if [ ! -d "$IDF_PATH" ]; then
        git clone --branch v5.5.5 --depth 1 --recursive --shallow-submodules \
            https://github.com/espressif/esp-idf.git "$IDF_PATH"
    fi
    if [ "$(git -C "$IDF_PATH" describe --tags --exact-match)" != v5.5.5 ]; then
        echo "slate.firmware: IDF_PATH must point to ESP-IDF v5.5.5" >&2
        exit 1
    fi
    if [ "$(uname -s)" = Darwin ]; then
        brew install libgcrypt glib pixman sdl2 libslirp
    fi
    uv run --no-project --python 3.12 "$IDF_PATH/tools/idf_tools.py" \
        install --targets esp32s3
    uv run --no-project --python 3.12 "$IDF_PATH/tools/idf_tools.py" \
        install qemu-xtensa
    if [ ! -x "$IDF_PYTHON_ENV_PATH/bin/python" ]; then
        uv venv --python 3.12 "$IDF_PYTHON_ENV_PATH"
    fi
    curl -fsSL https://dl.espressif.com/dl/esp-idf/espidf.constraints.v5.5.txt \
        -o "$IDF_TOOLS_PATH/espidf.constraints.v5.5.txt"
    uv pip install --python "$IDF_PYTHON_ENV_PATH/bin/python" \
        -r "$IDF_PATH/tools/requirements/requirements.core.txt" \
        -c "$IDF_TOOLS_PATH/espidf.constraints.v5.5.txt"
    printf '5.5' > "$IDF_PYTHON_ENV_PATH/idf_version.txt"
    echo "slate.firmware: ESP-IDF and ESP32-S3 QEMU are ready"
    exit 0
fi

if [ ! -x "$IDF_PYTHON_ENV_PATH/bin/python" ]; then
    echo "slate.firmware: run make firmware-setup first" >&2
    exit 1
fi

export PATH="$IDF_PYTHON_ENV_PATH/bin:$PATH"
source "$IDF_PATH/export.sh" > /dev/null
exec uv run --no-project --python "$IDF_PYTHON_ENV_PATH/bin/python" \
    "$IDF_PATH/tools/idf.py" -C firmware "$@"
