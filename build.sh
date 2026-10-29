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

# Sign with your Apple Development certificate if there is one. A stable signature means
# the Keychain remembers "Always Allow" across rebuilds instead of asking every time.
IDENTITY=$(security find-identity -v -p codesigning | grep -m1 "Apple Development" | awk '{print $2}')
if [ -n "$IDENTITY" ]; then
    codesign --force --deep --sign "$IDENTITY" dist/Ember.app
    echo "Signed with $(security find-identity -v -p codesigning | grep -m1 "$IDENTITY" | cut -d'"' -f2)"
else
    echo "No Apple Development certificate found; the Keychain will ask again after each rebuild."
fi
echo
echo "Done. Drag dist/Ember.app into /Applications and open it."
