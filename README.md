# Thor Gamepad Bridge

I wanted to record a demonstration of [NetHack-Android-DS](https://github.com/fancypantalons/NetHack-Android-DS) on my Ayn Thor. I used `scrcpy` to get access to the dual displays, which worked great, but I wanted some way to demonstrate the gamepad functionality.

So I did the clearly obvious thing: me and the robot hacked together this little script to connect to a Thor via ADB, read the raw input device events from the gamepad, and emit them on a virtual gamepad on my Linux machine that mirrors the Thor's controller. With that working, I could use the [input-overlay plugin](https://github.com/univrsal/input-overlay) with OBS studio to render a virtual gamepad alongside the video. Neat.

It's silly and janky, but if you ever wanted to use your Thor as a high latency USB gamepad, congratulations, your dream has come true.

## How it Works

The script uses ADB to run `getevent` on the Android device. It then parses that stream and uses the `python-evdev` library to inject those same events into a virtual `uinput` device on your Linux host.

## Requirements

*   **ADB:** You'll need `adb` installed and your device connected with USB debugging enabled.
*   **Python 3:** Obviously.
*   **evdev:** `pip install evdev`
*   **Permissions:** You need write access to `/dev/uinput`. Usually, this means being in the `input` group or running with `sudo`.

## Usage

Just run the script. It'll try to auto-detect your gamepad.

```bash
python3 thor_gamepad_bridge.py
```

If you have multiple controllers or devices connected, you can list them:

```bash
python3 thor_gamepad_bridge.py --list
```

This probes the Android device and shows you every input path it finds, along with a "GAMEPAD" tag if the script thinks it's found a controller.

And then specify the one you want:

```bash
python3 thor_gamepad_bridge.py --device /dev/input/event9
```

Passing an explicit device path lets you bypass the auto-detection and the selection menu entirely—handy if you've already figured out which event path your controller is sitting on.

If you have multiple Android devices, you can pick one by serial:

```bash
python3 thor_gamepad_bridge.py --serial 12345678
```

## Installation

If you don't want to clutter your global Python environment, you can run this in a virtual environment:

```bash
python3 -m venv venv
source venv/bin/activate
pip install .
```

If you want to be fancy and install it as a local tool globally:

```bash
pip install .
```

Then you can just run `thor-gamepad-bridge` from anywhere.

## Limitations

*   **Linux Only:** Uses `uinput` and `evdev`, so this is a Linux-only affair.
*   **Latency:** It's ADB-based, so there's a tiny bit of lag. It's fine for overlays or casual play, but maybe don't try to win a Frame-Perfect-Super-Meat-Boy competition with it.
*   **D-Pad Synthesis:** Most Android gamepads report the D-pad as "Hats" (absolute axes). This script synthesizes `BTN_DPAD_*` events so that most Linux apps and overlays recognize them correctly.
