# Spotify -> QQ 音乐每日自动同步方案

## 目标

实现一个**单向同步系统**：以 Spotify 歌单作为唯一真源，每天自动把新增或变更的歌曲同步到 QQ 音乐歌单中。

推荐先做：

- **单向同步**：`Spotify -> QQ音乐`
- **每日一次定时任务**
- **默认追加，不自动删除**
- **匹配失败输出清单，人工兜底**

---

## 总结结论

### Spotify

Spotify 适合作为上游数据源：

- 有官方 Web API
- 支持 OAuth 授权与 refresh token
- 支持读取歌单、创建歌单、增删歌单曲目

适合长期稳定跑自动任务。

### QQ 音乐

QQ 音乐公开可用的官方写歌单接口不适合直接做这个项目；更现实的实现方式是使用社区维护的 API 库，通过登录态完成：

- 登录
- 获取用户歌单
- 创建歌单
- 添加歌曲
- 删除歌曲

因此最佳技术路线是：

- **Spotify：官方 API**
- **QQ音乐：社区维护 API / 登录态方案**

---

## 推荐技术方案

### 方案 A：后端定时同步服务（推荐）

做一个小型同步服务，每天自动运行一次：

1. 读取 Spotify 源歌单
2. 标准化歌曲标题 / 歌手 / 时长
3. 在 QQ 音乐搜索对应歌曲
4. 计算差异
5. 更新 QQ 音乐目标歌单
6. 输出同步报告

推荐部署位置：

- VPS
- NAS
- Railway / Render / Fly.io
- GitHub Actions（低成本，但登录态维护略麻烦）

推荐语言：

- **Python**

原因：

- Spotify SDK 和 HTTP 调用都成熟
- QQ 音乐社区库在 Python 侧可用
- 做 cron、SQLite、日志很方便

### 方案 B：半自动同步（更稳）

流程改为：

1. 每天自动拉 Spotify 变化
2. 先生成“待同步歌曲清单”
3. 你点确认后，再写入 QQ 音乐

适合下面场景：

- 害怕误匹配
- 害怕自动删歌
- 害怕 QQ 音乐登录态/风控不稳定

### 方案 C：UI 自动化（不推荐）

用 Playwright 或 Appium 控制 QQ 音乐网页 / App 完成加歌。

缺点：

- 页面一改就坏
- 维护成本高
- 长期稳定性差

---

## 推荐系统架构

### 1. Source of Truth

- Spotify 歌单为唯一真源

### 2. Target

- QQ 音乐中的一个目标歌单

### 3. 存储

用 SQLite 即可。

建议数据表：

#### `playlist_map`

记录歌单映射：

- `spotify_playlist_id`
- `spotify_playlist_name`
- `qq_playlist_id`
- `qq_playlist_name`
- `sync_mode`（append / mirror）

#### `track_map_cache`

记录跨平台歌曲映射：

- `spotify_track_id`
- `spotify_track_name`
- `spotify_artist`
- `spotify_isrc`
- `qq_song_id`
- `qq_song_mid`
- `qq_song_name`
- `qq_artist`
- `match_score`
- `match_method`（isrc / exact / fuzzy / manual）
- `updated_at`

#### `sync_runs`

记录每次同步任务：

- `run_id`
- `playlist_id`
- `started_at`
- `finished_at`
- `status`
- `added_count`
- `skipped_count`
- `failed_count`
- `notes`

#### `unmatched_tracks`

记录未匹配成功的歌曲，方便人工处理：

- `spotify_track_id`
- `title`
- `artist`
- `album`
- `reason`
- `created_at`

---

## 每日同步流程

### Step 1：拉取 Spotify 歌单

从 Spotify API 读取指定歌单内容：

- track id
- title
- artists
- album
- duration
- ISRC（如果可拿到）

### Step 2：歌曲标准化

把歌曲名做清洗：

- 去掉 `Remaster` / `Deluxe` / `Live` 等后缀噪音
- 统一全半角 / 空格 / 大小写
- 歌手名拆主歌手

### Step 3：QQ 音乐搜索匹配

优先级建议：

1. **ISRC 精确匹配**
2. `标题 + 主歌手` 精确匹配
3. `标题 + 歌手 + 时长容差` 模糊匹配
4. 失败则落入人工清单

### Step 4：建立映射缓存

找到一次后写入 `track_map_cache`，后续不必重复搜。

### Step 5：计算 Diff

同步模式建议支持两种：

#### Append 模式（推荐先上）

- 只把 Spotify 新增曲目加到 QQ 音乐
- 不自动删除 QQ 已有歌

优点：

- 稳
- 不容易误删
- 适合 MVP

#### Mirror 模式

