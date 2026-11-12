# Ember

macOS menu bar app that controls Hive heating through the unofficial `pyhiveapi` library
(pip name `pyhive-integration`). Python, AppKit through PyObjC, `rumps` for the status item.

## Run and build
- `bash run.sh` makes `.venv`, installs dependencies and runs `ember.py`.
- `bash build.sh` builds `dist/Ember.app` with py2app. Settings are in `setup.py`.
- `build.sh` uses python.org's Python 3.15 in `.venv-build`, not Homebrew's (Homebrew builds need the build Mac's macOS version, so the app crashed on a friend's older Mac). tkinter is excluded in `setup.py`: its Tcl/Tk frameworks break py2app's signing.
- Saved login lives in the macOS Keychain under the service name `HiveMenuBar`. Keep that name so existing logins survive.

## Files
- `ember.py`: everything else. Design tokens at the top, custom views (`Label`, `Hit`, `Box`, `FieldBox`, `DayBar`, `ScaleBar`, `HistoryChart`, `PopoverBackground`, `Panel`), then `PopoverUI` (panes: control, settings, signin, code, schedule, zones), then `HiveApp`.
- `dial.py`: tints the icon PNGs with Pillow. Its round dial (`dial_png`) is no longer shown; the control pane uses `ScaleBar`, a straight scale.
- `design/test-screens.html`: the HTML test screens the 1.6 look was picked from (screen 4, with the caps of 5 and the numeral of 1).
- `assets/icons/*.png`: white Lucide icon masters, rendered at stroke 1.25 from `assets/lucide-svg/`. `assets/AppIcon.svg` is the app icon source (orange with a white "e"); `python assets/make_icon.py assets/AppIcon.svg assets/AppIcon.png` puts it on Apple's icon grid, then make `AppIcon.icns` from that with `iconutil`.

## Design source
Figma file `5ZrtELYwIfj7wNS4GYssZG`: Scheduled control 3:814, One-hour heating boost 3:854, Settings and mode menu 3:895, Sign in 7:88. The popover is 360pt wide (`POPOVER_W`; layouts were drawn at 320, so right-hand parts add `GROW` and full-width parts use `CONTENT_W`). Since 1.6 it no longer follows Figma's look: it is after Barbican signage and Braun / Teenage Engineering. Matte near-black, every label in SF Mono capitals (`CAPS`) with a little extra letter spacing (`MONO_TRACKING`), the target as a big SF Pro Heavy numeral on the left (`"heavy-display"`) with small rows to its right (label left, value right: Current, Outside, Mode or the boost countdown, Hot water), a straight 10° to 30° scale, and hairline rules between rows instead of buttons with fills. While heating (or boosting) the whole control pane turns orange (`ORANGE`, shading to `ORANGE_RED` for high targets) with black text and a white numeral; `PopoverUI._theme` holds both colour sets. Only the big numeral, the word "Heating" in the status line and the plain note under Boost ("Following your thermostat schedule"), the values in the small rows (Current, Outside with its icon, Mode or the boost countdown, Hot water), the scale marker, and the Low and High values above the chart are white on orange. The user chose the white small text knowing it is about 3.4:1; warnings and typing hints in that note stay black, and nothing else small goes white there. Amber means heating, shading to red from 23° to 26°. The sign-in panes use a glass look (blurred backdrop plus tint).
Deliberate difference from Figma: the header says "Ember". The − and + buttons sit on one row with "Set temperature", as in the design. The status line has no dot (its colour already says heating or idle). The menu bar icon is still the thermometer; the app icon is the "e".

