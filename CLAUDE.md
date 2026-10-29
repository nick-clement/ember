# Ember

macOS menu bar app that controls Hive heating through the unofficial `pyhiveapi` library
(pip name `pyhive-integration`). Python, AppKit through PyObjC, `rumps` for the status item.

## Run and build
- `bash run.sh` makes `.venv`, installs dependencies and runs `ember.py`.
- `bash build.sh` builds `dist/Ember.app` with py2app. Settings are in `setup.py`.
- Saved login lives in the macOS Keychain under the service name `HiveMenuBar`. Keep that name so existing logins survive.

## Files
- `ember.py`: everything else. Design tokens at the top, custom views (`Hit`, `Box`, `FieldBox`, `HistoryChart`, `PopoverBackground`, `Panel`), then `PopoverUI` (panes: control, settings, signin, code), then `HiveApp`.
- `dial.py`: draws the dial (ticks plus target marker) with Pillow and tints the icon PNGs. Only a small patch is redrawn per frame, so it stays fast.
- `assets/icons/*.png`: white Lucide icon masters, rendered at stroke 1.25 from `assets/lucide-svg/`. `AppIcon.png/.icns` is the app icon.

## Design source
Figma file `5ZrtELYwIfj7wNS4GYssZG`: Scheduled control 3:814, One-hour heating boost 3:854, Settings and mode menu 3:895, Sign in 7:88. The popover is 320pt wide, graphite surfaces, SF Pro. Amber means heating, shading to red from 23° to 26°. The sign-in panes use a glass look (blurred backdrop plus tint).
Deliberate difference from Figma: the header says "Ember". The − and + buttons sit on one row with "Set temperature", as in the design. The status line has no dot (its colour already says heating or idle). The app icon is the thermometer, like the menu bar.

## Behaviour rules
- +/- taps update the screen at once and send one request to Hive after 0.8s of quiet (`SEND_DELAY`). Do not send one request per tap. Hive returns 403 if flooded.
- Warnings: line from 24°, stronger from 28°, typed values of 26° or more need Return twice. Boost never aims above 25° unless the target is already higher. Hive's own limits are 5° to 32°.
- No modal dialogs. Everything happens inside the popover.
- Temperatures round to 0.5°.
- The "Last 24 hours" chart reads `GET beekeeper-uk.hivehome.com/1.0/history/heating/{hiveID}` (not in pyhiveapi; 30-minute averages), at most every 15 minutes (`HISTORY_SECONDS`). The amber shading is where the schedule asked for more than the room had, worked out from the schedule, not Hive's own on/off record.

## PyObjC gotchas
- A Python method on an ObjC subclass must match the selector arity (underscores count arguments), or be decorated `@objc.python_method`. A method called `style` clashed once; helper methods on views use `@objc.python_method`.
- Update the UI from worker threads only through `AppHelper.callAfter`.
- pyhiveapi fetched its Cognito settings from sso.hivehome.com (with `verify=False`); that page went Access Denied on 2026-10-29. `ember.py` now patches `HiveApi.getLoginInfo` to return `HIVE_LOGIN_INFO`. If sign-in says the client wasn't found, those values changed.
- Sign-in errors are written with tracebacks to `~/Library/Logs/Ember.log`.
- pyhiveapi's sync client needs `threading.current_task`, `threading.sleep` and `threading.TimeoutError` stand-ins (patched near the top of `ember.py`). Without TimeoutError, any failed refresh shows "module 'threading' has no attribute 'TimeoutError'".
- Errors in the status line use `short_error` (it is only 240pt wide). Full tracebacks go to `~/Library/Logs/Ember.log`.

## Style for replies
Brief and plain. No em dashes. Explain new concepts in a line.
