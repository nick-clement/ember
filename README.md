# Ember

A small macOS menu bar app for Hive heating. Check the temperature, change the target,
boost for an hour, switch modes and see the last 24 hours, without opening the Hive app.

> Ember is not made by or affiliated with Hive or Centrica. It uses Hive's unofficial API
> (through [pyhiveapi](https://github.com/Pyhass/Pyhiveapi)), so it can stop working if Hive changes something.

## Run it

Needs macOS and Python 3.

```bash
bash run.sh
```

This sets up a virtual environment, installs the dependencies and starts Ember in the menu bar.

## Build the app

```bash
bash build.sh
```

This makes `dist/Ember.app` with py2app. It needs Python 3.15 from the python.org macOS installer:
Homebrew's Python only runs on the macOS version it was built on, so an app built with it crashes on
older Macs. The app runs on Apple silicon Macs with macOS 11 or later. The build is not notarized, so on another Mac it needs
approving once in System Settings → Privacy & Security → Open Anyway.

## Privacy

- Your Hive login is stored in your Mac's Keychain and is only sent to Hive (via Amazon Cognito, which Hive uses for sign-in).
- Ember only talks to Hive's servers. Nothing else is collected or sent anywhere.
- Errors are written to `~/Library/Logs/Ember.log` on your Mac.

## Credits

Icons from [Lucide](https://lucide.dev) (ISC licence). Hive control through
[pyhiveapi](https://github.com/Pyhass/Pyhiveapi).

## Licence

MIT. See [LICENSE](LICENSE).
