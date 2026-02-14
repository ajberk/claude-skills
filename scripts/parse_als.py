#!/usr/bin/env python3
"""Extract mixing data from Ableton Live Set (.als) files."""

import sys
import gzip
import math
import xml.etree.ElementTree as ET

# --- Device name mapping ---
DEVICE_NAMES = {
    # Instruments
    "InstrumentGroupDevice": "Instrument Rack",
    "DrumGroupDevice": "Drum Rack",
    "Operator": "Operator",
    "InstrumentVector": "Wavetable",
    "OriginalSimpler": "Simpler",
    "MultiSampler": "Sampler",
    "StringStudio": "Tension",
    "Collision": "Collision",
    "LoungeLizard": "Electric",
    "InstrumentImpulse": "Impulse",
    "UltraAnalog": "Analog",
    "Drift": "Drift",
    "Meld": "Meld",
    # Audio Effects
    "AudioEffectGroupDevice": "Audio Effect Rack",
    "Reverb": "Reverb",
    "Delay": "Delay",
    "Eq8": "EQ Eight",
    "ChannelEq": "Channel EQ",
    "GlueCompressor": "Glue Compressor",
    "Compressor2": "Compressor",
    "AutoFilter": "Auto Filter",
    "FilterDelay": "Filter Delay",
    "Chorus2": "Chorus-Ensemble",
    "Phaser": "Phaser",
    "Flanger": "Flanger",
    "Gate": "Gate",
    "Limiter": "Limiter",
    "MultibandDynamics": "Multiband Dynamics",
    "Saturator": "Saturator",
    "Overdrive": "Overdrive",
    "Redux2": "Redux",
    "DrumBuss": "Drum Buss",
    "PingPongDelay": "Ping Pong Delay",
    "Vinyl": "Vinyl Distortion",
    "StereoGain": "Utility",
    "Tuner": "Tuner",
    "SpectrumAnalyzer": "Spectrum",
    "CrossDelay": "Echo",
    "Corpus": "Corpus",
    "Resonators": "Resonators",
    "FrequencyShifter": "Frequency Shifter",
    "BeatRepeat": "Beat Repeat",
    "Erosion": "Erosion",
    "Amp": "Amp",
    "Cabinet": "Cabinet",
    "Pedal": "Pedal",
    "Shifter": "Shifter",
    # Third-party
    "PluginDevice": "VST Plugin",
    "AuPluginDevice": "AU Plugin",
    "Vst3PluginDevice": "VST3 Plugin",
}

# Which native device params to extract (mixing-relevant)
DEVICE_PARAMS = {
    "Reverb": {
        "PreDelay": ("Pre-delay", "ms"),
        "DecayTime": ("Decay", "ms"),
        "RoomSize": ("Room Size", "%"),
        "DryWet": ("Dry/Wet", "ratio"),
    },
    "Delay": {
        "Feedback": ("Feedback", "ratio"),
        "DryWet": ("Dry/Wet", "ratio"),
    },
    "Compressor2": {
        "Threshold": ("Threshold", "dB_linear"),
        "Ratio": ("Ratio", ""),
        "Attack": ("Attack", "ms"),
        "Release": ("Release", "ms"),
        "GainCompensation": ("Makeup", "bool"),
        "DryWet": ("Dry/Wet", "ratio"),
    },
    "GlueCompressor": {
        "Threshold": ("Threshold", "dB"),
        "Ratio": ("Ratio", ""),
        "Attack": ("Attack", "ms"),
        "Release": ("Release", "s"),
        "Makeup": ("Makeup", "dB"),
        "DryWet": ("Dry/Wet", "ratio"),
    },
    "Limiter": {
        "Ceiling": ("Ceiling", "dB"),
        "Release": ("Release", "ms"),
        "Gain": ("Gain", "dB"),
    },
    "Eq8": {
        "GlobalGain": ("Global Gain", "dB"),
    },
    "ChannelEq": {
        "HighpassOn": ("HPF", "bool"),
        "LowShelfGain": ("Low Gain", "raw"),
        "MidGain": ("Mid Gain", "raw"),
        "MidFreq": ("Mid Freq", "Hz"),
        "HighShelfGain": ("High Gain", "raw"),
    },
    "StereoGain": {},  # Utility — handled specially to suppress defaults
    "Saturator": {
        "DryWet": ("Dry/Wet", "ratio"),
        "Drive": ("Drive", "dB"),
    },
    "DrumBuss": {
        "DryWet": ("Dry/Wet", "ratio"),
        "Drive": ("Drive", "raw"),
    },
    "Gate": {
        "Threshold": ("Threshold", "dB_linear"),
        "Return": ("Return", "dB_linear"),
        "Attack": ("Attack", "ms"),
        "Hold": ("Hold", "ms"),
        "Release": ("Release", "ms"),
    },
    "MultibandDynamics": {
        "DryWet": ("Dry/Wet", "ratio"),
    },
    "AutoFilter": {
        "DryWet": ("Dry/Wet", "ratio"),
        "Frequency": ("Frequency", "Hz"),
        "Resonance": ("Resonance", "raw"),
    },
}

