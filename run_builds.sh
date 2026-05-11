#!/bin/bash
# Run *_build.sh scripts in the given directory (default: build_scripts/).
#
# Usage:
#   bash run_builds.sh [--prefix PREFIX] [--yes] [scripts_dir]
#
# Options:
#   --prefix PREFIX   Only run scripts matching PREFIX*_build.sh
#   --yes             Skip confirmation prompt (useful for automation)
#   scripts_dir       Directory containing build scripts (default: build_scripts)
#
# Output from each build script is captured to logs/<case_name>.log.

SCRIPTS_DIR="build_scripts"
PREFIX=""
AUTO_YES=false

# --- Argument parsing ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        --prefix)
            PREFIX="$2"
            shift 2
            ;;
        --yes|-y)
            AUTO_YES=true
            shift
            ;;
        -*)
            echo "ERROR: unknown option: $1"
            exit 1
            ;;
        *)
            SCRIPTS_DIR="$1"
            shift
            ;;
    esac
done

if [ ! -d "$SCRIPTS_DIR" ]; then
    echo "ERROR: directory not found: $SCRIPTS_DIR"
    exit 1
fi

LOGS_DIR="$SCRIPTS_DIR/logs"
mkdir -p "$LOGS_DIR"

# --- Collect matching scripts ---
shopt -s nullglob
build_scripts=("$SCRIPTS_DIR"/${PREFIX}*_build.sh)

if [ ${#build_scripts[@]} -eq 0 ]; then
    echo "No scripts matching '${PREFIX}*_build.sh' found in $SCRIPTS_DIR"
    exit 1
fi

# --- Confirmation ---
echo "The following ${#build_scripts[@]} build script(s) will be run:"
for script in "${build_scripts[@]}"; do
    echo "  $(basename "$script")"
done
echo ""

if [ "$AUTO_YES" = false ]; then
    read -p "Proceed? [y/N] " answer
    case "$answer" in
        [yY][eE][sS]|[yY]) ;;
        *)
            echo "Aborted."
            exit 0
            ;;
    esac
fi

# --- Run loop ---
passed=0
failed=0
failed_cases=()

for script in "${build_scripts[@]}"; do
    case_name=$(basename "$script" _build.sh)
    log_file="$LOGS_DIR/${case_name}.log"

    echo -n "Building: $case_name ... "
    bash "$script" > "$log_file" 2>&1

    if [ $? -eq 0 ]; then
        echo "OK"
        ((passed++))
    else
        echo "FAILED  (see $log_file)"
        ((failed++))
        failed_cases+=("$case_name")
    fi
done

echo ""
echo "======================================================"
echo "Summary: $passed succeeded, $failed failed"
if [ ${#failed_cases[@]} -gt 0 ]; then
    echo "Failed cases:"
    for c in "${failed_cases[@]}"; do
        echo "  - $c  →  $LOGS_DIR/${c}.log"
    done
fi
echo "======================================================"
