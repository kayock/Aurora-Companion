#!/usr/bin/env python3
"""Where does plain extractall() actually put these members on THIS Python?"""
import io, os, sys, tarfile, tempfile
from pathlib import Path

tmp = Path(tempfile.mkdtemp(prefix="tar-sem-"))
staging = tmp / "staging"; staging.mkdir()
print("python:", sys.version)

with tarfile.open(tmp / "t.tar", "w") as tf:
    def add(name, typ, data="", **kw):
        ti = tarfile.TarInfo(name); ti.type = typ; ti.mode = 0o644
        if typ == tarfile.REGTYPE:
            ti.size = len(data)
        ti.linkname = kw.get("link", "")
        if data or typ == tarfile.REGTYPE:
            tf.addfile(ti, io.BytesIO(data.encode()) if data else None)
        else:
            tf.addfile(ti)
    add("app/run.py", tarfile.REGTYPE, "x\n")
    add("app/../../TRAV2.txt", tarfile.REGTYPE, "t\n")       # 2 levels up
    add("app/../../../TRAV3.txt", tarfile.REGTYPE, "t\n")    # 3 levels up
    add("app/../../../../TRAV4.txt", tarfile.REGTYPE, "t\n") # 4 levels up
    add("/ABS.txt", tarfile.REGTYPE, "a\n")                  # absolute
    add("app/link", tarfile.SYMTYPE, link="/etc/passwd")     # symlink
    add("app/hard", tarfile.LNKTYPE, link="/etc/passwd")     # hardlink
    add("app/fifo", tarfile.FIFOTYPE)                        # fifo

try:
    with tarfile.open(tmp / "t.tar") as tf:
        tf.extractall(staging)
    print("extractall: OK (no exception)")
except Exception as e:
    print("extractall raised:", type(e).__name__, e)

for p in sorted(tmp.rglob("*")):
    kind = "L" if p.is_symlink() else ("d" if p.is_dir() else "f")
    print(f"  {kind} {p.relative_to(tmp)}" + (f" -> {os.readlink(p)}" if p.is_symlink() else ""))
print("outside candidates:")
for name in ("TRAV2.txt", "TRAV3.txt", "TRAV4.txt", "ABS.txt"):
    print(f"  {name}: tmp={ (tmp/name).exists() } parent={ (tmp.parent/name).exists() }")
