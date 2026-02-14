#!/usr/bin/env python3
"""Apply mixing changes to an Ableton Live Set (.als) file.

Takes an .als file and a JSON file describing changes, outputs a new .als file.

JSON format:
{
  "changes": [
    {
      "track_name": "Snare",
      "track_type": "MidiTrack",       // optional, to disambiguate duplicate names
      "track_index": 0,                 // optional, 0-based index among matches
      "target": "volume",              // volume | pan | send | device_param | add_device | group_volume
      "value": -6.0,                   // for volume/send: dB value. for pan: position string like "18R" or "C"
      "send_index": 0,                 // for send: 0=A, 1=B, 2=C, etc.
      "device_tag": "GlueCompressor",  // for device_param: the XML tag of the device
      "device_index": 0,               // for device_param: 0-based index if multiple of same type
      "param_name": "Ratio",           // for device_param: the XML element name within the device
      "param_value": 4.0               // for device_param: the raw value to set
    }
  ]
}

Special track names:
  - "MASTER" or "MAIN": targets the Main/Master track
  - "RETURN:A-Reverb": targets a return track by name (prefix with RETURN:)
"""

import sys
import gzip
import json
import math
import os
import copy
import xml.etree.ElementTree as ET


def db_to_linear(db):
    """Convert dB to linear volume value."""
    if db is None or db <= -70:
        return 0.0003162277571  # Ableton's -inf
    return 10 ** (db / 20.0)


def pan_str_to_value(pan_str):
    """Convert pan string like '18R', '22L', 'C' to internal -1 to 1 value."""
    pan_str = str(pan_str).strip()
    if pan_str.upper() == "C" or pan_str == "0":
        return 0.0
    if pan_str.upper().endswith("L"):
        pos = float(pan_str[:-1])
        return -(pos / 50.0)
    elif pan_str.upper().endswith("R"):
        pos = float(pan_str[:-1])
        return pos / 50.0
    else:
        # Try as raw float
        return float(pan_str)


def find_tracks_by_name(tracks_el, name, track_type=None):
    """Find track elements matching a name and optional type."""
    matches = []
    for track in tracks_el:
        name_el = track.find(".//Name/EffectiveName")
        if name_el is None:
            continue
        if name_el.get("Value") == name:
            if track_type is None or track.tag == track_type:
                matches.append(track)
    return matches


# Device parameters that are stored as linear amplitude values in XML
# but specified as dB in the change JSON (same encoding as volume).
# Key format: (device_tag, param_name)
DB_LINEAR_PARAMS = {
    ("Compressor2", "Threshold"),
    ("Compressor2", "OutputGain"),
    ("Gate", "Threshold"),
    ("Gate", "Return"),
}


def find_max_id(root):
    """Find the highest Id attribute value in the entire XML tree."""
    max_id = 0
    for el in root.iter():
        id_val = el.get("Id")
        if id_val is not None:
            try:
                max_id = max(max_id, int(id_val))
            except ValueError:
                pass
    return max_id


def find_donor_device(root, device_tag):
    """Find an existing device of the given type anywhere in the project to use as a template."""
    for el in root.iter():
        if el.tag == device_tag:
            return el
    return None


def remap_ids(element, start_id):
    """Remap all Id attributes in an element tree to new unique values starting from start_id."""
    next_id = start_id
    for el in element.iter():
        if "Id" in el.attrib:
            el.set("Id", str(next_id))
            next_id += 1
    return next_id


def reset_eq8_defaults(device):
    """Reset an EQ Eight clone to clean defaults — all bands off with neutral settings."""
    for i in range(8):
        for param_set in ("ParameterA", "ParameterB"):
            prefix = f"Bands.{i}/{param_set}"
            # Turn off all bands
            is_on = device.find(f"{prefix}/IsOn/Manual")
            if is_on is not None:
                is_on.set("Value", "false")
            # Reset gain to 0
            gain = device.find(f"{prefix}/Gain/Manual")
            if gain is not None:
                gain.set("Value", "0")
            # Reset Q
            q = device.find(f"{prefix}/Q/Manual")
            if q is not None:
                q.set("Value", "0.7071067")


def reset_compressor_defaults(device):
    """Reset a Compressor2 clone to clean defaults."""
    defaults = {
        "Threshold": str(db_to_linear(0)),  # 0 dB
        "Ratio": "2",
        "Attack": "10",
        "Release": "100",
        "DryWet": "1",
        "GainCompensation": "true",
    }
    for param, val in defaults.items():
        el = device.find(f"{param}/Manual")
        if el is not None:
            el.set("Value", val)


DEVICE_RESETTERS = {
    "Eq8": reset_eq8_defaults,
    "Compressor2": reset_compressor_defaults,
}


def find_device(track_el, device_tag, device_index=0):
    """Find a device in a track's device chain by tag and index."""
    devices_el = track_el.find("DeviceChain/DeviceChain/Devices")
    if devices_el is None:
        return None
    matches = [d for d in devices_el if d.tag == device_tag]
    if device_index < len(matches):
        return matches[device_index]
    return None


