# 表情包库

让机器人能自己选表情包发给你的插件。

## 它能干嘛

聊天时 LLM 觉得该发个表情包了，就会从库里挑一张发出来。比如你说"哈哈太好笑了"，它可能回你一张大笑的图。

怎么挑呢？有三种方式：

- **关键词匹配**（默认）— 看标签里有没有你提到的词，比如搜"猫"就找带有"猫"标签的图
- **向量匹配** — 用 AI 理解你说的意思，就算用词不一样也能找到感觉对的表情包
- **混合模式** — 对全库做语义召回，再与关键词证据融合排序；没有词面命中时也不会漏掉语义结果

向量模式会为整个当前模板库建立持久化缓存，缓存位于插件数据目录的
`embedding_cache.json`。缓存按 Provider 身份、向量维度和每个条目的标签/文件名指纹校验；模型或模板库变化时只重算缺失条目，写入采用同目录原子替换。

模板库可以直接使用插件自己的管理目录，也可以组合多个 JSON 索引或图片目录。目录来源会根据文件夹和文件名自动生成标签，JSON 来源则复用索引中已有的标签。

## 多来源模板库

插件启动时会自动创建并扫描：

```text
data/plugin_data/astrbot_plugin_memes/library/
```

把 PNG、JPEG、GIF、WebP 等常见图片放进去即可使用；子目录名和文件名会参与标签匹配。这个 `managed` 来源始终启用，插件更新或重装时不会覆盖其中的数据。

还可以在 AstrBot 插件配置的 **额外模板库来源** 中添加至多 32 个来源：

- **JSON 索引来源**：填写索引文件、图片数据根目录、唯一命名空间，可兼容现有 `images` 索引格式。
- **目录扫描来源**：填写图片根目录、唯一命名空间，可选择是否递归，并可给整个来源附加标签。
- 每项都可单独禁用；路径必须是绝对路径，命名空间只允许字母、数字、点、下划线和连字符。

来源加载采用原子刷新：任一已启用来源校验失败时，继续保留上一次成功加载的库。只读 `GET /astrbot_plugin_memes/sources` 接口可查看各来源状态、缺失文件与汇总计数。

## LLM 能调的 Tool

插件注册了一个叫 `send_meme` 的函数工具，LLM 对话时可以自主调用：

```
# 推荐：只用一句话描述意图，插件自动完成匹配和发送
send_meme(intent="对方讲了冷笑话，我想无语吐槽")

# tags/scene/pack/persona 仍兼容旧调用，但通常不需要填写
send_meme(tags=["吐槽"], pack="fun")
```

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `intent` | 字符串 | ✗ | 推荐字段；一句话描述想表达的感觉或回应。省略时自动读取当前消息 |
| `tags` | 字符串列表 | ✗ | 兼容旧调用；最多 4 个情绪/内容词 |
| `scene` | 字符串 | ✗ | 高级/兼容字段；补充特殊场景 |
| `pack` | 字符串 | ✗ | 高级选项；指定表情包包 ID，不填则自动路由 |
| `persona` | 字符串 | ✗ | 高级选项；指定人格别名 |

调用一次即可；插件会自动匹配、路由、去重、检查策略并发送。成功结果会明确标记 `status=sent`，无需再次调用。

### 一句话里有多种情绪

插件会按以下顺序判断主要情绪：优先选择“但/但是/不过/然而”等转折词后的分句；同分时优先第一人称（我/我们）的情绪；“很/非常”等强度词加权，“有些/有点”等弱化词降权；被转述的“用户/对方”情绪只作为次要上下文。例如“虽然用户有些伤心，但我也很无奈”会以“无奈”为主、“伤心”为辅。句子确实含糊时，直接在 `intent` 里写出想发送的情绪即可。

## 怎么用

装上去就行，默认关键词模式直接用。想用向量的话：

1. 在 AstrBot 里配一个 Embedding Provider（OpenAI、SiliconFlow 等都行）
2. 打开表情包管理页面，点右上角 **设置**，把匹配模式换成 `embedding` 或 `hybrid`
3. 保存后重载插件

首次使用向量或混合模式时会按需建立全库索引。Provider 暂时不可用时，插件不会写入半成品缓存，并可按配置回退到关键词匹配。

## 聊天命令

插件还注册了 `/meme` 命令组：

- `/meme search 关键词` 只搜索并返回候选文件名
- `/meme send 关键词` 搜索并发送一张图片（仍经过冷却、配额和内容策略）
- `/meme list [标签]` 查看标签或筛选结果
- `/meme refresh` 刷新索引，`/meme stats` 查看聚合统计

## WebUI 能干嘛

打开插件页面能看到所有表情包，支持：

