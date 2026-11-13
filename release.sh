#!/bin/bash
# Ship a new version to everyone running Ember: bash release.sh 1.7
# Bumps VERSION, builds and checks the app, commits, tags, pushes, and publishes a GitHub
# Release with the zip. Ember checks that release every 6 hours and offers the update in Settings.
set -e
cd "$(dirname "$0")"

V="$1"
if [[ ! "$V" =~ ^[0-9]+(\.[0-9]+)+$ ]]; then
    echo "Usage: bash release.sh 1.7"
    exit 1
fi
if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "Commit your changes first, so the release matches what's on GitHub."
    exit 1
fi
if [ "$(git branch --show-current)" != "main" ]; then
    echo "Release from main."
    exit 1
fi
if git rev-parse "v$V" >/dev/null 2>&1; then
    echo "v$V already exists."
    exit 1
fi
gh auth status >/dev/null 2>&1 || { echo "Sign in to GitHub first: gh auth login"; exit 1; }

PREV=$(git describe --tags --abbrev=0 2>/dev/null || true)
if [ -n "$PREV" ]; then
    NOTES=$(git log --pretty='- %s' "$PREV"..HEAD)
else
    NOTES="- First release with automatic updates"
fi

sed -i '' -E "s/^VERSION = \"[^\"]+\"/VERSION = \"$V\"/" ember.py
bash build.sh
codesign --verify --deep --strict dist/Ember.app

ZIP="dist/Ember-$V.zip"
ditto -c -k --keepParent dist/Ember.app "$ZIP"

# Notarise when the app is signed with a Developer ID and notarytool has a saved login called "ember"
# (xcrun notarytool store-credentials ember). Then Gatekeeper opens it without Open Anyway.
if codesign -dv dist/Ember.app 2>&1 | grep -q "Authority=Developer ID Application" \
    && xcrun notarytool history --keychain-profile ember >/dev/null 2>&1; then
    echo "Notarising…"
    xcrun notarytool submit "$ZIP" --keychain-profile ember --wait
    xcrun stapler staple dist/Ember.app
    rm "$ZIP"
    ditto -c -k --keepParent dist/Ember.app "$ZIP"
else
    echo "Not notarised (needs a Developer ID certificate and: xcrun notarytool store-credentials ember)."
fi

git commit -q -am "Release $V"
git tag "v$V"
git push -q origin main "v$V"
gh release create "v$V" "$ZIP" --title "Ember $V" --notes "$NOTES"
echo
echo "Released $V. Ember offers it in Settings within 6 hours, or straight away from Settings → Updates."
