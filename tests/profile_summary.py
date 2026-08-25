#!/usr/bin/env python3
"""Summarize profiling results from tests/profiles/ directory.

Usage:
    python -m tests.profile_summary [--profiles-dir DIR]
"""

from __future__ import annotations

import argparse
import pstats
from pathlib import Path


def summarize_cpu_profiles(profiles_dir: Path) -> None:
    prof_files = sorted(profiles_dir.glob("*.prof"))
    if not prof_files:
        print("No CPU profiles found.")
        return

    print(f"\n{'='*70}")
    print("CPU PROFILES SUMMARY")
    print(f"{'='*70}")

    for prof_path in prof_files:
        print(f"\n--- {prof_path.name} ---")
        stats = pstats.Stats(str(prof_path))
        stats.sort_stats("cumulative")
        print(f"  Total function calls: {stats.total_calls}")
        print(f"  Primitive calls: {stats.prim_calls}")
        print(f"  Total time: {stats.total_tt:.4f}s")
        print("\n  Top 10 functions by cumulative time:")
        stats.print_stats(10)


def summarize_memory_profiles(profiles_dir: Path) -> None:
    bin_files = sorted(profiles_dir.glob("*.bin"))
    if not bin_files:
        print("No memory profiles found.")
        return

    print(f"\n{'='*70}")
    print("MEMORY PROFILES SUMMARY")
    print(f"{'='*70}")

    try:
        import memray
    except ImportError:
        print("  memray not installed - cannot analyze .bin files")
        for bin_path in bin_files:
            size_kb = bin_path.stat().st_size / 1024
            print(f"  {bin_path.name}: {size_kb:.1f} KB")
        return

    for bin_path in bin_files:
        print(f"\n--- {bin_path.name} ---")
        size_kb = bin_path.stat().st_size / 1024
        print(f"  File size: {size_kb:.1f} KB")
        try:
            reader = memray.FileReader(str(bin_path))
            metadata = reader.metadata
            rss = getattr(metadata, "process_rss", None)
            peak = getattr(metadata, "peak_memory", None)
            if rss is not None:
                print(f"  Process RSS: {rss / 1024 / 1024:.1f} MB")
            if peak is not None:
                print(f"  Peak RSS: {peak / 1024 / 1024:.1f} MB")
        except Exception as e:
            print(f"  Could not read metadata: {e}")


def summarize_sampling_profiles(profiles_dir: Path) -> None:
    svg_files = sorted(profiles_dir.glob("*.svg"))
    if not svg_files:
        print("No sampling profiles found.")
        return

    print(f"\n{'='*70}")
    print("SAMPLING PROFILES (FLAME GRAPHS) SUMMARY")
    print(f"{'='*70}")

    for svg_path in svg_files:
        size_kb = svg_path.stat().st_size / 1024
        print(f"  {svg_path.name}: {size_kb:.1f} KB")


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize profiling results")
    parser.add_argument(
        "--profiles-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "profiles",
        help="Directory containing profile artifacts (default: tests/profiles/)",
    )
    args = parser.parse_args()

    profiles_dir = args.profiles_dir
    if not profiles_dir.exists():
        print(f"Profiles directory not found: {profiles_dir}")
        return

    print(f"Profiling results from: {profiles_dir}")
    summarize_cpu_profiles(profiles_dir)
    summarize_memory_profiles(profiles_dir)
    summarize_sampling_profiles(profiles_dir)

    total_files = len(list(profiles_dir.iterdir()))
    print(f"\n{'='*70}")
    print(f"Total profile artifacts: {total_files}")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