def set_param_value(element, param_path, value):
    """Set a parameter value, handling both Manual sub-element and direct Value attribute."""
    parts = param_path.split("/")
    current = element
    for part in parts[:-1]:
        current = current.find(part)
        if current is None:
            return False

    target_name = parts[-1]
    target = current.find(target_name)
    if target is None:
        return False

    # Check for Manual sub-element
    manual = target.find("Manual")
    if manual is not None:
        manual.set("Value", str(value))
        return True

    # Direct Value attribute
    if "Value" in target.attrib:
        target.set("Value", str(value))
        return True

    return False


def apply_change(root, tracks_el, change):
    """Apply a single change to the XML tree. Returns a description of what was done."""
    track_name = change.get("track_name", "")
    target = change.get("target", "")
    descriptions = []

    # Find the target track
    track_el = None

    if track_name.upper() in ("MASTER", "MAIN"):
        track_el = root.find(".//LiveSet/MainTrack")
        if track_el is None:
            track_el = root.find(".//LiveSet/MasterTrack")
        if track_el is None:
            return [f"ERROR: Could not find Main/Master track"]
    elif track_name.upper().startswith("RETURN:"):
        return_name = track_name[7:]
        for t in tracks_el:
            if t.tag == "ReturnTrack":
                name_el = t.find(".//Name/EffectiveName")
                if name_el is not None and return_name in name_el.get("Value", ""):
                    track_el = t
                    break
        if track_el is None:
            return [f"ERROR: Could not find return track '{return_name}'"]
    else:
        track_type = change.get("track_type")
        matches = find_tracks_by_name(tracks_el, track_name, track_type)
        track_index = change.get("track_index", 0)
        if not matches:
            return [f"ERROR: Could not find track '{track_name}'"]
        if track_index >= len(matches):
            return [f"ERROR: Track '{track_name}' index {track_index} out of range (found {len(matches)})"]
        track_el = matches[track_index]

    mixer = track_el.find(".//DeviceChain/Mixer")

    if target == "volume":
        db_val = change.get("value")
        linear = db_to_linear(db_val)
        vol_el = mixer.find("Volume/Manual")
        if vol_el is not None:
            old_val = float(vol_el.get("Value", "1"))
            old_db = 20 * math.log10(old_val) if old_val > 0.0003163 else float("-inf")
            vol_el.set("Value", str(linear))
            descriptions.append(f"  {track_name}: Volume {old_db:+.1f} → {db_val:+.1f} dB")

    elif target == "pan":
        pan_str = change.get("value")
        pan_val = pan_str_to_value(pan_str)
        pan_el = mixer.find("Pan/Manual")
        if pan_el is not None:
            old_val = float(pan_el.get("Value", "0"))
            old_str = "C" if abs(old_val) < 0.01 else f"{abs(old_val)*50:.0f}{'L' if old_val < 0 else 'R'}"
            pan_el.set("Value", str(pan_val))
            descriptions.append(f"  {track_name}: Pan {old_str} → {pan_str}")

    elif target == "send":
        send_index = change.get("send_index", 0)
        db_val = change.get("value")
        linear = db_to_linear(db_val)
        sends_el = mixer.find("Sends")
        if sends_el is not None:
            holders = list(sends_el)
            if send_index < len(holders):
                send_manual = holders[send_index].find("Send/Manual")
                if send_manual is not None:
                    old_val = float(send_manual.get("Value", "0.0003162277571"))
                    old_db = 20 * math.log10(old_val) if old_val > 0.0003163 else float("-inf")
                    old_str = f"{old_db:+.1f}" if not math.isinf(old_db) else "-inf"
                    send_manual.set("Value", str(linear))
                    send_label = chr(65 + send_index)
                    descriptions.append(f"  {track_name}: Send {send_label} {old_str} → {db_val:+.1f} dB")

    elif target == "device_param":
        device_tag = change.get("device_tag")
        device_index = change.get("device_index", 0)
        param_name = change.get("param_name")
        param_value = change.get("param_value")

        device = find_device(track_el, device_tag, device_index)
        if device is None:
            return [f"ERROR: Could not find device '{device_tag}' on track '{track_name}'"]

        # Convert dB→linear for params that use linear encoding
        display_value = param_value
        if (device_tag, param_name) in DB_LINEAR_PARAMS:
            param_value = db_to_linear(param_value)
            display_value = f"{change.get('param_value')} dB"

        # Try to get old value for description
        old_value = None
        target_el = device.find(param_name)
        if target_el is not None:
            manual = target_el.find("Manual")
            if manual is not None:
                old_value = manual.get("Value")
            elif "Value" in target_el.attrib:
                old_value = target_el.get("Value")

        # Format old value as dB for linear params
        old_str = "?"
        if old_value is not None:
            if (device_tag, param_name) in DB_LINEAR_PARAMS:
                old_float = float(old_value)
                if old_float > 0.0003163:
                    old_str = f"{20 * math.log10(old_float):.1f} dB"
                else:
                    old_str = "-inf dB"
            else:
                old_str = str(old_value)

        success = set_param_value(device, param_name, param_value)
        if success:
            device_display = change.get("device_name", device_tag)
            descriptions.append(f"  {track_name}: {device_display} {param_name} {old_str} → {display_value}")
        else:
            return [f"ERROR: Could not set {param_name} on {device_tag} for track '{track_name}'"]

    elif target == "add_device":
        device_tag = change.get("device_tag")
        position = change.get("position", -1)  # -1 = end, 0 = first, etc.
        params = change.get("params", {})
        device_display = change.get("device_name", device_tag)

        # Find a donor device to clone
        donor = find_donor_device(root, device_tag)
        if donor is None:
            return [f"ERROR: No existing '{device_tag}' found in project to use as template"]

        # Deep copy the donor
        new_device = copy.deepcopy(donor)

        # Remap all IDs to unique values
        max_id = find_max_id(root)
        remap_ids(new_device, max_id + 1)

        # Reset to defaults if we have a resetter for this device type
        resetter = DEVICE_RESETTERS.get(device_tag)
        if resetter:
            resetter(new_device)

        # Ensure device is on
        on_el = new_device.find("On/Manual")
        if on_el is not None:
            on_el.set("Value", "true")

        # Apply requested parameters
        for param_path, param_val in params.items():
            # Handle dB→linear conversion for known params
            actual_val = param_val
            if (device_tag, param_path) in DB_LINEAR_PARAMS:
                actual_val = db_to_linear(float(param_val))
            success = set_param_value(new_device, param_path, actual_val)
            if not success:
                return [f"ERROR: Could not set param '{param_path}' on new {device_tag} for '{track_name}'"]

        # Insert into the track's device chain
        devices_el = track_el.find("DeviceChain/DeviceChain/Devices")
        if devices_el is None:
            return [f"ERROR: No device chain found on track '{track_name}'"]

        device_list = list(devices_el)
        if position == -1 or position >= len(device_list):
            devices_el.append(new_device)
            pos_desc = "end"
        else:
            devices_el.insert(position, new_device)
            pos_desc = f"position {position}"

        param_desc = ", ".join(f"{k}={v}" for k, v in params.items()) if params else "defaults"
        descriptions.append(f"  {track_name}: Added {device_display} at {pos_desc} ({param_desc})")

    elif target == "group_volume":
        # For group tracks, same as volume but explicitly for groups
        db_val = change.get("value")
        linear = db_to_linear(db_val)
        vol_el = mixer.find("Volume/Manual")
        if vol_el is not None:
            old_val = float(vol_el.get("Value", "1"))
            old_db = 20 * math.log10(old_val) if old_val > 0.0003163 else float("-inf")
            vol_el.set("Value", str(linear))
            descriptions.append(f"  {track_name} (group): Volume {old_db:+.1f} → {db_val:+.1f} dB")

    return descriptions


