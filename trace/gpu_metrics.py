"""Sample the amdgpu gpu_metrics table (format v3.0, the 860M's) whenever it refreshes.

The SMU refreshes the table about every 6 ms and clocks in it are filtered averages
(there is no instantaneous gfxclk in v3.0), so this shows DPM ramps at ~6 ms resolution.
Polls every 1 ms and prints one CSV row per refresh:

    t_mono_ns,smu_boot_ns,gfxclk_mhz,socclk_mhz,fclk_mhz,uclk_mhz,gfx_activity_pct,gfx_power_mw

t_mono_ns is CLOCK_MONOTONIC (same clock as the traces); smu_boot_ns is the driver's
timestamp of the table (system_clock_counter, CLOCK_BOOTTIME).

    python3 trace/gpu_metrics.py [--card /sys/class/drm/card1] > gpu_metrics.csv
"""

import argparse
import ctypes as C
import glob
import signal
import sys
import time

u8, u16, u32, u64 = C.c_uint8, C.c_uint16, C.c_uint32, C.c_uint64


class MetricsV3(C.Structure):
    """Prefix of struct gpu_metrics_v3_0 (include/linux/kgd_pp_interface.h)."""
    _fields_ = [
        ("structure_size", u16), ("format_revision", u8), ("content_revision", u8),
        ("temperature_gfx", u16), ("temperature_soc", u16), ("temperature_core", u16 * 16),
        ("temperature_skin", u16),
        ("average_gfx_activity", u16), ("average_vcn_activity", u16),
        ("average_ipu_activity", u16 * 8), ("average_core_c0_activity", u16 * 16),
        ("average_dram_reads", u16), ("average_dram_writes", u16),
        ("average_ipu_reads", u16), ("average_ipu_writes", u16),
        ("system_clock_counter", u64),
        ("average_socket_power", u32), ("average_ipu_power", u16), ("average_apu_power", u32),
        ("average_gfx_power", u32), ("average_dgpu_power", u32), ("average_all_core_power", u32),
        ("average_core_power", u16 * 16), ("average_sys_power", u16),
        ("stapm_power_limit", u16), ("current_stapm_power_limit", u16),
        ("average_gfxclk_frequency", u16), ("average_socclk_frequency", u16),
        ("average_vpeclk_frequency", u16), ("average_ipuclk_frequency", u16),
        ("average_fclk_frequency", u16), ("average_vclk_frequency", u16),
        ("average_uclk_frequency", u16),
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--card", default=None)
    args = ap.parse_args()
    card = args.card or sorted(glob.glob("/sys/class/drm/card[0-9]"))[0]
    path = f"{card}/device/gpu_metrics"
    first = MetricsV3.from_buffer_copy(open(path, "rb").read()[:C.sizeof(MetricsV3)])
    if first.format_revision != 3 or first.content_revision != 0:
        sys.exit(f"gpu_metrics v{first.format_revision}.{first.content_revision}, expected v3.0")

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    print("t_mono_ns,smu_boot_ns,gfxclk_mhz,socclk_mhz,fclk_mhz,uclk_mhz,"
          "gfx_activity_pct,gfx_power_mw")
    last = None
    try:
        with open(path, "rb", buffering=0) as f:
            while True:
                f.seek(0)
                m = MetricsV3.from_buffer_copy(f.read(C.sizeof(MetricsV3)))
                if m.system_clock_counter != last:
                    last = m.system_clock_counter
                    print(f"{time.clock_gettime_ns(time.CLOCK_MONOTONIC)},{last},"
                          f"{m.average_gfxclk_frequency},{m.average_socclk_frequency},"
                          f"{m.average_fclk_frequency},{m.average_uclk_frequency},"
                          f"{m.average_gfx_activity},{m.average_gfx_power}", flush=True)
                time.sleep(0.001)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
