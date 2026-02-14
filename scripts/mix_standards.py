#!/usr/bin/env python3
"""Evaluate an Ableton Live Set against industry mixing standards.

Scores a mix on gain staging, stereo image, dynamics, frequency balance,
effects usage, and master chain — all from the .als XML data alone.
"""

import sys
import gzip
import math
import xml.etree.ElementTree as ET


def vol_to_db(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v <= 0.0003163:
        return float("-inf")
    return 20 * math.log10(v)


def pan_to_pos(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0
    return v


def get_param(element, path):
    child = element.find(path)
    if child is None:
        return None
    manual = child.find("Manual")
    if manual is not None:
        return manual.get("Value")
    return child.get("Value")


DEVICE_NAMES = {
    "Eq8": "EQ Eight", "ChannelEq": "Channel EQ",
    "Compressor2": "Compressor", "GlueCompressor": "Glue Compressor",
    "Limiter": "Limiter", "MultibandDynamics": "Multiband Dynamics",
    "Reverb": "Reverb", "Delay": "Delay",
    "StereoGain": "Utility", "Saturator": "Saturator",
    "Gate": "Gate", "DrumBuss": "Drum Buss",
    "AutoFilter": "Auto Filter",
    "PluginDevice": "VST", "AuPluginDevice": "AU",
    "Vst3PluginDevice": "VST3",
}

EQ_TAGS = {"Eq8", "ChannelEq"}
COMP_TAGS = {"Compressor2", "GlueCompressor"}
LIMITER_TAGS = {"Limiter"}
PLUGIN_TAGS = {"PluginDevice", "AuPluginDevice", "Vst3PluginDevice"}


def extract_tracks(root):
    """Extract track info for scoring."""
    tracks_el = root.find(".//LiveSet/Tracks")
    if tracks_el is None:
        return [], [], []

    regular = []
    returns = []
    groups = []

    # Get return names for send labels
    return_names = []
    for t in tracks_el:
        if t.tag == "ReturnTrack":
            name_el = t.find(".//Name/EffectiveName")
            return_names.append(name_el.get("Value") if name_el is not None else "?")

    for t in tracks_el:
        name_el = t.find(".//Name/EffectiveName")
        name = name_el.get("Value") if name_el is not None else "?"

        mixer = t.find(".//DeviceChain/Mixer")
        vol_db = None
        pan_val = 0
        is_muted = False
        sends_active = 0
        sends_total = 0

        if mixer is not None:
            vol_el = mixer.find("Volume/Manual")
            if vol_el is not None:
                vol_db = vol_to_db(vol_el.get("Value"))
            pan_el = mixer.find("Pan/Manual")
            if pan_el is not None:
                pan_val = pan_to_pos(pan_el.get("Value"))
            speaker = mixer.find("Speaker/Manual")
            if speaker is not None:
                is_muted = speaker.get("Value", "true").lower() == "false"

            sends_el = mixer.find("Sends")
            if sends_el is not None:
                for sh in sends_el:
                    sends_total += 1
                    sv = sh.find("Send/Manual")
                    if sv is not None:
                        v = float(sv.get("Value", "0"))
                        if v > 0.0004:
                            sends_active += 1

        # Devices
        devices_el = t.find("DeviceChain/DeviceChain/Devices")
        device_tags = []
        device_info = []
        if devices_el is not None:
            for d in devices_el:
                device_tags.append(d.tag)
                info = {"tag": d.tag, "on": True}
                on_val = get_param(d, "On")
                if on_val is not None and str(on_val).lower() == "false":
                    info["on"] = False

                # Check compressor settings
                if d.tag in COMP_TAGS:
                    ratio = get_param(d, "Ratio")
                    threshold = get_param(d, "Threshold")
                    info["ratio"] = float(ratio) if ratio else None
                    info["threshold"] = float(threshold) if threshold else None

                device_info.append(info)

        track_data = {
            "name": name,
            "type": t.tag,
            "id": t.get("Id"),
            "vol_db": vol_db,
            "pan": pan_val,
            "muted": is_muted,
            "sends_active": sends_active,
            "sends_total": sends_total,
            "device_tags": device_tags,
            "device_info": device_info,
        }

        if t.tag == "ReturnTrack":
            returns.append(track_data)
        elif t.tag == "GroupTrack":
            groups.append(track_data)
        else:
            regular.append(track_data)

    return regular, returns, groups


def extract_master(root):
    master = root.find(".//LiveSet/MainTrack")
    if master is None:
        master = root.find(".//LiveSet/MasterTrack")
    if master is None:
        return None

    vol_el = master.find(".//Mixer/Volume/Manual")
    devices_el = master.find("DeviceChain/DeviceChain/Devices")
    device_tags = []
    if devices_el is not None:
        device_tags = [d.tag for d in devices_el]

    # Check limiter settings
    limiter_info = None
    if devices_el is not None:
        for d in devices_el:
            if d.tag == "Limiter":
                ceiling = get_param(d, "Ceiling")
                gain = get_param(d, "Gain")
                limiter_info = {
                    "ceiling": float(ceiling) if ceiling else None,
                    "gain": float(gain) if gain else None,
                }

    return {
        "vol_db": vol_to_db(vol_el.get("Value")) if vol_el is not None else None,
        "device_tags": device_tags,
        "limiter": limiter_info,
    }


def score_gain_staging(tracks, groups):
    """Score gain staging 0-100."""
    score = 100
    issues = []
    non_muted = [t for t in tracks if not t["muted"]]

    if not non_muted:
        return 0, ["No active tracks"]

    # Check how many tracks are at default 0.0 dB
    default_count = sum(1 for t in non_muted if t["vol_db"] is not None and abs(t["vol_db"]) < 0.05)
    default_pct = default_count / len(non_muted)
    if default_pct > 0.5:
        penalty = int(default_pct * 40)
        score -= penalty
        issues.append(f"{default_count}/{len(non_muted)} tracks at default 0.0 dB (-{penalty}pts)")
    elif default_pct > 0.2:
        penalty = int(default_pct * 20)
        score -= penalty
        issues.append(f"{default_count}/{len(non_muted)} tracks at default 0.0 dB (-{penalty}pts)")

    # Check for tracks too hot (above +3 dB)
    hot = [t["name"] for t in non_muted if t["vol_db"] is not None and t["vol_db"] > 3.0]
    if hot:
        score -= 10 * len(hot)
        issues.append(f"Tracks above +3 dB: {', '.join(hot)} (-{10*len(hot)}pts)")

    # Check for good range (-18 to -6)
    in_range = sum(1 for t in non_muted if t["vol_db"] is not None and -18 <= t["vol_db"] <= -6)
    range_pct = in_range / len(non_muted) if non_muted else 0
    if range_pct > 0.5:
        bonus = int(range_pct * 15)
        score = min(100, score + bonus)
        issues.append(f"{in_range}/{len(non_muted)} tracks in ideal -18 to -6 dB range (+{bonus}pts)")

    # Check group faders
    default_groups = sum(1 for g in groups if g["vol_db"] is not None and abs(g["vol_db"]) < 0.05)
    if groups and default_groups == len(groups):
        score -= 20
        issues.append(f"All {len(groups)} group faders at 0.0 dB (unused) (-20pts)")
    elif groups and default_groups > len(groups) * 0.5:
        score -= 10
        issues.append(f"{default_groups}/{len(groups)} group faders at 0.0 dB (-10pts)")
    elif groups and default_groups == 0:
        issues.append(f"All group faders adjusted (+0pts, good)")

    return max(0, min(100, score)), issues


def score_stereo_image(tracks):
    """Score stereo image 0-100."""
    score = 100
    issues = []
    non_muted = [t for t in tracks if not t["muted"]]

    if not non_muted:
        return 0, ["No active tracks"]

    center_count = sum(1 for t in non_muted if abs(t["pan"]) < 0.02)
    center_pct = center_count / len(non_muted)

    if center_pct > 0.85:
        score -= 50
        issues.append(f"{center_count}/{len(non_muted)} tracks at center — nearly mono mix (-50pts)")
    elif center_pct > 0.7:
        score -= 30
        issues.append(f"{center_count}/{len(non_muted)} tracks at center — narrow image (-30pts)")
    elif center_pct > 0.5:
        score -= 15
        issues.append(f"{center_count}/{len(non_muted)} tracks at center — could be wider (-15pts)")
    else:
        issues.append(f"Good stereo spread: {len(non_muted) - center_count}/{len(non_muted)} tracks panned off-center")

    # Check for extreme panning (>40)
    extreme = [t["name"] for t in non_muted if abs(t["pan"]) > 0.8]
    if extreme:
        score -= 5 * len(extreme)
        issues.append(f"Extreme panning (>40): {', '.join(extreme)} (-{5*len(extreme)}pts)")

    # Check bass elements are centered (by name heuristic)
    bass_names = {"sub", "bass", "kick", "low bass", "basssss"}
    for t in non_muted:
        if any(b in t["name"].lower() for b in bass_names) and abs(t["pan"]) > 0.1:
            score -= 10
            issues.append(f"Bass element '{t['name']}' panned off-center (-10pts)")

    return max(0, min(100, score)), issues


def score_dynamics(tracks):
    """Score dynamics processing 0-100."""
    score = 50  # Start at 50, gain points for good compression
    issues = []
    non_muted = [t for t in tracks if not t["muted"]]

    if not non_muted:
        return 0, ["No active tracks"]

    # Check for compression on key elements
    has_any_comp = False
    for t in non_muted:
        has_comp = any(d["tag"] in COMP_TAGS and d["on"] for d in t["device_info"])
        if has_comp:
            has_any_comp = True

        # Check compressor settings
        for d in t["device_info"]:
            if d["tag"] in COMP_TAGS and d["on"]:
                ratio = d.get("ratio")
                threshold = d.get("threshold")

                # Ratio of 0 or 1 = no compression
                if ratio is not None and ratio <= 1.01:
                    score -= 5
                    issues.append(f"'{t['name']}' compressor ratio {ratio:.1f} — not compressing (-5pts)")

                # Threshold at 0 dB = never engaging
                if threshold is not None and threshold > -1.0:
                    score -= 5
                    issues.append(f"'{t['name']}' compressor threshold {threshold:.1f} dB — too high to engage (-5pts)")

                # Good settings
                if ratio is not None and 2.0 <= ratio <= 6.0 and threshold is not None and threshold < -5.0:
                    score += 5
                    issues.append(f"'{t['name']}' compressor ratio {ratio:.1f}, threshold {threshold:.1f} dB — good (+5pts)")

    if not has_any_comp:
        score -= 20
        issues.append(f"No compression on any track (-20pts)")

    # Check for key elements by name
    for keyword, label in [("snare", "Snare"), ("vox", "Vocal"), ("vocal", "Vocal")]:
        matches = [t for t in non_muted if keyword in t["name"].lower()]
        for t in matches:
            has_comp = any(d["tag"] in COMP_TAGS and d["on"] for d in t["device_info"])
            if has_comp:
                score += 5
                issues.append(f"'{t['name']}' has compression — good for {label} (+5pts)")
            else:
                score -= 5
                issues.append(f"'{t['name']}' has no compression — {label} usually needs it (-5pts)")

    return max(0, min(100, score)), issues


def score_frequency_balance(tracks):
    """Score EQ usage 0-100."""
    score = 50
    issues = []
    non_muted = [t for t in tracks if not t["muted"]]

    if not non_muted:
        return 0, ["No active tracks"]

    has_eq_count = sum(1 for t in non_muted if any(d in EQ_TAGS for d in t["device_tags"]))
    eq_pct = has_eq_count / len(non_muted)

    if eq_pct == 0:
        score -= 30
        issues.append(f"No EQ on any track (-30pts)")
    elif eq_pct < 0.2:
        score -= 10
        issues.append(f"Only {has_eq_count}/{len(non_muted)} tracks have EQ (-10pts)")
    elif eq_pct > 0.4:
        score += 15
        issues.append(f"{has_eq_count}/{len(non_muted)} tracks have EQ — good coverage (+15pts)")
    else:
        score += 5
        issues.append(f"{has_eq_count}/{len(non_muted)} tracks have EQ (+5pts)")

    # Check for tracks with no processing at all
    empty = [t["name"] for t in non_muted if len(t["device_tags"]) == 0]
    if empty:
        score -= 3 * len(empty)
        issues.append(f"Tracks with no devices: {', '.join(empty[:5])} (-{3*len(empty)}pts)")

    return max(0, min(100, score)), issues


def score_effects_sends(tracks, returns):
    """Score effects and send usage 0-100."""
    score = 50
    issues = []
    non_muted = [t for t in tracks if not t["muted"]]

    if not returns:
        score -= 20
        issues.append(f"No return tracks (-20pts)")
        return max(0, min(100, score)), issues

    # Check if sends are being used
    tracks_with_sends = sum(1 for t in non_muted if t["sends_active"] > 0)
    if tracks_with_sends == 0:
        score -= 30
        issues.append(f"Return tracks exist but no sends active — unused returns (-30pts)")
    elif tracks_with_sends < len(non_muted) * 0.2:
        score -= 10
        issues.append(f"Only {tracks_with_sends}/{len(non_muted)} tracks use sends (-10pts)")
    elif tracks_with_sends > len(non_muted) * 0.3:
        score += 20
        issues.append(f"{tracks_with_sends}/{len(non_muted)} tracks use sends — good (+20pts)")
    else:
        score += 10
        issues.append(f"{tracks_with_sends}/{len(non_muted)} tracks use sends (+10pts)")

    # Check return tracks have appropriate devices
    for r in returns:
        has_reverb = "Reverb" in r["device_tags"]
        has_delay = "Delay" in r["device_tags"]
        has_comp = any(t in COMP_TAGS for t in r["device_tags"])
        if has_reverb or has_delay or has_comp:
            score += 5
            issues.append(f"Return '{r['name']}' has processing — good (+5pts)")

    return max(0, min(100, score)), issues


def score_master(master):
    """Score master chain 0-100."""
    score = 50
    issues = []

    if master is None:
        return 0, ["No master track found"]

    has_limiter = any(t in LIMITER_TAGS for t in master["device_tags"])
    has_eq = any(t in EQ_TAGS for t in master["device_tags"])
    has_comp = any(t in COMP_TAGS for t in master["device_tags"])

    if has_limiter:
        score += 25
        issues.append(f"Limiter present on master (+25pts)")

        if master["limiter"]:
            ceiling = master["limiter"]["ceiling"]
            if ceiling is not None:
                if -1.5 <= ceiling <= -0.1:
                    score += 10
                    issues.append(f"Limiter ceiling at {ceiling:.1f} dB — good range (+10pts)")
                elif ceiling > 0:
                    score -= 10
                    issues.append(f"Limiter ceiling at {ceiling:.1f} dB — above 0, will clip (-10pts)")
    else:
        score -= 25
        issues.append(f"No limiter on master (-25pts)")

    if has_eq:
        score += 5
        issues.append(f"EQ on master (+5pts)")

    # Check for metering (Spectrum, etc.)
    has_meter = "SpectrumAnalyzer" in master["device_tags"] or "Spectrum" in master.get("device_tags", [])
    # Check for plugin metering
    has_plugin = any(t in PLUGIN_TAGS for t in master["device_tags"])
    if has_meter or has_plugin:
        score += 5
        issues.append(f"Metering/analysis on master (+5pts)")

    if len(master["device_tags"]) == 0:
        score -= 20
        issues.append(f"Master chain is completely empty (-20pts)")

    return max(0, min(100, score)), issues


def overall_grade(total_score):
    if total_score >= 85:
        return "A", "Mix Ready — professional-level mixing decisions"
    elif total_score >= 70:
        return "B", "Solid Foundation — most fundamentals in place"
    elif total_score >= 55:
        return "C", "Getting There — key areas need attention"
    elif total_score >= 40:
        return "D", "Needs Work — significant mixing gaps"
    else:
        return "F", "Starting Out — major fundamentals missing"


def main():
    if len(sys.argv) != 2:
        print("Usage: python3 mix_standards.py <path/to/file.als>", file=sys.stderr)
        sys.exit(1)

    path = sys.argv[1]
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            tree = ET.parse(f)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    root = tree.getroot()
    creator = root.get("Creator", "Unknown")
    tempo_el = root.find(".//MainTrack/.//Mixer/Tempo/Manual")
    if tempo_el is None:
        tempo_el = root.find(".//MasterTrack/.//Mixer/Tempo/Manual")
    tempo = tempo_el.get("Value") if tempo_el is not None else "?"

    tracks, returns, groups = extract_tracks(root)
    master = extract_master(root)

    print(f"=== MIX STANDARDS CHECK ===")
    print(f"Version: {creator}")
    print(f"Tempo: {float(tempo):.0f} BPM" if tempo != "?" else "Tempo: ?")
    print(f"Tracks: {len(tracks)} regular, {len(groups)} groups, {len(returns)} returns")
    print()

    categories = []

    # Run all scoring
    s1, i1 = score_gain_staging(tracks, groups)
    categories.append(("Gain Staging", s1, i1))

    s2, i2 = score_stereo_image(tracks)
    categories.append(("Stereo Image", s2, i2))

    s3, i3 = score_dynamics(tracks)
    categories.append(("Dynamics", s3, i3))

    s4, i4 = score_frequency_balance(tracks)
    categories.append(("Frequency Balance", s4, i4))

    s5, i5 = score_effects_sends(tracks, returns)
    categories.append(("Effects & Sends", s5, i5))

    s6, i6 = score_master(master)
    categories.append(("Master Chain", s6, i6))

    total = sum(s for _, s, _ in categories)
    max_total = len(categories) * 100
    pct = int((total / max_total) * 100)

    for name, score, issues in categories:
        bar_len = score // 5
        bar = "█" * bar_len + "░" * (20 - bar_len)
        print(f"  {name:>20}: {bar} {score}/100")
        for issue in issues:
            print(f"                        {issue}")
        print()

    grade, label = overall_grade(pct)
    print(f"=== OVERALL: {pct}% — Grade {grade} ===")
    print(f"{label}")


if __name__ == "__main__":
    main()