- QQ 音乐歌单始终与 Spotify 完全一致
- 新增就加，删除就删

优点：

- 结果最一致

缺点：

- 误匹配/误删成本更高

### Step 6：写入 QQ 音乐歌单

- 创建歌单（如不存在）
- 添加新增歌曲
- 可选删除已下线歌曲

### Step 7：生成同步报告

同步结束后生成摘要：

- 新增几首
- 跳过几首
- 哪些没匹配上
- 哪些登录失败 / API 异常

可发到：

- 飞书机器人
- 邮件
- 本地日志

---

## 核心难点

### 1. 跨平台歌曲匹配

这是整个系统最难的部分。

原因：

- 同一首歌在两边命名可能不同
- 可能有多个版本：Live / Remaster / Deluxe / Radio Edit
- 歌手名顺序不同
- QQ 音乐与 Spotify 的 metadata 不完全一致

建议匹配策略：

```text
优先 ISRC
否则标题标准化 + 主歌手标准化
再结合时长做二次筛选
最后把失败项送人工确认
```

### 2. QQ 音乐登录态

QQ 音乐侧最大风险不是功能本身，而是：

- 登录态失效
- 库接口变化
- 风控

建议：

- 不要高频运行
- 每天最多 1~2 次
- 做好失败重试
- 保留二维码重新登录能力

### 3. 风控与幂等

必须保证：

- 同一首歌不会重复添加
- 同一轮任务失败可安全重跑

所以同步逻辑要以“差异计算”为核心，而不是盲目重写整个歌单。

---

## MVP 建议

### 第 1 版只做这些

- 1 个 Spotify 歌单
- 1 个 QQ 音乐歌单
- 每天同步 1 次
- 只做追加新增
- 不自动删除
- 匹配失败写到 `unmatched_tracks`
- 输出简单日志/日报

### 第 2 版再做

- 多歌单同步
- 手工确认页面
- Mirror 模式
- 飞书通知
- Web 管理后台

---

## 推荐项目结构

```text
spotify-qqmusic-sync/
  README.md
  .env.example
  requirements.txt
  src/
    main.py
    config.py
    db.py
    models.py
    scheduler.py
    sync/
      spotify_client.py
      qqmusic_client.py
      matcher.py
      diff_engine.py
      sync_service.py
    reports/
      notifier.py
      report_builder.py
  data/
    sync.db
  logs/
```

---

## 推荐运行方式

### 方式 1：本机 / NAS / VPS 上的 cron

```cron
0 3 * * * /usr/bin/python3 /path/to/src/main.py sync
```

### 方式 2：GitHub Actions

优点：

- 省钱
- 不需要常驻服务

缺点：

- QQ 音乐登录态管理更麻烦
- 调试不如 VPS 方便

### 方式 3：长期在线小服务

适合你以后想扩成：

- Web 面板
- 多歌单
- 手工审核
- 飞书通知

---

## 风险评估

### 低风险部分

- Spotify 读歌单
- 定时任务
- SQLite 存储
- 差异计算

### 中风险部分

- QQ 音乐非官方接口波动
- 登录态过期
- 匹配精度

### 高维护成本部分

- 浏览器 UI 自动化
- 做成完全零人工干预、强一致镜像同步

---

## 我建议的最终路线

直接做：

1. **Python 后端定时同步服务**
2. **Spotify 官方 API** 作为上游
3. **QQ 音乐社区 API** 作为目标写入端
4. **先做 Append 模式**
5. **加映射缓存 + 未匹配清单**

这是当前最容易落地、维护成本最低、长期可用性也最高的方案。

---

## 参考资料

### Spotify 官方

- Authorization Code Flow: https://developer.spotify.com/documentation/web-api/tutorials/code-flow
- Playlists 概念： https://developer.spotify.com/documentation/web-api/concepts/playlists
- Create Playlist: https://developer.spotify.com/documentation/web-api/reference/create-playlist
- Add Items to Playlist: https://developer.spotify.com/documentation/web-api/reference/add-items-to-playlist
- Remove Playlist Items: https://developer.spotify.com/documentation/web-api/reference/remove-items-playlist

### QQ 音乐社区库

- PyPI: https://pypi.org/project/qqmusic-api-python/
- 文档首页: https://l-1124.github.io/QQMusicApi/
- 登录模块: https://l-1124.github.io/QQMusicApi/reference/modules/login/
- 凭证说明: https://l-1124.github.io/QQMusicApi/tutorial/credential/
- 搜索模块: https://l-1124.github.io/QQMusicApi/reference/modules/search/
- 歌单模块: https://l-1124.github.io/QQMusicApi/reference/modules/songlist/
- 用户模块: https://l-1124.github.io/QQMusicApi/reference/modules/user/

