import sqlite3
db=sqlite3.connect("/vol1/@appdata/nas-search/find_index_vol4.db")
rows=db.execute("SELECT sql FROM sqlite_master WHERE type='table'").fetchall()
for r in rows:
    print(r[0])
