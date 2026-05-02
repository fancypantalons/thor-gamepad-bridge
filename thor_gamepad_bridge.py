#!/usr/bin/env python3
"""
Android Gamepad Bridge

Reads gamepad events from an Android device via ADB and creates a virtual
evdev device on the Linux host that mirrors the Android controller.

Useful for:
- Gamepad overlays in OBS (via scrcpy or similar)
- Playing Android-only games with a local gamepad setup
- Specialized remote-play configurations

Requires:
    - adb (Android Debug Bridge) in PATH
    - python-evdev (pip install evdev)
    - root or membership in the 'input' group for UInput access
"""

import argparse
import logging
import os
import re
import signal
import subprocess
import sys
import time
from typing import Dict, List, Optional, Set, Tuple

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

try:
    from evdev import UInput, AbsInfo, ecodes
except ImportError:
    logger.error("python-evdev is required. Install it with: pip install evdev")
    sys.exit(1)

class AndroidDeviceInfo:
    """Holds parsed information about an Android input device."""

    def __init__(self, path: str):
        self.path = path
        self.name: str = "Unknown Android Device"
        self.bus: int = 0
        self.vendor: int = 0
        self.product: int = 0
        self.version: int = 0
        
        self.keys: Set[int] = set()
        self.abs_axes: Dict[int, AbsInfo] = {}
        self.rel_axes: Set[int] = set()
        self.msc_codes: Set[int] = set()
        self.ff_codes: Set[int] = set()
        self.sw_codes: Set[int] = set()

    def __repr__(self) -> str:
        return f"<AndroidDeviceInfo {self.path} '{self.name}'>"

    def is_gamepad(self) -> bool:
        """Heuristic: a gamepad should have both keys (buttons) and absolute axes (sticks)."""
        return ((len(self.keys) > 0) and (len(self.abs_axes) > 0))

def run_adb(args: List[str], serial: Optional[str] = None, timeout: Optional[int] = 10) -> subprocess.CompletedProcess:
    """Run an adb command and return the completed process."""
    cmd = ["adb"]
    
    if (serial is not None):
        cmd.extend(["-s", serial])
        
    cmd.extend(args)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        logger.error("'adb' not found in PATH. Please install Android Debug Bridge.")
        sys.exit(1)
    except subprocess.TimeoutExpired:
        logger.error(f"ADB command timed out: {' '.join(cmd)}")
        sys.exit(1)

    if (result.returncode != 0):
        stderr = result.stderr.strip()
        
        if (("no devices" in stderr.lower()) or ("device not found" in stderr.lower())):
            logger.error("No Android device detected via ADB. Is it connected and USB debugging enabled?")
        elif (len(stderr) > 0):
            logger.error(f"ADB error: {stderr}")
        else:
            logger.error(f"ADB command failed (exit code {result.returncode}): {' '.join(cmd)}")
            
        sys.exit(1)
        
    return result

def parse_getevent_il(output: str) -> List[AndroidDeviceInfo]:
    """Parse the output of 'adb shell getevent -il' into device structures."""
    lines = output.splitlines()
    devices: List[AndroidDeviceInfo] = []
    current: Optional[AndroidDeviceInfo] = None

    # Regex patterns for getevent -il output
    add_device_re = re.compile(r'^add device \d+: (.+)$')
    prop_re = re.compile(r'^\s+(\w+):?\s+([0-9a-fA-F]+)$')
    name_re = re.compile(r'^\s+name:\s+"(.+)"$')
    event_type_re = re.compile(r'^(\w+)\s+\([0-9a-fA-F]+\):')
    
    abs_line_re = re.compile(
        r'([A-Z][A-Z_0-9]+)\s*:\s*value\s+(-?\d+),\s*min\s+(-?\d+),\s*max\s+(-?\d+),\s*fuzz\s+(-?\d+),\s*flat\s+(-?\d+),\s*resolution\s+(-?\d+)'
    )
    token_re = re.compile(r'[A-Z][A-Z_0-9]+')

    i = 0
    
    while (i < len(lines)):
        line = lines[i]
        
        m = add_device_re.match(line)
        
        if (m is not None):
            if (current is not None):
                devices.append(current)
                
            current = AndroidDeviceInfo(path=m.group(1))
            i += 1
            continue

        if (current is None):
            i += 1
            continue

        stripped = line.strip()

        if (stripped.startswith("name:")):
            m = name_re.match(line)
            
            if (m is not None):
                current.name = m.group(1)
                
        elif (any((stripped.startswith(p)) for p in ["bus:", "vendor", "product", "version"])):
            m = prop_re.match(stripped)
            
            if (m is not None):
                key, val = m.groups()
                
                if ("bus" in key): 
                    current.bus = int(val, 16)
                elif ("vendor" in key): 
                    current.vendor = int(val, 16)
                elif ("product" in key): 
                    current.product = int(val, 16)
                elif ("version" in key): 
                    current.version = int(val, 16)

        elif (event_type_re.match(stripped) is not None):
            cap_match = event_type_re.match(stripped)
            cap_type = cap_match.group(1)
            rest = stripped[cap_match.end():].strip()

            # Handle multi-line capabilities
            j = i + 1
            
            while (j < len(lines)):
                next_line = lines[j].strip()
                
                if ((len(next_line) == 0) or (event_type_re.match(next_line) is not None) or (add_device_re.match(lines[j]) is not None)):
                    break
                    
                # Don't consume properties if they appear after events
                props = ["bus:", "vendor", "product", "version", "name:", "location:", "id:"]
                
                if (any((next_line.startswith(p)) for p in props)):
                    break
                    
                rest += " " + next_line
                j += 1
                
            i = j - 1

            if (cap_type == "ABS"):
                for abs_match in abs_line_re.finditer(rest):
                    tok = abs_match.group(1)
                    code = getattr(ecodes, tok, None)
                    
                    if (code is not None):
                        current.abs_axes[code] = AbsInfo(
                            value=int(abs_match.group(2)),
                            min=int(abs_match.group(3)),
                            max=int(abs_match.group(4)),
                            fuzz=int(abs_match.group(5)),
                            flat=int(abs_match.group(6)),
                            resolution=int(abs_match.group(7)),
                        )
            else:
                target_map = {
                    "KEY": current.keys,
                    "REL": current.rel_axes,
                    "MSC": current.msc_codes,
                    "SW": current.sw_codes,
                    "FF": current.ff_codes,
                }
                
                target_set = target_map.get(cap_type)
                
                if (target_set is not None):
                    for tok in token_re.findall(rest):
                        code = getattr(ecodes, tok, None)
                        
                        if (code is not None):
                            target_set.add(code)
                            
        i += 1

    if (current is not None):
        devices.append(current)
        
    return devices

