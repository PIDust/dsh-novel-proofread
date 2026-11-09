# dsh-novel-proofread — DSH 小说校对技能插件

本插件将 `E:\迅雷下载\novel-proofread` 的校对 skill 集成进 DSH 客户端，作为 `novel-proofread` 技能供模型调用。

## 安全声明

- **纯本地运行**：所有校对处理都在你的电脑上完成，**不上传任何数据到外部服务器**。
- **无网络通信**：脚本中没有任何网络库（requests/urllib/socket/aiohttp），没有上传、发送或扫描本机文件的代码。
- **仅读写指定文件**：只操作你明确指定的 `.txt` 小说文件及其同目录下的产出文件（备份、TSV、报告、词库等）。

## 安装

```powershell
& 'D:\TOOL\DSH\resources\runtime\cli\bin\dsh.cmd' plugin --profile desktop add "file:E:/deepseek work/dsh-plugins/dsh-novel-proofread-1.0.0.tgz"
```

安装后需要**重启 DSH 客户端**才能生效。

## 使用

安装并重启后，该 skill 会注册为 `novel-proofread`，可通过 DSH 的技能系统调用。详细使用流程见插件包内的 `SKILL.md`（与 `E:\迅雷下载\novel-proofread\SKILL.md` 同内容）。

核心子命令：
- `prepare` — 检查输入文件编码、行数、章节
- `init` — 生成批次码、备份原文件
- `scan` — 单遍扫描，产出候选清单
- `judge` — 回填判定结果
- `apply` — 应用自动修改（需用户先确认）
- `diff` / `finalize` — 生成校对报告和 diff

## 文件结构

```
dsh-novel-proofread/
├── package.json
├── plugin.mjs
├── plugin.d.ts
├── cordis.patch.yml
├── SKILL.md
├── scripts/
│   ├── common.py
│   ├── proofread.py
│   ├── scanner.py
│   └── typo_dict.py
└── references/
    ├── judging-rules.md
    └── scripts-and-files.md
```

## 兼容性

- DSH 宿主：0.2.1-rc.2 / 0.2.1-rc.3
- Profile：desktop
- 依赖：Python 3.8+、pandas