## Behaviour rules
- +/- taps update the screen at once and send one request to Hive after 0.8s of quiet (`SEND_DELAY`). Do not send one request per tap. Hive returns 403 if flooded.
- Only one send runs at a time (`send_lock`) and it always takes the newest value; the waiting tap (`pending_target`) is kept until its send finishes, and each tap bumps `action_seq`, so a refresh can't put an older Hive value back on screen. Stepping slowly up then quickly down once left Hive and the screen on the wrong number.
- "Heating" (and the orange pane) is Hive's own boiler status, the thermostat's `props.working` (`boiler_working`), the same as the Hive app's "Heating to 18°". pyhiveapi's `getState` only compares room and target, which disagrees near the target. A tap doesn't change it; the boiler takes a while to react.
- Status line: "Idle · Scheduled tomorrow 05:45", "Idle · Scheduled now" when the slot already running asks for more than the room has, "Heating · Scheduled until 22:30".
- Hot water: shown as a row when the account has a tank (`session.deviceList["water_heater"]`, `hot_water_state`). Read only.
- Warnings: line from 24°, stronger from 28°, typed values of 26° or more need Return twice. Boost never aims above 25° unless the target is already higher. Hive's own limits are 5° to 32°.
- No modal dialogs. Everything happens inside the popover.
- Temperatures round to 0.5°.
- The Schedule pane (Settings → Schedule) is read only. It shows the weekly schedule pyhiveapi already loads (`product["state"]["schedule"]`). pyhiveapi can't write a schedule (its `setState` sends flat strings), so editing needs our own request.
- Zones: an account can have several thermostats (`session.deviceList["climate"]`, e.g. underfloor downstairs plus radiators upstairs). With more than one, Settings shows a Zone row, the header shows the zone name instead of "Home", and the choice (its `hiveID`) is kept in NSUserDefaults under `zone`. A +/- tap still waiting is sent to the zone it was made on (`pending_device`). Untested with a real two-zone account here; the developer's account has one zone.
- Outside temperature comes from Hive's weather service (`WEATHER_URL`, no sign-in needed) using the postcode on the Hive account, every 15 minutes (`WEATHER_SECONDS`). No location permission. The sun/moon icon choice uses rough UK sunrise/sunset times (`SUN_HOURS`).
- The "Last 24 hours" chart reads `GET beekeeper-uk.hivehome.com/1.0/history/heating/{hiveID}` (not in pyhiveapi; 30-minute averages), at most every 15 minutes (`HISTORY_SECONDS`). The amber shading is where the schedule asked for more than the room had, worked out from the schedule, not Hive's own on/off record.

## PyObjC gotchas
- A Python method on an ObjC subclass must match the selector arity (underscores count arguments), or be decorated `@objc.python_method`. A method called `style` clashed once; helper methods on views use `@objc.python_method`.
- Update the UI from worker threads only through `AppHelper.callAfter`.
- pyhiveapi fetched its Cognito settings from sso.hivehome.com (with `verify=False`); that page went Access Denied on 2026-10-29. `ember.py` now patches `HiveApi.getLoginInfo` to return `HIVE_LOGIN_INFO`. If sign-in says the client wasn't found, those values changed.
- Sign-in errors are written with tracebacks to `~/Library/Logs/Ember.log`.
- pyhiveapi's sync client needs `threading.current_task`, `threading.sleep` and `threading.TimeoutError` stand-ins (patched near the top of `ember.py`). Without TimeoutError, any failed refresh shows "module 'threading' has no attribute 'TimeoutError'".
- pyhiveapi's `refresh_token` sends `AuthParameters` as a tuple when no device key is saved, and Hive rejects it, so every refresh after the first hour failed. The wrapper in `keep_auth_errors` unwraps it.
- `log_error` replaces sign-in tokens (`eyJ…`) with `[token]`, because people send the log in.
- Errors in the status line use `short_error`. A status too long for the line scrolls to its end and back like a banner (`_set_status`, still with Reduce Motion, full text in the tooltip). Full tracebacks go to `~/Library/Logs/Ember.log`.
- Hive stops accepting the session's refresh token about once a day ("Invalid Refresh Token", botocore `NotAuthorizedException`). pyhiveapi's own re-login doesn't catch that, so Ember signs in again with the saved Keychain login (`is_session_expired`, `_reauth`, at most every 5 minutes) instead of failing every refresh until a restart.

## Accessibility
- `Hit` is a VoiceOver button. It is named by `ax_label`, or else by the text inside it, and `ax_selected` adds ", selected". Give every icon-only `Hit` an `ax_label`.
- Keyboard: Tab and Shift-Tab move between the visible buttons (`PopoverUI.focus_next`), Space or Return presses one, and up and down arrows change the target on the control pane. A `Hit` only takes focus while Tab is moving it (`Hit.tabbing`), so clicks never leave a focus ring.
- `TEXT_3` is the faintest text colour and must stay at 4.5:1 or more on every surface. On orange the faintest is `ON_ORANGE_2`, same rule, hover fills included.

## Copy
- Write copy in sentence case in the code. It shows in capitals because `attributed` upper-cases it when `CAPS` is on, and VoiceOver still reads the sentence-case words (`Label.plain`, `set_plain`). Each part after a · starts with a capital ("Heating · Scheduled until 22:30").

## Style for replies
Brief and plain. No em dashes. Explain new concepts in a line.