def discover_gamepads(serial: Optional[str] = None) -> List[AndroidDeviceInfo]:
    """Discover gamepad devices connected to the Android device."""
    result = run_adb(["shell", "getevent", "-il"], serial=serial)
    
    devices = parse_getevent_il(result.stdout)
    
    return [d for d in devices if d.is_gamepad()]

def create_virtual_device(device: AndroidDeviceInfo) -> UInput:
    """Create a UInput virtual device based on the Android device profile."""
    events = {}

    # Cleanup: remove keys that Android uses for system navigation
    system_keys = {
        ecodes.KEY_HOME, 
        ecodes.KEY_BACK, 
        ecodes.KEY_APPSELECT, 
        ecodes.KEY_VOLUMEUP, 
        ecodes.KEY_VOLUMEDOWN
    }
    
    keys = device.keys - system_keys
    
    # Ensure DPAD buttons are available (we synthesize them from hats)
    dpad_buttons = {
        ecodes.BTN_DPAD_UP, 
        ecodes.BTN_DPAD_DOWN, 
        ecodes.BTN_DPAD_LEFT, 
        ecodes.BTN_DPAD_RIGHT
    }
    
    keys |= dpad_buttons

    if (len(keys) > 0): 
        events[ecodes.EV_KEY] = list(keys)
        
    if (len(device.abs_axes) > 0): 
        events[ecodes.EV_ABS] = list(device.abs_axes.items())
        
    if (len(device.rel_axes) > 0): 
        events[ecodes.EV_REL] = list(device.rel_axes)
        
    if (len(device.msc_codes) > 0): 
        events[ecodes.EV_MSC] = list(device.msc_codes)
        
    if (len(device.sw_codes) > 0): 
        events[ecodes.EV_SW] = list(device.sw_codes)

    # Attempt to find a matching bustype name for display
    bustype = device.bus
    
    for attr in dir(ecodes):
        if ((attr.startswith("BUS_")) and (getattr(ecodes, attr) == device.bus)):
            bustype = getattr(ecodes, attr)
            break

    vname = f"Android Bridge: {device.name}"
    
    ui = UInput(
        events,
        name=vname,
        vendor=device.vendor,
        product=device.product,
        version=device.version,
        bustype=bustype,
    )
    
    return ui

def _emit_dpad_from_hat(ui: UInput, axis: int, value: int, last_value: int) -> None:
    """Convert ABS_HAT0 events into BTN_DPAD button presses."""
    mapping = {
        ecodes.ABS_HAT0X: (-1, ecodes.BTN_DPAD_LEFT, 1, ecodes.BTN_DPAD_RIGHT),
        ecodes.ABS_HAT0Y: (-1, ecodes.BTN_DPAD_UP, 1, ecodes.BTN_DPAD_DOWN),
    }
    
    if (axis not in mapping):
        return

    neg_val, neg_btn, pos_val, pos_btn = mapping[axis]

    # Release previous buttons
    if (last_value == neg_val): 
        ui.write(ecodes.EV_KEY, neg_btn, 0)
    elif (last_value == pos_val): 
        ui.write(ecodes.EV_KEY, pos_btn, 0)

    # Press new buttons
    if (value == neg_val): 
        ui.write(ecodes.EV_KEY, neg_btn, 1)
    elif (value == pos_val): 
        ui.write(ecodes.EV_KEY, pos_btn, 1)


