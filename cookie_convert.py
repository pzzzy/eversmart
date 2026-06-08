#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
from http.cookiejar import MozillaCookieJar
from datetime import datetime
import time


def parse_cookieinfo(path: str | Path, out: str | Path) -> Path:
    """Convert Safari/Chrome exported cookieinfo.txt tabular copy to Netscape cookies.txt."""
    path = Path(path)
    out = Path(out)
    jar = MozillaCookieJar(str(out))
    # Write manually: domain flag path secure expires name value
    lines = ["# Netscape HTTP Cookie File\n"]
    now = int(time.time())
    for raw in path.read_text().splitlines():
        if not raw.strip():
            continue
        parts = raw.split("\t")
        if len(parts) < 8:
            continue
        name, value, domain, cookie_path, expires, size, secure, httponly, *rest = parts
        include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
        secure_flag = "TRUE" if secure.strip() == "✓" else "FALSE"
        exp = "0"
        if expires and expires != "Session":
            for fmt in ("%m/%d/%Y, %I:%M:%S %p", "%m/%d/%y, %I:%M:%S %p"):
                try:
                    exp = str(int(datetime.strptime(expires, fmt).timestamp()))
                    break
                except ValueError:
                    pass
        lines.append("\t".join([domain, include_subdomains, cookie_path or "/", secure_flag, exp, name, value]) + "\n")
    out.write_text("".join(lines))
    return out


if __name__ == "__main__":
    p = parse_cookieinfo("cookieinfo.txt", ".eversource_cookies.txt")
    print(p)
