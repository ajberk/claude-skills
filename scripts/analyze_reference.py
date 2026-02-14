#!/usr/bin/env python3
"""Analyze a mastered audio reference track for mixing comparison."""

import sys
import subprocess
import json
import os
import math
import wave
import struct
import tempfile


def get_audio_info(path):
    """Get basic audio file info using ffprobe."""
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return json.loads(result.stdout)


def get_loudness(path):
    """Get LUFS, true peak, and loudness range using ffmpeg loudnorm filter."""
    cmd = [
        "ffmpeg", "-i", path, "-af",
        "loudnorm=I=-14:TP=-1:LRA=11:print_format=json",
        "-f", "null", "-"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    # loudnorm outputs JSON to stderr
    output = result.stderr
    # Find the JSON block in the output
    json_start = output.rfind("{")
    json_end = output.rfind("}") + 1
    if json_start == -1 or json_end == 0:
        return None
    try:
        return json.loads(output[json_start:json_end])
    except json.JSONDecodeError:
        return None


def get_spectral_balance(path):
    """Get energy in frequency bands using ffmpeg's astats and multiple bandpass filters."""
    bands = [
        ("Sub", 20, 60),
        ("Low Bass", 60, 150),
        ("Mid Bass", 150, 300),
        ("Low Mids", 300, 600),
        ("Mids", 600, 1200),
        ("Upper Mids", 1200, 3000),
        ("Presence", 3000, 6000),
        ("Brilliance", 6000, 12000),
        ("Air", 12000, 20000),
    ]

    results = {}
    for name, low, high in bands:
        cmd = [
            "ffmpeg", "-i", path, "-af",
            f"highpass=f={low},lowpass=f={high},astats=metadata=1:reset=0",
            "-f", "null", "-"
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        output = result.stderr
        # Extract RMS level from astats output
        for line in output.split("\n"):
            if "RMS level dB" in line and "Overall" in line:
                try:
                    val = line.split("RMS level dB:")[1].strip()
                    results[name] = float(val)
                except (IndexError, ValueError):
                    pass
                break
            elif "RMS level dB:" in line:
                try:
                    val = line.split("RMS level dB:")[1].strip()
                    results[name] = float(val)
                except (IndexError, ValueError):
                    pass

    return results


def get_stereo_info(path):
    """Get stereo width/correlation info."""
    cmd = [
        "ffmpeg", "-i", path, "-af",
        "astats=metadata=1:reset=0",
        "-f", "null", "-"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    output = result.stderr

    info = {}
    lines = output.split("\n")
    for i, line in enumerate(lines):
        if "Peak level dB:" in line:
            try:
                val = line.split("Peak level dB:")[1].strip()
                if "Overall" in lines[max(0, i-3):i+1][-1] if lines[max(0, i-3):i+1] else "":
                    info["peak_db"] = float(val)
                elif "peak_db" not in info:
                    info.setdefault("channel_peaks", []).append(float(val))
            except (IndexError, ValueError):
                pass
        elif "RMS level dB:" in line:
            try:
                val = line.split("RMS level dB:")[1].strip()
                info.setdefault("rms_values", []).append(float(val))
            except (IndexError, ValueError):
                pass
        elif "Crest factor:" in line:
            try:
                val = line.split("Crest factor:")[1].strip()
                info.setdefault("crest_factors", []).append(float(val))
            except (IndexError, ValueError):
                pass
        elif "Flat factor:" in line:
            try:
                val = line.split("Flat factor:")[1].strip()
                info.setdefault("flat_factors", []).append(float(val))
            except (IndexError, ValueError):
                pass
        elif "Dynamic range:" in line:
            try:
                val = line.split("Dynamic range:")[1].strip()
                info.setdefault("dynamic_range", []).append(float(val))
            except (IndexError, ValueError):
                pass

    return info


def get_dynamic_profile(path):
    """Get loudness over time to understand dynamic arc."""
    # Get duration first
    info = get_audio_info(path)
    if not info:
        return None
    duration = float(info.get("format", {}).get("duration", 0))
    if duration == 0:
        return None

    # Measure loudness in 8 equal segments
    segment_count = 8
    segment_duration = duration / segment_count
    segments = []

    for i in range(segment_count):
        start = i * segment_duration
        cmd = [
            "ffmpeg", "-ss", str(start), "-t", str(segment_duration),
            "-i", path, "-af",
            "astats=metadata=1:reset=0",
            "-f", "null", "-"
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        output = result.stderr
        rms = None
        for line in output.split("\n"):
            if "RMS level dB:" in line:
                try:
                    val = line.split("RMS level dB:")[1].strip()
                    rms = float(val)
                except (IndexError, ValueError):
                    pass
        if rms is not None:
            pct_start = int((i / segment_count) * 100)
            pct_end = int(((i + 1) / segment_count) * 100)
            segments.append((f"{pct_start}-{pct_end}%", rms))

    return segments, duration


def format_output(path, audio_info, loudness, spectral, stereo, dynamics):
    """Format all analysis into structured text."""
    lines = []
    filename = os.path.basename(path)
    lines.append(f"=== REFERENCE TRACK ANALYSIS: {filename} ===")

    # Basic info
    if audio_info:
        fmt = audio_info.get("format", {})
        duration = float(fmt.get("duration", 0))
        mins = int(duration // 60)
        secs = int(duration % 60)
        bit_rate = int(fmt.get("bit_rate", 0)) // 1000
        lines.append(f"Duration: {mins}:{secs:02d}")
        lines.append(f"Bitrate: {bit_rate} kbps")

        for stream in audio_info.get("streams", []):
            if stream.get("codec_type") == "audio":
                lines.append(f"Format: {stream.get('codec_name', '?')} / {stream.get('sample_rate', '?')}Hz / {stream.get('channels', '?')}ch")
                if stream.get("bits_per_raw_sample"):
                    lines.append(f"Bit Depth: {stream['bits_per_raw_sample']}-bit")
                break

    # Loudness
    if loudness:
        lines.append("")
        lines.append("=== LOUDNESS ===")
        lines.append(f"Integrated LUFS: {loudness.get('input_i', '?')} LUFS")
        lines.append(f"True Peak: {loudness.get('input_tp', '?')} dBTP")
        lines.append(f"Loudness Range (LRA): {loudness.get('input_lra', '?')} LU")
        lines.append(f"Threshold: {loudness.get('input_thresh', '?')} LUFS")

    # Stereo / dynamics info
    if stereo:
        lines.append("")
        lines.append("=== DYNAMICS ===")
        channel_peaks = stereo.get("channel_peaks", [])
        if len(channel_peaks) >= 2:
            lines.append(f"Peak L: {channel_peaks[0]:.1f} dB | Peak R: {channel_peaks[1]:.1f} dB")
            peak_diff = abs(channel_peaks[0] - channel_peaks[1])
            if peak_diff > 1.0:
                lines.append(f"  L/R peak imbalance: {peak_diff:.1f} dB")

        rms = stereo.get("rms_values", [])
        if len(rms) >= 3:
            lines.append(f"RMS L: {rms[0]:.1f} dB | RMS R: {rms[1]:.1f} dB | Overall: {rms[2]:.1f} dB")

        crest = stereo.get("crest_factors", [])
        if crest:
            avg_crest = sum(crest) / len(crest)
            lines.append(f"Crest Factor: {avg_crest:.1f} (peak-to-RMS ratio)")
            if avg_crest < 6:
                lines.append("  -> Heavily limited/compressed master")
            elif avg_crest < 10:
                lines.append("  -> Moderate compression, typical for electronic music")
            else:
                lines.append("  -> Dynamic master with lots of headroom")

    # Spectral balance
    if spectral:
        lines.append("")
        lines.append("=== SPECTRAL BALANCE ===")
        # Normalize relative to the loudest band
        max_val = max(spectral.values()) if spectral.values() else 0
        for name, val in spectral.items():
            relative = val - max_val
            bar_len = max(0, int((val + 60) / 2))  # rough visual bar
            bar = "#" * bar_len
            lines.append(f"  {name:>12}: {val:+.1f} dB (rel: {relative:+.1f}) {bar}")

    # Dynamic profile over time
    if dynamics:
        segments, duration = dynamics
        lines.append("")
        lines.append("=== ENERGY OVER TIME ===")
        for label, rms in segments:
            bar_len = max(0, int((rms + 40) * 1.5))
            bar = "#" * bar_len
            lines.append(f"  {label:>8}: {rms:+.1f} dB {bar}")

    return "\n".join(lines)


def main():
    if len(sys.argv) != 2:
        print("Usage: python3 analyze_reference.py <path/to/audio.wav|mp3>", file=sys.stderr)
        sys.exit(1)

    path = sys.argv[1]
    if not os.path.exists(path):
        print(f"Error: File not found: {path}", file=sys.stderr)
        sys.exit(1)

    # Check ffmpeg is available
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
    except FileNotFoundError:
        print("Error: ffmpeg is required but not installed. Run: brew install ffmpeg", file=sys.stderr)
        sys.exit(1)

    print(f"Analyzing reference track... (this takes a moment)", file=sys.stderr)

    audio_info = get_audio_info(path)
    loudness = get_loudness(path)
    spectral = get_spectral_balance(path)
    stereo = get_stereo_info(path)
    dynamics = get_dynamic_profile(path)

    output = format_output(path, audio_info, loudness, spectral, stereo, dynamics)
    print(output)


if __name__ == "__main__":
    main()