def stream_events(device_path: str, ui: UInput, serial: Optional[str] = None) -> None:
    """Stream events from ADB and forward them to the virtual device."""
    adb_cmd = ["adb"]
    
    if (serial is not None):
        adb_cmd.extend(["-s", serial])
        
    adb_cmd.extend(["shell", "getevent", device_path])

    proc = subprocess.Popen(
        adb_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1  # Line buffered
    )

    logger.info(f"Bridging {device_path} to {ui.device.path}")
    logger.info("Ready! Press Ctrl+C to stop.")

    hat_state = {ecodes.ABS_HAT0X: 0, ecodes.ABS_HAT0Y: 0}

    try:
        while True:
            line = proc.stdout.readline()
            
            if ((line is None) or (len(line) == 0)):
                if (proc.poll() is not None):
                    break
                    
                continue

            parts = line.strip().split()
            
            if (len(parts) != 3):
                continue

            try:
                etype, ecode, evalue = [int(p, 16) for p in parts]
                
                # Convert unsigned 32-bit hex to signed for negative values
                if (evalue >= 0x80000000):
                    evalue -= 0x100000000

                # Synthesize D-pad buttons from hats
                if ((etype == ecodes.EV_ABS) and (ecode in hat_state) and (evalue != hat_state[ecode])):
                    _emit_dpad_from_hat(ui, ecode, evalue, hat_state[ecode])
                    hat_state[ecode] = evalue

                ui.write(etype, ecode, evalue)
                ui.syn()
                
            except (ValueError, OverflowError):
                continue

    except KeyboardInterrupt:
        print("") # New line after ^C
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()


def main():
    parser = argparse.ArgumentParser(
        description="Bridge Android gamepad input to a Linux virtual device."
    )
    parser.add_argument("-s", "--serial", help="Android device serial number")
    parser.add_argument("-d", "--device", help="Android input device path (e.g. /dev/input/event9)")
    parser.add_argument("-l", "--list", action="store_true", help="List Android input devices")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")
    args = parser.parse_args()

    if (args.verbose):
        logger.setLevel(logging.DEBUG)

    if (args.list):
        # Trigger validation via a dummy run
        run_adb(["shell", "getevent", "-il"], serial=args.serial)
        
        output = run_adb(["shell", "getevent", "-il"], serial=args.serial).stdout
        devices = parse_getevent_il(output)
        
        print(f"{'Path':<20} {'Name':<30} {'Type'}")
        print("-" * 60)
        
        for d in devices:
            dtype = "GAMEPAD" if d.is_gamepad() else "Other"
            print(f"{d.path:<20} {d.name:<30} {dtype}")
            
        return

    # Find the target device
    gamepads = discover_gamepads(serial=args.serial)
    
    target_device: Optional[AndroidDeviceInfo] = None
    
    if (args.device is not None):
        # User specified a path, find it in the list
        output = run_adb(["shell", "getevent", "-il"], serial=args.serial).stdout
        all_devices = parse_getevent_il(output)
        
        for d in all_devices:
            if (d.path == args.device):
                target_device = d
                break
                
        if (target_device is None):
            logger.error(f"Device {args.device} not found.")
            sys.exit(1)
            
    elif (len(gamepads) == 0):
        logger.error("No gamepads detected. Connect a controller to your Android device or use --list.")
        sys.exit(1)
        
    elif (len(gamepads) == 1):
        target_device = gamepads[0]
        logger.info(f"Auto-selected: {target_device.name} ({target_device.path})")
        
    else:
        print("Multiple gamepads found. Please select one:")
        
        for idx, gp in enumerate(gamepads, 1):
            print(f"  {idx}. {gp.name} ({gp.path})")
            
        while True:
            try:
                choice = int(input("Selection: "))
                
                if ((1 <= choice) and (choice <= len(gamepads))):
                    target_device = gamepads[choice - 1]
                    break
                    
            except ValueError:
                pass
                
            print("Invalid selection.")

    # Check for uinput permissions
    if (not os.access("/dev/uinput", os.W_OK)):
        logger.error("No write access to /dev/uinput. Try running with sudo or add yourself to the 'input' group.")
        sys.exit(1)

    ui = create_virtual_device(target_device)
    
    def cleanup(signum=None, frame=None):
        logger.info("Cleaning up...")
        ui.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    stream_events(target_device.path, ui, serial=args.serial)
    cleanup()


if __name__ == "__main__":
    main()
