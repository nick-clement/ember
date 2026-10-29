#!/bin/bash
# Run this on your Mac. Builds "Ember.app" into ./dist
set -e
cd "$(dirname "$0")"
python3 -m venv .venv
source .venv/bin/activate
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