- 网格浏览，鼠标悬停有动效
- 点击标签快速筛选
- 点图片放大看
- 右上角设置面板，不用去翻配置页就能调参数
- 一键刷新库

## 配置项

都是在 WebUI 设置面板里改，不用动代码：

- **匹配模式** — 关键词 / 向量 / 混合
- **Embedding Provider** — 用哪个模型来算向量，自动列出可用的
- **向量失败回退** — 开了的话向量不灵就自动换关键词
- **候选数量** — 每次最多挑多少张
- **最低分数** — 低于这个分数的不要
- **选择策略** — `weighted` 按分数加权随机、`top` 取最高分、`random` 在候选池中随机
- **候选池大小** — 选择策略最多探索前 N 个候选
- **会话冷却与历史** — 按 UMO 会话记录最近发送，尽量避免短时间重复
- **图片内容去重** — 相同文件内容只保留一张；单个文件超过 100 MB 时不读取其内容摘要
- **发送分析与反馈学习** — WebUI 预览时可标记“有用/不合适”，后续同一会话会适度调整候选排序
- **数据保留与隐私** — 分析只保存有界聚合计数和哈希化会话标识，不保存聊天原文；可关闭或设置 1–365 天保留期
- **标签同义词** — 比如把"哈哈"和"大笑"当一回事
- **缩略图尺寸** — 预览图的大小
- **额外模板库来源** — 组合 JSON 索引与目录扫描来源，可逐项启用、设置命名空间和来源标签
- **表情包包与人格路由** — 使用 `meme_packs` 按来源命名空间/标签组织风格，`persona_packs` 将人格别名映射到包；`GET /astrbot_plugin_memes/routing` 可查看路由状态
- **权限与内容治理** — `quota_*` 限制单会话发送频率，`blocked_*`/`allowed_tags` 控制内容，`max_file_bytes` 防止异常大文件；`GET /astrbot_plugin_memes/policy` 可查看聚合状态
- **图库管理** — `POST /astrbot_plugin_memes/library/import` 接受受限 Base64 图片导入，`library/tags` 更新 managed 标签，`library/delete` 与 `library/batch` 支持安全删除和批量操作；外部来源只读
- **备份与恢复** — `backups`、`backup/create`、`backup/restore`、`backup/delete` 提供带 SHA-256 清单的 managed 快照；恢复前自动保留恢复点，解压路径和总大小都会校验
- **兼容发送管线** — `send_mode` 支持自动选择消息链或 `image_result`，并可配置超时与最多两次重试；`GET /astrbot_plugin_memes/pipeline` 可查看实际管线状态

发送分析可通过 `GET /astrbot_plugin_memes/analytics` 查看，反馈使用
`POST /astrbot_plugin_memes/feedback`，请求体为
`{"id":"managed:...","rating":1}` 或 `rating:-1`。插件会限制事件、来源、图片和标签数量，并使用同目录原子写入 `analytics.json`。

### 旧配置兼容

原有的 `index_path` 与 `data_root` 配置仍受支持：当 `index_path` 指向真实文件时，它会作为 `legacy` 来源与独立模板库一起加载；文件不存在时只记录告警，不会阻止 `managed` 目录加载。因此不再强制依赖 `smart_imagechat_hub`，已有部署也无需立即迁移配置。

## 文件结构

```
astrbot_plugin_memes/
├── main.py                 # 插件入口
├── _conf_schema.json       # 配置定义
├── metadata.yaml           # 插件信息
├── backend/
│   ├── index.py            # 读标签索引
│   ├── library.py          # 多来源加载、校验与健康报告
│   ├── matcher.py          # 匹配逻辑
│   ├── embedder.py         # 向量化
│   ├── selector.py         # 候选去重、冷却与策略选择
│   ├── analytics.py        # 发送统计、反馈与个性化
│   ├── catalog.py          # managed 导入、标签元数据与删除
│   ├── backup.py           # 快照校验、备份与恢复
│   ├── sender.py           # 兼容多版本事件 API 的发送管线
│   ├── query.py            # intent 归一化、上下文兜底与标签提取
│   ├── llm_schema.py       # 低认知负荷的 Tool 参数契约
│   └── tool.py             # LLM 调用的 tool
└── pages/gallery/          # 管理页面
```

## 安装

```bash
# 放到 AstrBot 插件目录
cp -r astrbot_plugin_memes /root/astrbot/data/plugins/

# 装 numpy（向量模式需要）
docker exec astrbot pip install numpy

# 重启
docker restart astrbot
```

如需继续复用 `smart_imagechat_hub` 的标签索引，保留原来的 `index_path` 和 `data_root` 即可；否则直接使用插件自带的管理目录。
