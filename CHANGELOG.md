# Changelog

What changed in each version of Ember. New changes go under Unreleased; `release.sh` turns that
section into the next version and uses it as the GitHub Release notes.

## Unreleased

- The big number stays one size. A half degree shows as a small ".5" beside it, under the °, so nothing jumps as you step.
- If Hive can't be reached when Ember starts, it tries again by itself (after 30 seconds, then longer) instead of showing the sign-in screen and asking for a password it already has.
- Messages on the sign-in screens are in capitals like every other label, and centred in their space.
- + and − (and the up and down arrow keys) now change the target by half a degree: 18, 18.5, 19.
- The chart marks where today starts, and its hover readout says "Yesterday" for yesterday's readings.
- New: "Open at login" in Settings (macOS 13 or later).
- Schedule shows the period carried over from the day before as its first row, so there is always a "Now" row.
- In Settings, GitHub moved up beside Updates, and Sign out and Quit come last.
- The last rounded corners are square: Back, Settings, Quit, the eye, and the schedule strip.
- Ember no longer signs in to Hive from scratch every hour. It now registers itself as a remembered device, as the Hive app does, so Hive accepts its session renewals. The pane no longer blinks to "Signing in to Hive again…" each hour.
- Accounts with SMS codes are no longer asked for a code again each time Ember has to sign in.
- "Updated 20s ago" in Settings now counts from the last time Hive was really read. Opening Ember reads Hive again if that was over 30 seconds ago, so the numbers are no longer up to 2 minutes behind.
- Ember no longer wakes the Mac 30 times a second while a long status line is waiting behind a closed popover.
- The error log stops growing past about half a megabyte.
- Update messages in Settings fit their row ("Failed. Tap to retry", "Move to Applications").
- Signing out clears the chart and the outside temperature.

## 1.7.2 · 2026-11-14

- Sign in and Verification match the black home screen: the Ember logo, rows with hairlines between them, and an outlined button. No more blurred glass background.
- Settings, Schedule and Zone use the same header and flat rows. Ticks are orange, except the green one on "Check for updates automatically", and the days in Schedule sit in one boxed row.
- Choosing "Passwords…" in the password field now opens your saved passwords. Sign in and Verification stay open while another app has focus.
- AutoFill knows the email, password and code fields, so it can offer a texted code from Messages.
- The email shows in capitals, like every other label.
- Settings ends with "Made by Nick" and a link to nickclement.com.
- Settings has a GitHub row, for the source and this changelog.
- Times under the chart are spread evenly, each showing the real time at its place.
- Hover over the chart to see the time and temperature of any reading.

## 1.7.1 · 2026-11-13

- The tick on "Check for updates automatically" is the heating orange.

## 1.7 · 2026-11-13

- Updates: Ember looks for a newer version every 6 hours and offers it in Settings → Updates. One tap downloads it, checks it comes from the same developer, installs it and restarts. You can turn the checks off.
- The big number is larger (127pt), and "Off" is bigger too.
- The number can no longer be clicked. "Set temperature" still lets you type one, now at the same size as the number and with its °.
- While heating, the readings on the right, the scale marker and Low and High above the chart are white.
- "Last 24 hours • Low • High" sits on one line above the chart, which moved up a little.
- When the thermostat or hub loses its connection, the status says "Thermostat not responding" instead of a general error.
- Hour labels under the chart stay inside its edges.
- Showing the password no longer narrows the password field.
- The README lists every server Ember connects to.

## 1.6 · 2026-11-12

- New look after Barbican signage and Braun / Teenage Engineering: matte black, labels in capitals, a big number with small rows beside it, and a straight 10° to 30° scale.
- The whole pane turns orange while the boiler is heating, using Hive's own heating status.
- New app icon, an orange "e".
- Hot water shows as a row when your account has a tank.
- Boost's label and timer ease inwards on hover or Tab focus.
- Ember signs in again by itself when Hive stops accepting its session (about once a day), instead of failing until a restart.
- Fixed stepping up slowly and then down quickly leaving the wrong temperature on screen and on Hive.

## 1.5 · 2026-11-09

- VoiceOver names every button, and the popover works from the keyboard: Tab between buttons, Space or Return to press, up and down arrows to change the temperature.
- Fixed refreshes failing after the first hour of a session.

## 1.4 · 2026-11-02

- Accounts with several thermostats can pick a zone in Settings.

## 1.3 · 2026-10-30

- Settings → Schedule shows your weekly heating schedule (read only).
- The outside temperature, from Hive's weather service.
- Small labels in sentence case.

## 1.2 · 2026-10-29

- First release: the temperature in your menu bar, +/- and typed targets, a one-hour boost, and Scheduled, Manual and Off modes.
- Settings shows the version.
- Built to run on macOS 11 and later.
