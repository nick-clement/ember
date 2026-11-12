# Ember

A small macOS menu bar app for Hive heating. Check the temperature, change the target,
boost for an hour, switch modes, see your weekly schedule, the outside temperature and the last 24 hours, without opening the Hive app.

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
- Ember only talks to Hive's servers. For the outside temperature it sends the postcode on your Hive account to Hive's weather service, as the Hive app does. Nothing else is collected or sent anywhere.
- Errors are written to `~/Library/Logs/Ember.log` on your Mac. Sign-in tokens are replaced with `[token]` there, so the log is safe to share.
- No analytics, no update checks, no accounts of its own, and no AI at runtime. Ember doesn't listen for incoming connections, and it asks for no permissions (no location, microphone or files).

The only hosts it connects to:

| Host | What for |
| --- | --- |
| `cognito-idp.eu-west-1.amazonaws.com` | Hive sign-in (Amazon Cognito) |
| `beekeeper.hivehome.com`, `beekeeper-uk.hivehome.com` | Thermostat state, changes and the 24-hour history |
| `api.prod.bgchprod.info` | Hive's older API, used by pyhiveapi |
| `weather.prod.bgchprod.info` | Outside temperature for your Hive postcode |

To check for yourself, search `ember.py` for `https://`. You can also watch its traffic with Little Snitch or `nettop -p Ember`, or run it from source with `bash run.sh` instead of using a prebuilt app.

## Credits

Icons from [Lucide](https://lucide.dev) (ISC licence). Hive control through
[pyhiveapi](https://github.com/Pyhass/Pyhiveapi).

## Licence

MIT. See [LICENSE](LICENSE).
