"""OS boot identity independent of wall clock, NTP and suspend duration."""
from pathlib import Path
import subprocess
import sys
from functools import lru_cache


@lru_cache(maxsize=1)
def host_boot_id():
    if sys.platform == "linux":
        return "linux:"+Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    if sys.platform == "darwin":
        result = subprocess.run(["/usr/sbin/sysctl","-n","kern.bootsessionuuid"],text=True,capture_output=True)
        if result.returncode == 0:
            return "darwin:"+result.stdout.strip()
    return None
