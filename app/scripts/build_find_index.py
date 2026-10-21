#!/usr/bin/env python3
"""Build FTS5 full-text index for all volumes. Outputs to .tmp then mv replaces."""
import os, sqlite3, sys, time

ROOTS = ["/vol1", "/vol2", "/vol3", "/vol4"]
# 任意层级都剪掉的（fnOS 私有 / 应用目录 / 回收站）
SKIP_ANY = {".@__thumb", ".@#local", ".@#recycle", ".@#snap", ".@db", ".@iscsi", ".@upload",
            "@appdata", "@appmeta", "@appbackup", "@appcenter", "@appconf",
            "lost+found", ".Trash-0"}
# 只在卷根剪掉的通用名（用户子目录里叫 docker/thumb 不能被误删，2026-09-13 实测修正）
SKIP_ROOT = {"docker", "thumb", "trim.media"}
MIN_SIZE = 0


def scan(root, db_path):
    tmp_db = db_path + ".tmp"
    # Always write to .tmp first, so no lock conflict with running app
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, name TEXT, size INTEGER)")
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(path, name, size)")
    conn.execute("CREATE TABLE IF NOT EXISTS dirs (path TEXT PRIMARY KEY, name TEXT)")

    count = 0
    dircount = 0
    t0 = time.time()
    print(f"  Scanning {root} -> {tmp_db}")
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        if dirpath == root:
            dirnames[:] = [d for d in dirnames if d not in SKIP_ANY
                           and d not in SKIP_ROOT and not d.startswith(".")]
        else:
            dirnames[:] = [d for d in dirnames if d not in SKIP_ANY
                           and not d.startswith(".")]
        # index directory itself (skip the volume root)
        if dirpath != root:
            conn.execute("INSERT OR REPLACE INTO dirs VALUES (?, ?)",
                         (dirpath, os.path.basename(dirpath)))
            dircount += 1
        for fname in filenames:
            fpath = os.path.join(dirpath, fname)
            try:
                st = os.stat(fpath)
                if st.st_size < MIN_SIZE:
                    continue
                rel = fpath
                conn.execute(
                    "INSERT OR REPLACE INTO files VALUES (?, ?, ?)",
                    (rel, fname, st.st_size),
                )
                conn.execute(
                    "INSERT INTO files_fts VALUES (?, ?, ?)",
                    (rel, fname, st.st_size),
                )
                count += 1
                if count % 10000 == 0:
                    conn.commit()
                    elapsed = time.time() - t0
                    print(f"    {count} files ({elapsed:.1f}s)...")
            except (OSError, PermissionError):
                pass
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()

    # Replace old db with new one atomically
    try:
        os.remove(db_path)
    except FileNotFoundError:
        pass
    os.rename(tmp_db, db_path)

    # Remove WAL/SHM leftover from tmp
    for suffix in ["-wal", "-shm"]:
        try:
            os.remove(db_path + suffix)
        except FileNotFoundError:
            pass

    elapsed = time.time() - t0
    print(f"  DONE {count} files in {elapsed:.1f}s")
    return count


def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <root> <db_path>")
        sys.exit(1)
    root = sys.argv[1]
    db_path = sys.argv[2]
    scan(root, db_path)


if __name__ == "__main__":
    main()
