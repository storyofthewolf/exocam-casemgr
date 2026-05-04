#!/bin/bash
# Run all *_build.sh scripts in the given directory (default: scripts/).
#
# Usage:
#   bash run_builds.sh [scripts_dir]
#
# Each script is run sequentially. A failed build is reported but does not
# stop the remaining builds. Logs are written by each build script itself.

SCRIPTS_DIR=${1:-scripts}

if [ ! -d "$SCRIPTS_DIR" ]; then
    echo "ERROR: directory not found: $SCRIPTS_DIR"
    exit 1
fi

shopt -s nullglob
build_scripts=("$SCRIPTS_DIR"/*_build.sh)

if [ ${#build_scripts[@]} -eq 0 ]; then
    echo "No *_build.sh scripts found in $SCRIPTS_DIR"
    exit 1
fi

echo "Found ${#build_scripts[@]} build script(s) in $SCRIPTS_DIR"
echo ""

passed=0
failed=0
failed_cases=()

for script in "${build_scripts[@]}"; do
    case_name=$(basename "$script" _build.sh)
    echo "======================================================"
    echo "Building: $case_name"
    echo "======================================================"
    bash "$script"
    if [ $? -eq 0 ]; then
        echo "OK: $case_name"
        ((passed++))
    else
        echo "FAILED: $case_name"
        ((failed++))
        failed_cases+=("$case_name")
    fi
    echo ""
done

echo "======================================================"
echo "Summary: $passed succeeded, $failed failed"
if [ ${#failed_cases[@]} -gt 0 ]; then
    echo "Failed cases:"
    for c in "${failed_cases[@]}"; do
        echo "  - $c"
    done
fi
echo "======================================================"