def main():
    if len(sys.argv) < 3 or len(sys.argv) > 4:
        print("Usage: python3 modify_als.py <input.als> <changes.json> [output.als]", file=sys.stderr)
        print("  If output.als is not specified, appends '-modified' to the input filename.", file=sys.stderr)
        sys.exit(1)

    input_path = sys.argv[1]
    changes_path = sys.argv[2]

    if len(sys.argv) == 4:
        output_path = sys.argv[3]
    else:
        base, ext = os.path.splitext(input_path)
        output_path = f"{base}-modified{ext}"

    # Read the .als file
    try:
        with gzip.open(input_path, "rt", encoding="utf-8") as f:
            xml_content = f.read()
    except FileNotFoundError:
        print(f"Error: File not found: {input_path}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error reading .als file: {e}", file=sys.stderr)
        sys.exit(1)

    # Parse XML
    root = ET.fromstring(xml_content)
    tracks_el = root.find(".//LiveSet/Tracks")
    if tracks_el is None:
        print("Error: No Tracks element found", file=sys.stderr)
        sys.exit(1)

    # Read changes
    try:
        with open(changes_path, "r") as f:
            changes_data = json.load(f)
    except Exception as e:
        print(f"Error reading changes file: {e}", file=sys.stderr)
        sys.exit(1)

    changes = changes_data.get("changes", [])
    if not changes:
        print("No changes to apply.", file=sys.stderr)
        sys.exit(0)

    # Apply changes
    print(f"Applying {len(changes)} changes...")
    all_descriptions = []
    errors = []

    for change in changes:
        result = apply_change(root, tracks_el, change)
        for desc in result:
            if desc.startswith("ERROR:"):
                errors.append(desc)
                print(desc, file=sys.stderr)
            else:
                all_descriptions.append(desc)
                print(desc)

    if errors:
        print(f"\n{len(errors)} error(s) occurred.", file=sys.stderr)

    # Write output
    xml_output = ET.tostring(root, encoding="unicode", xml_declaration=True)

    with gzip.open(output_path, "wt", encoding="utf-8") as f:
        f.write(xml_output)

    print(f"\nWritten to: {output_path}")
    print(f"Applied {len(all_descriptions)} changes successfully.")
    if errors:
        print(f"{len(errors)} changes failed.")


if __name__ == "__main__":
    main()
