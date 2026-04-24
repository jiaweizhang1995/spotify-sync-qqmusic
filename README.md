# Spotify → QQ 音乐 每日同步

每天自动把一个 Spotify 歌单里的新歌同步到 QQ 音乐的同名歌单。单向、只追加、不删歌。

## 它做什么

- 每天定时跑一次（默认 UTC 19:00 / 北京 03:00）
- 读 Spotify 源歌单
- 在 QQ 音乐里搜同一首歌
- 把新增的加到 QQ 音乐目标歌单
- 匹配不上的写到 `data/unmatched.txt`，人工兜底

## 需要准备

- Python 3.12+
- Spotify 开发者账号（拿 Client ID / Secret）
- QQ 音乐账号（扫码登录）

## 本地跑一次

```bash
# 1. 装依赖
make install

# 2. 复制配置
cp .env.example .env

# 3. 拿 Spotify refresh token（一次性，会开浏览器）
make bootstrap-spotify
# 按提示把 SPOTIFY_CLIENT_ID / SECRET 填进 .env，跑完会打印 refresh token

# 4. 扫码登录 QQ 音乐（一次性）
make bootstrap-qq
# 用手机 QQ 音乐扫终端里的二维码，跑完会打印 QQ_CREDENTIAL_JSON

# 5. 设置要同步的歌单名（Spotify 和 QQ 两边都要有同名歌单，没有会自动建 QQ 那边）
python -m src.main playlists

# 6. 先预览，不写 QQ
make dry-run

# 7. 真跑
make sync
```

跑完看 `data/sync.log` 和 `data/unmatched.txt`。

## .env 字段

| 字段 | 说明 |
|---|---|
| `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` | Spotify 开发者后台拿 |
| `SPOTIFY_REFRESH_TOKEN` | `bootstrap-spotify` 生成 |
| `SPOTIFY_PLAYLIST_NAME` | 源歌单名 |
| `QQ_PLAYLIST_NAME` | 目标歌单名（不存在会新建） |
| `QQ_CREDENTIAL_JSON` | `bootstrap-qq` 生成 |
| `MIRROR_DELETE_THRESHOLD` | 镜像模式安全阀（默认 0.2，当前用不到） |

## 让它每天自己跑（GitHub Actions）

仓库里已经有 `.github/workflows/sync.yml`。只要把 `.env` 里的值作为 Repository Secrets 配到 GitHub：

- `SPOTIFY_CLIENT_ID`
- `SPOTIFY_CLIENT_SECRET`
- `SPOTIFY_REFRESH_TOKEN`
- `SPOTIFY_PLAYLIST_NAME`
- `QQ_PLAYLIST_NAME`
- `QQ_CREDENTIAL_JSON`
- `GH_PAT_SECRETS_WRITE`（fine-grained PAT，权限 `secrets:write`，用于 QQ musickey 过期时自动写回）

配好后每天 UTC 19:00 自动跑，也能在 Actions 页面手点 `Run workflow` 触发（勾选 `dry_run` 可预览）。

运行产物：
- `data/` 分支：SQLite 库、日志、未匹配清单（自动 commit 回去）
- Actions artifacts：`sync.log` + `unmatched.txt`（保留 30 天）

## 常用命令

```bash
make install              # 装依赖
make bootstrap-spotify    # Spotify 首次授权
make bootstrap-qq         # QQ 音乐扫码登录
make dry-run              # 预览不写
make sync                 # 真同步
make test                 # 跑测试
python -m src.main playlists -s "源歌单" -q "目标歌单"   # 改歌单名
```

## 常见问题

**QQ 登录态过期？**  
重新跑 `make bootstrap-qq`，把新的 `QQ_CREDENTIAL_JSON` 更新到 `.env`（或 GitHub Secret）。GitHub Actions 里如果配了 `GH_PAT_SECRETS_WRITE`，musickey 刷新会自动写回 Secret。

**有歌没同步过去？**  
看 `data/unmatched.txt`。跨平台 metadata 对不上是常态，手动加即可。

**不想删歌？**  
默认就不删（append 模式）。Mirror 模式暂未启用。

## 项目结构

```
src/
  main.py            # CLI 入口
  config.py          # 读 .env
  spotify_client.py  # Spotify API
  qqmusic_client.py  # QQ 音乐 API（社区库 qqmusic-api-python）
  matcher.py         # 跨平台歌曲匹配
  diff_engine.py     # 算差异
  sync_service.py    # 主流程
  db.py              # SQLite
  report.py          # 同步报告
scripts/
  bootstrap_spotify.py
  bootstrap_qq_login.py
tests/
data/                # 运行时产物（sync.db / sync.log / unmatched.txt）
```

## 参考

- Spotify Web API: https://developer.spotify.com/documentation/web-api
- qqmusic-api-python: https://pypi.org/project/qqmusic-api-python/
