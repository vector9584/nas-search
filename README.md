# nas-search

> 给 NAS 做的全盘文件名秒搜 —— Everything 的轻量替代。

[中文](#) · 面向 [fnOS](https://www.fnnas.com/) 微应用环境

![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)
![SQLite FTS5](https://img.shields.io/badge/SQLite-FTS5-003B57?logo=sqlite&logoColor=white)
![No Dependencies](https://img.shields.io/badge/dependencies-none-success)

## 解决什么问题

NAS 上堆到几百万个文件之后，系统自带的搜索要么只能搜某个共享目录，要么慢到没法用。想找一个三个月前丢进去的 PDF，得靠翻目录。

这个项目把文件名预先建成 SQLite FTS5 索引，搜索走全文索引而不是实时遍历目录，输入关键词就能毫秒级出结果。**只索引文件名，不索引路径、不读文件内容** —— 索引小、建得快、也不会去碰你的文件。

## 功能

- 分卷独立索引（`/vol1` ~ `/vol4` 各一个库），单卷重建不影响其他卷
- 毫秒级搜索，结果按相关度排序，支持分页（20 条/页）与文件类型筛选
- 定时重建 / 手动重建，带实时进度
- 移动端与 PC 自适应，UI 内嵌在服务端，单文件部署
- 索引重建用 `.tmp` 副本再原子替换，重建时旧索引仍可搜索

## 技术实现

**零第三方依赖**：只用了 Python 标准库 —— `http.server`、`socketserver`、`sqlite3`、`os.walk`。目标机器上不需要 pip，也不需要虚拟环境。

几个值得说的细节：

1. **Unix socket 上跑 HTTP**：Python 3.11 的 `http.server` 只支持 TCP，这里把 `socketserver.UnixStreamServer` 和 `http.server.HTTPServer` 拼起来，监听应用目录下的 socket 文件，由 NAS 网关转发。省掉端口占用，也避开防火墙。

2. **只搜文件名，不搜路径**：FTS5 里 `path` 和 `name` 都进了索引，但查询用双重 MATCH 限定到 `name` 列：
   ```sql
   SELECT path, size, rank FROM files_fts
   WHERE files_fts MATCH '"关键词"' AND name MATCH '"关键词"'
   ORDER BY rank LIMIT 200
   ```
   只用第一层 MATCH 会把「路径里含关键词、但文件名无关」的文件一起捞出来，搜索质量明显变差。

3. **重建不锁库**：全量扫描写进 `find_index_volN.db.tmp`，写完后 `mv` 原子替换。旧库在重建全程可读，前端不会因为重建而搜索失败。

4. **WAL 收尾必须 checkpoint**：SQLite WAL 模式下进程退出时若没 checkpoint，最后一批写入会丢。重建脚本结尾强制 `PRAGMA wal_checkpoint(TRUNCATE)` —— 这个坑不踩一次想不到。

5. **裁剪目录要分两层**：`.@#local`、`@appdata` 这类 fnOS 私有目录**任何层级**都要剪掉；但 `docker`、`thumb` 这种通用名**只在卷根剪掉** —— 用户自己的子目录里很可能就叫 `docker`，一起剪了就会漏索引。

6. **网关前缀**：请求进来带着 `/app/nas-search` 前缀，服务端用 `urlparse` 剥掉；前端 `API_BASE` 按 `location.pathname` 动态算，兼容 `/app/nas-search`、`/app/nas-search/`、`/app/nas-search/index.html` 三种形态。

## 目录结构

```
manifest                      # 应用元数据（.cfg 格式，不是 JSON）
app/
├── server/server.py          # HTTP 服务 + 内嵌前端（单文件）
├── scripts/build_find_index.py  # 全盘索引重建
├── cmd/main                  # 生命周期脚本（start/stop/status）
├── config/privilege          # 权限声明
├── config/resource           # 资源声明
└── ui/config, ui/images/     # 网关 socket 配置与图标
cmd/                          # 安装/升级/卸载/配置回调
```

## 运行要求

**需要 fnOS 环境**。服务端依赖 fnOS 微应用的约定：`TRIM_APPDEST`、`TRIM_PKGVAR` 等环境变量、网关 socket 转发、`/vol1/@appdata` 数据目录。脱离 fnOS 单独跑意义不大 —— 它不是通用跨平台搜索工具。

如果只想看核心逻辑，读 `app/server/server.py`（869 行，含前端）和 `app/scripts/build_find_index.py` 这两个文件就够了。

## 打包成 fpk

用 Python 直接打，比 `fnpack build` 可控（`fnpack` 会消费掉 `app/` 目录内容）：

```python
import tarfile, gzip, os

with tarfile.open("app.tgz", "w:gz") as tar:
    for root, dirs, files in os.walk("app"):
        for fn in files:
            p = os.path.join(root, fn)
            tar.add(p, arcname=os.path.relpath(p, "app"))

with open("app.tgz", "rb") as f:
    data = f.read()
with gzip.open("nas-search.fpk", "wb", compresslevel=9) as gz:
    gz.write(data)
```

注意：`manifest` 必须是 `.cfg` 的 `key=value` 格式，写成 JSON 版本号会显示异常；同版本号覆盖安装会被应用中心拒绝，每次打包都要升 `version`。

## License

MIT
