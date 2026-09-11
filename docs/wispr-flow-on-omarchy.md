# Wispr Flow on Omarchy

How to install Wispr Flow on Omarchy, and the four fixes it needs before
push-to-talk, the shortcuts, the Flow bar and browser sign-in all work. Every
step here was carried out and verified on Omarchy 4.0.3 (Arch, Hyprland 0.56.2,
Wayland) with Wispr Flow 1.6.7 from AUR package `wispr-flow-appimage` 1.0.3.

Wispr Flow has no official Linux build. This guide uses the unofficial port,
[wispr-flow-linux/wispr-flow-linux](https://github.com/wispr-flow-linux/wispr-flow-linux),
which runs Wispr's Windows Electron build with a clean-room helper for
keystroke injection and push-to-talk. Issue numbers below refer to that
repository.

## Install

```bash
yay -S wispr-flow-appimage
```

The package extracts the release AppImage to `/opt/wispr-flow-appimage`, so no
FUSE is needed at runtime, and installs the launcher `wispr-flow` and the
desktop entry `wispr-flow-appimage.desktop`. Its runtime dependencies (`gtk3`,
`nss`, `alsa-lib`, `wl-clipboard`) are already on Omarchy. `yay -Syu` keeps it
up to date.

Launch it with `wispr-flow` and sign in. Dictation, pasting into the focused
app and accessibility work at this point. Push-to-talk does not, until Fix 1.

The port has a built-in diagnostic:

```bash
wispr-flow --doctor
```

After the fixes below, the one remaining failure is expected:

```text
[FAIL] chrome-sandbox: perms=755 owner=root (need 4755 root)
```

The launcher runs Electron with `--no-sandbox`, so the chrome-sandbox helper is
never used and its permissions do not matter. Leave it alone. The warning
`input group: current user is NOT a member` is also fine once Fix 1 is in place.

## Fix 1: push-to-talk cannot read the keyboard

**Symptom.** In onboarding, "Test the keyboard shortcut" never turns the key
purple. `wispr-flow --doctor` reports `/dev/input: none of N event device(s)
readable`, and `~/.cache/wispr-flow/launcher.log` contains
`evdev capture: no readable keyboard under /dev/input`.

**Why.** Wayland has no global-hotkey API, so the port's helper reads raw key
events from `/dev/input/event*`, which only root can read by default. The
port's `.deb` and `.rpm` packages install a udev rule for this; the AUR package
does not.

**Fix.** Save the port's own rule as
`/etc/udev/rules.d/70-wispr-flow-uinput.rules`:

```text
# Wispr Flow (wispr-flow-linux): grant the active-session user the input access the helper needs.
#  - write /dev/uinput        - keystroke injection (PasteText/SimulateKeyPress)
#  - read  /dev/input/event*  - global key monitor for push-to-talk and the in-app shortcut recorder
# TAG+="uaccess" scopes the grant to the active logind session; the input group + 0660 is the
# cross-distro fallback.
KERNEL=="uinput", SUBSYSTEM=="misc", OPTIONS+="static_node=uinput", TAG+="uaccess", GROUP="input", MODE="0660"
SUBSYSTEM=="input", KERNEL=="event*", TAG+="uaccess", GROUP="input", MODE="0660"
```

Then, also as root, reload the rules and apply them to the devices that already
exist:

```bash
udevadm control --reload-rules
udevadm trigger --subsystem-match=misc --sysname-match=uinput
udevadm trigger --subsystem-match=input
```

Writing to `/etc` and running `udevadm` are the only steps in this guide that
need root. The bar plugin itself never does.

As an alternative, `wispr-flow --install-udev-rules` writes the same rule under
`/usr/lib/udev/rules.d/` after a polkit prompt. Either location works; pick one.

`TAG+="uaccess"` makes logind grant access to whoever holds the active session,
and only while they hold it. So you do **not** need to join the `input` group,
which would let every process you run read every keystroke at all times,
whether or not you are at the machine.

The rule takes effect for the running session as soon as the trigger runs.
Restart Wispr Flow so its helper finds the keyboards again.

**Verify.**

```bash
wispr-flow --doctor | grep /dev/input
grep -a 'evdev capture' ~/.cache/wispr-flow/launcher.log | tail -1
```

The first prints `[PASS] /dev/input: 22/22 event device(s) readable
(push-to-talk available)` (your device count will differ); the second prints
`evdev capture: watching /dev/input/eventN`.

On a machine with Steam installed, `steam-devices` already makes `/dev/uinput`
writable. The rule above covers `/dev/uinput` too, for machines without it.

## Fix 2: the default shortcuts cannot be pressed

**Symptom.** The push-to-talk shortcut is labelled "Ctrl + Win", but pressing it
does nothing.

**Why.** Wispr seeds a new Linux profile with its macOS chords, and the key
they need is stored as `-1`, which matches no key on Linux. Upstream tracks
this as issues #33 and #46, and the open PR #55 fixes the seeding for future
builds.

**Fix.** In onboarding, or later in **Settings → Shortcuts**, choose **Edit
shortcut** for push-to-talk and record a chord you can press, for example
Ctrl+Shift. Do the same for **lens**, **paste last text** and **copy last
text**, which otherwise stay unpressable.

**Verify.** No shortcut in `~/.config/Wispr Flow/config.json` should still
contain `-1`:

```bash
jq -c '.prefs.user.shortcuts' ~/.config/Wispr\ Flow/config.json
```

A recorded chord appears as key codes, such as `"160+162":"ptt"`. An entry like
`"-1+162":"lens"` has not been re-recorded yet.

## Fix 3: the Flow bar is an empty framed box that eats clicks

**Symptom.** Wispr's floating Flow bar, a transparent 440×320 window titled
"Flow Status Indicator", gets Hyprland's border and shadow, and its invisible
surface swallows clicks along the bottom of the screen (issue #44).

**Fix.** Use the [bar plugin](../README.md) as the dictation indicator instead,
and put the Flow bar away:

1. In Wispr Flow, open **Settings → System** and turn off **Show Flow Bar at
   all times**. Wispr stores this as `hideFlowBarPermanently` in
   `~/.config/Wispr Flow/config.json`.
2. Wispr still maps the status window. Park it in a hidden special workspace,
   and float Wispr's settings window (the Hub) while you are at it, with these
   rules in `~/.config/hypr/hyprland.lua`:

```lua
-- Wispr Flow: park the transparent status window; the Omarchy bar widget shows dictation state.
o.window({ class = "^wispr-flow$", initial_title = "^Flow Status Indicator$" }, {
  float = true,
  no_initial_focus = true,
  no_focus = true,
  focus_on_activate = false,
  no_anim = true,
  workspace = "special:wispr silent",
})

-- The Hub (settings / onboarding) is an ordinary app window on macOS; float and centre it.
o.window({ class = "^wispr-flow$", initial_title = "^Hub$" }, {
  float = true,
  center = true,
})
```

**Verify.**

```bash
hyprctl reload
hyprctl configerrors
```

`configerrors` prints nothing when the rules are valid. Rules apply to new
windows only, so restart Wispr Flow once; the status window then no longer
appears on your workspace.

## Fix 4: signing in or paying in the browser does not return to the app

**Symptom.** Sign-in and billing pages in the browser end on a `wispr-flow://`
link that opens nothing. The launcher log shows
`Protocol registration success: false`.

**Why.** The AUR desktop entry declares no URL scheme, and Electron cannot
register one itself on Linux, so nothing handles `wispr-flow://`.

**Fix.** Make a user-level copy of the desktop entry that declares the scheme,
and make it the handler:

```bash
cp /usr/share/applications/wispr-flow-appimage.desktop ~/.local/share/applications/
echo 'MimeType=x-scheme-handler/wispr-flow;' >> ~/.local/share/applications/wispr-flow-appimage.desktop
update-desktop-database ~/.local/share/applications
xdg-mime default wispr-flow-appimage.desktop x-scheme-handler/wispr-flow
```

The copy in `~/.local/share/applications` takes precedence over the packaged
one and survives package updates.

**Verify.**

```bash
xdg-mime query default x-scheme-handler/wispr-flow
xdg-open 'wispr-flow://open'
```

The query prints `wispr-flow-appimage.desktop`. `xdg-open` starts a second
Wispr Flow, which hands the link to the running one and exits.

## Autostart

Add one line to `~/.config/hypr/autostart.lua`:

```lua
o.launch_on_start("wispr-flow")
```

Wispr Flow runs a single instance: a second launch passes control to the one
already running, so launching it again is harmless.

## Where things live

| What | Path |
|---|---|
| App | `/opt/wispr-flow-appimage` |
| Launcher | `/usr/bin/wispr-flow` |
| Input helper | `/opt/wispr-flow-appimage/usr/lib/wispr-flow/resources/Release/wispr-flow-linux-helper` |
| Launcher log (Electron output) | `~/.cache/wispr-flow/launcher.log` |
| Settings and preferences | `~/.config/Wispr Flow/config.json` |
| udev rule (Fix 1) | `/etc/udev/rules.d/70-wispr-flow-uinput.rules` |
| URL handler (Fix 4) | `~/.local/share/applications/wispr-flow-appimage.desktop` |

The bar plugin reads the launcher log to tell starting and processing apart;
see [How it works](../README.md#how-it-works).

## Remove

```bash
yay -R wispr-flow-appimage
rm ~/.local/share/applications/wispr-flow-appimage.desktop
update-desktop-database ~/.local/share/applications
rm -rf ~/.cache/wispr-flow ~/.config/Wispr\ Flow
```

Then, as root, delete `/etc/udev/rules.d/70-wispr-flow-uinput.rules` (or the
copy under `/usr/lib/udev/rules.d/` if you used `--install-udev-rules`) and run
`udevadm control --reload-rules`. Finally remove the two `o.window` rules from
`~/.config/hypr/hyprland.lua` and the `o.launch_on_start("wispr-flow")` line
from `~/.config/hypr/autostart.lua`.

Deleting `~/.config/Wispr Flow` removes your local settings and history; your
account stays with Wispr.

## Known port issues

- **#53**: after a dictation, a modifier key occasionally stays logically
  pressed on Wayland.
- **#44**: the Flow bar's transparent surface intercepts clicks, which is why
  Fix 3 parks it.
- The port packages Wispr's Windows build and has been held at Wispr 1.6.7
  since July; PR #55 unblocks fetching newer releases.
- Wispr Notetaker is Mac-only, so meeting recording is not available on Linux.