PLUGIN_TAGS = {"PluginDevice", "AuPluginDevice", "Vst3PluginDevice"}


def vol_to_db(value):
    """Convert internal volume value to dB."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v <= 0.0003163:
        return float("-inf")
    return 20 * math.log10(v)


def db_str(db):
    """Format dB value as string."""
    if db is None:
        return "N/A"
    if math.isinf(db) and db < 0:
        return "-inf"
    return f"{db:+.1f} dB"


def pan_to_str(value):
    """Convert internal pan value to human-readable position."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "N/A"
    if abs(v) < 0.01:
        return "C"
    pos = abs(v) * 50
    direction = "L" if v < 0 else "R"
    return f"{pos:.0f}{direction}"


def format_param(name, value, unit):
    """Format a device parameter value."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return f"{name}: {value}"

    if unit == "bool":
        return f"{name}: {'on' if v else 'off'}"
    elif unit == "ratio":
        return f"{name}: {v * 100:.0f}%"
    elif unit == "dB":
        return f"{name}: {v:.1f} dB"
    elif unit == "dB_linear":
        # Stored as linear amplitude, display as dB
        if v <= 0.0003163:
            return f"{name}: -inf dB"
        return f"{name}: {20 * math.log10(v):.1f} dB"
    elif unit == "dB_utility":
        # Utility gain is stored as linear value
        if v <= 0.0003163:
            return f"{name}: -inf"
        return f"{name}: {20 * math.log10(v):+.1f} dB"
    elif unit == "ms":
        if v >= 1000:
            return f"{name}: {v / 1000:.1f}s"
        return f"{name}: {v:.0f}ms"
    elif unit == "s":
        return f"{name}: {v:.2f}s"
    elif unit == "Hz":
        if v >= 1000:
            return f"{name}: {v / 1000:.1f}kHz"
        return f"{name}: {v:.0f}Hz"
    elif unit == "%":
        return f"{name}: {v:.0f}%"
    elif unit == "int":
        return f"{name}: {int(v)}"
    else:
        return f"{name}: {v:.2f}"


def get_param_value(element, param_name):
    """Get a parameter value from a device element. Checks both direct Value attr and Manual sub-element."""
    child = element.find(param_name)
    if child is None:
        return None
    # Check for Manual sub-element first (common for automatable params)
    manual = child.find("Manual")
    if manual is not None:
        return manual.get("Value")
    # Fall back to direct Value attribute
    return child.get("Value")


def extract_plugin_name(device_element):
    """Extract third-party plugin name."""
    # Try various paths where plugin names are stored
    for path in [
        "PluginDesc/VstPluginInfo/PlugName",
        "PluginDesc/Vst3PluginInfo/Name",
        "PluginDesc/AuPluginInfo/Name",
    ]:
        el = device_element.find(path)
        if el is not None:
            name = el.get("Value")
            if name:
                return name
    return None


def extract_eq8_bands(device_element):
    """Extract EQ Eight band info."""
    bands = []
    for i in range(1, 9):
        band_on = get_param_value(device_element, f"Bands.{i - 1}/ParameterA/IsOn")
        if band_on is None:
            # Try alternate path
            band_on = get_param_value(device_element, f"Band{i}On")
        if band_on and str(band_on).lower() == "true":
            gain = get_param_value(device_element, f"Bands.{i - 1}/ParameterA/Gain")
            freq = get_param_value(device_element, f"Bands.{i - 1}/ParameterA/Freq")
            q = get_param_value(device_element, f"Bands.{i - 1}/ParameterA/Q")
            band_info = f"B{i}"
            if freq:
                f_val = float(freq)
                band_info += f" {f_val:.0f}Hz" if f_val < 1000 else f" {f_val / 1000:.1f}kHz"
            if gain:
                band_info += f" {float(gain):+.1f}dB"
            if q:
                band_info += f" Q{float(q):.1f}"
            bands.append(band_info)
    return bands


def extract_devices(devices_element):
    """Extract device chain info."""
    if devices_element is None:
        return []

    devices = []
    for dev in devices_element:
        tag = dev.tag
        name = DEVICE_NAMES.get(tag, tag)

        # Check on/off
        on_val = get_param_value(dev, "On")
        is_on = on_val is None or str(on_val).lower() == "true"

        device_info = {"tag": tag, "name": name, "on": is_on, "params": []}

        # Third-party plugin name
        if tag in PLUGIN_TAGS:
            plugin_name = extract_plugin_name(dev)
            if plugin_name:
                device_info["name"] = f"{name}: {plugin_name}"

        # Utility special handling — only show non-default params
        if tag == "StereoGain":
            gain_val = get_param_value(dev, "Gain")
            if gain_val is not None:
                gain_db = vol_to_db(gain_val)
                if gain_db is not None and abs(gain_db) > 0.05:
                    device_info["params"].append(f"Gain: {db_str(gain_db)}")
            mute_val = get_param_value(dev, "Mute")
            if mute_val and str(mute_val).lower() == "true":
                device_info["params"].append("Muted")
            for phase_key, phase_label in [("PhaseInvertL", "Phase Invert L"), ("PhaseInvertR", "Phase Invert R")]:
                pv = get_param_value(dev, phase_key)
                if pv and str(pv).lower() == "true":
                    device_info["params"].append(phase_label)
            ch_mode = get_param_value(dev, "ChannelMode")
            if ch_mode is not None:
                mode_names = {0: "Stereo", 1: "Stereo", 2: "Left", 3: "Right", 4: "Swap", 5: "Mono", 6: "Mid", 7: "Side"}
                mode_int = int(float(ch_mode))
                if mode_int not in (0, 1):  # only show if not default stereo
                    device_info["params"].append(f"Mode: {mode_names.get(mode_int, mode_int)}")
            ms_bal = get_param_value(dev, "MidSideBalance")
            if ms_bal is not None and abs(float(ms_bal) - 1.0) > 0.01:
                device_info["params"].append(f"M/S Balance: {float(ms_bal):.2f}")
        # Native device params (non-Utility)
        elif tag in DEVICE_PARAMS:
            for param_key, (display_name, unit) in DEVICE_PARAMS[tag].items():
                val = get_param_value(dev, param_key)
                if val is not None:
                    device_info["params"].append(
                        format_param(display_name, val, unit)
                    )

        # EQ Eight special handling
        if tag == "Eq8":
            bands = extract_eq8_bands(dev)
            if bands:
                device_info["params"].append(f"Active bands: {', '.join(bands)}")

        devices.append(device_info)

    return devices


def extract_track(track_element, return_names):
    """Extract all mixing info from a track element."""
    track_type = track_element.tag
    track_id = track_element.get("Id")

    # Name
    name_el = track_element.find(".//Name/EffectiveName")
    name = name_el.get("Value") if name_el is not None else f"Track {track_id}"

    # Group membership
    group_el = track_element.find("TrackGroupId")
    group_id = int(group_el.get("Value")) if group_el is not None else -1

    # Mixer
    mixer = track_element.find(".//DeviceChain/Mixer")
    volume_db = None
    pan_str = "C"
    is_muted = False
    sends = []
    output_routing = "Main"

    if mixer is not None:
        # Volume
        vol_el = mixer.find("Volume/Manual")
        if vol_el is not None:
            volume_db = vol_to_db(vol_el.get("Value"))

        # Pan
        pan_el = mixer.find("Pan/Manual")
        if pan_el is not None:
            pan_str = pan_to_str(pan_el.get("Value"))

        # Speaker (mute)
        speaker_el = mixer.find("Speaker/Manual")
        if speaker_el is not None:
            is_muted = speaker_el.get("Value", "true").lower() == "false"

        # Sends
        sends_el = mixer.find("Sends")
        if sends_el is not None:
            for i, sh in enumerate(sends_el):
                send_val_el = sh.find("Send/Manual")
                active_el = sh.find("Active/Manual")
                send_db = vol_to_db(
                    send_val_el.get("Value") if send_val_el is not None else "0"
                )
                is_active = (
                    active_el is None
                    or active_el.get("Value", "true").lower() == "true"
                )
                r_name = return_names[i] if i < len(return_names) else f"Return {chr(65 + i)}"
                sends.append(
                    {"name": r_name, "db": send_db, "active": is_active}
                )

    # Output routing
    output_el = track_element.find("DeviceChain/AudioOutputRouting/UpperDisplayString")
    if output_el is not None:
        output_routing = output_el.get("Value", "Main")

    # Devices
    devices_el = track_element.find("DeviceChain/DeviceChain/Devices")
    devices = extract_devices(devices_el)

    type_labels = {
        "AudioTrack": "AUDIO",
        "MidiTrack": "MIDI",
        "ReturnTrack": "RETURN",
        "GroupTrack": "GROUP",
    }

    return {
        "type": type_labels.get(track_type, track_type),
        "raw_type": track_type,
        "id": track_id,
        "name": name,
        "group_id": group_id,
        "volume_db": volume_db,
        "pan": pan_str,
        "muted": is_muted,
        "sends": sends,
        "output": output_routing,
        "devices": devices,
    }


def detect_issues(tracks, returns, master):
    """Run heuristic checks for common mixing issues."""
    issues = []
    non_group = [t for t in tracks if t["type"] != "GROUP"]

    if not non_group:
        return issues

    # Gain staging: too many tracks at 0.0 dB
    zero_db = [t for t in non_group if t["volume_db"] is not None and abs(t["volume_db"]) < 0.05]
    if len(zero_db) > max(2, len(non_group) * 0.6):
        issues.append(
            f"{len(zero_db)} of {len(non_group)} tracks at 0.0 dB (default fader position — no gain staging)"
        )

    # Stereo spread
    center = [t for t in non_group if t["pan"] == "C"]
    if len(center) > max(3, len(non_group) * 0.7) and len(non_group) > 3:
        issues.append(
            f"{len(center)} of {len(non_group)} tracks panned to center (narrow stereo image)"
        )

    # No EQ on any track
    has_eq = any(
        "EQ" in d["name"] or "Channel EQ" in d["name"]
        for t in non_group
        for d in t["devices"]
    )
    if not has_eq and len(non_group) > 2:
        issues.append("No EQ found on any track")

    # No compression
    has_comp = any(
        "Compressor" in d["name"] or "Glue" in d["name"]
        for t in non_group
        for d in t["devices"]
    )
    if not has_comp and len(non_group) > 3:
        issues.append("No compressor found on any track")

    # No limiter on master
    if master:
        has_limiter = any("Limiter" in d["name"] for d in master.get("devices", []))
        if not has_limiter:
            issues.append("No limiter on the Main/Master track")

    # Sends unused
    if returns:
        all_sends_off = all(
            all(s["db"] is not None and (math.isinf(s["db"]) and s["db"] < 0) for s in t["sends"])
            for t in non_group
            if t["sends"]
        )
        if all_sends_off:
            issues.append(
                "Return tracks exist but no sends are active (all at -inf)"
            )

    # Hot tracks
    hot = [t["name"] for t in non_group if t["volume_db"] is not None and t["volume_db"] > 3.0]
    if hot:
        issues.append(f"Tracks above +3 dB (clipping risk): {', '.join(hot)}")

    # Empty tracks
    empty = [
        t["name"]
        for t in non_group
        if not t["devices"]
    ]
    if empty and len(empty) < len(non_group):
        issues.append(f"Tracks with no devices: {', '.join(empty)}")

    # Disabled devices
    disabled = []
    for t in tracks:
        off_devs = [d["name"] for d in t["devices"] if not d["on"]]
        if off_devs:
            disabled.append(f'"{t["name"]}": {", ".join(off_devs)}')
    if disabled:
        issues.append(f"Disabled devices on: {'; '.join(disabled)}")

    # Muted tracks
    muted = [t["name"] for t in tracks if t["muted"]]
    if muted:
        issues.append(f"Muted tracks: {', '.join(muted)}")

    return issues


def format_device(dev):
    """Format a single device for output."""
    status = "on" if dev["on"] else "OFF"
    params_str = ""
    if dev["params"]:
        params_str = f' ({", ".join(dev["params"])})'
    return f'{dev["name"]} ({status}){params_str}'


def format_output(project_info, tracks, returns, master, issues):
    """Format all data into structured text output."""
    lines = []
    lines.append("=== ABLETON LIVE SET ANALYSIS ===")
    lines.append(f'Version: {project_info["version"]}')
    lines.append(f'Tempo: {project_info["tempo"]} BPM')
    lines.append("")

    # Count track types
    non_group = [t for t in tracks if t["type"] != "GROUP"]
    groups = [t for t in tracks if t["type"] == "GROUP"]
    lines.append(
        f"=== TRACKS ({len(non_group)} tracks, {len(groups)} groups, {len(returns)} returns) ==="
    )

    # Build group map
    group_map = {int(g["id"]): g["name"] for g in groups}
    grouped = {}
    ungrouped = []

    for t in tracks:
        if t["type"] == "GROUP":
            continue
        if t["group_id"] > 0 and t["group_id"] in group_map:
            grouped.setdefault(t["group_id"], []).append(t)
        else:
            ungrouped.append(t)

    def format_track(t, indent="  "):
        """Format a single track line."""
        mute_flag = " [MUTED]" if t["muted"] else ""
        line = f'{indent}[{t["type"]}] "{t["name"]}" | Vol: {db_str(t["volume_db"])} | Pan: {t["pan"]} | Out: {t["output"]}{mute_flag}'
        lines.append(line)
        if t["devices"]:
            dev_strs = [format_device(d) for d in t["devices"]]
            lines.append(f"{indent}  Devices: {' -> '.join(dev_strs)}")
        # Show sends that are active (not -inf)
        active_sends = [
            s
            for s in t["sends"]
            if s["db"] is not None
            and not (math.isinf(s["db"]) and s["db"] < 0)
        ]
        if active_sends:
            send_strs = [f'{s["name"]}: {db_str(s["db"])}' for s in active_sends]
            lines.append(f"{indent}  Sends: {' | '.join(send_strs)}")

    # Print groups with their tracks
    for gid, group_name in sorted(group_map.items(), key=lambda x: x[1]):
        group_track = next((g for g in groups if int(g["id"]) == gid), None)
        group_vol = db_str(group_track["volume_db"]) if group_track else "N/A"
        lines.append(f'')
        lines.append(f'--- Group: "{group_name}" (Vol: {group_vol}) ---')
        for t in grouped.get(gid, []):
            format_track(t)

    if ungrouped:
        lines.append("")
        lines.append("--- Ungrouped ---")
        for t in ungrouped:
            format_track(t)

    # Returns
    if returns:
        lines.append("")
        lines.append("=== RETURN TRACKS ===")
        for t in returns:
            format_track(t, indent="  ")

    # Master
    if master:
        lines.append("")
        lines.append("=== MAIN/MASTER TRACK ===")
        lines.append(f'  Vol: {db_str(master["volume_db"])}')
        if master["devices"]:
            dev_strs = [format_device(d) for d in master["devices"]]
            lines.append(f"  Devices: {' -> '.join(dev_strs)}")

    # Issues
    if issues:
        lines.append("")
        lines.append("=== POTENTIAL ISSUES (auto-detected) ===")
        for issue in issues:
            lines.append(f"- {issue}")

    return "\n".join(lines)


def main():
    if len(sys.argv) != 2:
        print("Usage: python3 parse_als.py <path/to/file.als>", file=sys.stderr)
        sys.exit(1)

    als_path = sys.argv[1]

    try:
        with gzip.open(als_path, "rt", encoding="utf-8") as f:
            tree = ET.parse(f)
    except FileNotFoundError:
        print(f"Error: File not found: {als_path}", file=sys.stderr)
        sys.exit(1)
    except gzip.BadGzipFile:
        print(
            f"Error: Not a valid .als file (not gzip compressed): {als_path}",
            file=sys.stderr,
        )
        sys.exit(1)
    except Exception as e:
        print(f"Error reading .als file: {e}", file=sys.stderr)
        sys.exit(1)

    root = tree.getroot()
    liveset = root.find("LiveSet")
    if liveset is None:
        print("Error: No LiveSet found in file", file=sys.stderr)
        sys.exit(1)

    # Project info
    creator = root.get("Creator", "Unknown")
    master_el = liveset.find("MainTrack")
    if master_el is None:
        master_el = liveset.find("MasterTrack")
    tempo = "?"
    if master_el is not None:
        tempo_el = master_el.find(".//Mixer/Tempo/Manual")
        if tempo_el is not None:
            try:
                tempo = f"{float(tempo_el.get('Value')):.0f}"
            except (TypeError, ValueError):
                pass

    project_info = {"version": creator, "tempo": tempo}

    # Get return track names first (needed for send labels)
    tracks_el = liveset.find("Tracks")
    if tracks_el is None:
        print("Error: No Tracks element found", file=sys.stderr)
        sys.exit(1)

    return_names = []
    for t in tracks_el:
        if t.tag == "ReturnTrack":
            name_el = t.find(".//Name/EffectiveName")
            return_names.append(
                name_el.get("Value") if name_el is not None else f"Return {chr(65 + len(return_names))}"
            )

    # Extract all tracks
    regular_tracks = []
    return_tracks = []
    for t in tracks_el:
        if t.tag == "ReturnTrack":
            return_tracks.append(extract_track(t, return_names))
        elif t.tag in ("AudioTrack", "MidiTrack", "GroupTrack"):
            regular_tracks.append(extract_track(t, return_names))

    # Extract master
    master_info = None
    if master_el is not None:
        vol_el = master_el.find(".//Mixer/Volume/Manual")
        master_devices_el = master_el.find("DeviceChain/DeviceChain/Devices")
        master_info = {
            "volume_db": vol_to_db(vol_el.get("Value")) if vol_el is not None else None,
            "devices": extract_devices(master_devices_el),
        }

    # Detect issues
    all_tracks = regular_tracks + return_tracks
    issues = detect_issues(regular_tracks, return_tracks, master_info)

    # Format and print
    output = format_output(project_info, regular_tracks, return_tracks, master_info, issues)
    print(output)


if __name__ == "__main__":
    main()
