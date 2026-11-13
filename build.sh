#!/bin/bash
# Run this on your Mac. Builds "Ember.app" into ./dist
set -e
cd "$(dirname "$0")"
# Build with python.org's Python: it runs on macOS 11 and later. Homebrew's Python is built
# for the Mac it's installed on, so an app built with it crashes on older macOS versions.
PYTHON=/Library/Frameworks/Python.framework/Versions/3.15/bin/python3.15
if [ ! -x "$PYTHON" ]; then
    echo "Install Python 3.15 from python.org first (the macOS installer). See README."
    exit 1
fi
"$PYTHON" -m venv .venv-build
source .venv-build/bin/activate
python -m pip install -q -r requirements.txt
rm -rf build dist
python setup.py py2app

# Sign with a Developer ID if there is one (needed for notarising, see release.sh), else with the
# Apple Development certificate. A stable signature means the Keychain remembers "Always Allow"
# across rebuilds, and Ember's updater only installs apps signed by the same team.
IDENTITY=$(security find-identity -v -p codesigning | grep -m1 "Developer ID Application" | awk '{print $2}')
SIGN_OPTS=(--options runtime --timestamp)  # hardened runtime and a timestamp: notarising needs both
if [ -z "$IDENTITY" ]; then
    IDENTITY=$(security find-identity -v -p codesigning | grep -m1 "Apple Development" | awk '{print $2}')
    SIGN_OPTS=()
fi
if [ -n "$IDENTITY" ]; then
    codesign --force --deep "${SIGN_OPTS[@]}" --sign "$IDENTITY" dist/Ember.app
    echo "Signed with $(security find-identity -v -p codesigning | grep -m1 "$IDENTITY" | cut -d'"' -f2)"
else
    echo "No Apple Development certificate found; the Keychain will ask again after each rebuild."
fi
echo
echo "Done. Drag dist/Ember.app into /Applications and open it."
