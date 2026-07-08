# skill-lab

> 自己开发的一些 Agent Skills —— 给 Hermes 等 Agent 用的小工具。

这里放的都是遵循开放 [Agent Skills 规范](https://code.claude.com/docs/en/skills)（`SKILL.md` + YAML frontmatter）写的技能。不依赖任何宿主专有能力，理论上能装到任何支持 Agent Skills 的 Agent 里用。

主打「实用、够小、能跑」，不追求大而全。欢迎随便拿去改。

## 技能清单

| 技能 | 说明 |
|---|---|
| [todo-manager](todo-manager/) | 个人全局 GTD 助手。自然语言记录/查询/完成待办，Agent 自动推断优先级、截止日期等字段；支持子任务、循环任务、周报统计、全文搜索和撤销。纯 Python 标准库实现，跨平台。 |

## 安装

以 [Hermes](https://hermes-agent.nousresearch.com/) 为例，技能统一放在 `~/.hermes/skills/` 下，拷进去即可（在新会话中生效）：

```bash
# macOS / Linux
cp -r todo-manager ~/.hermes/skills/

# Windows (PowerShell)
Copy-Item -Recurse todo-manager $env:USERPROFILE\.hermes\skills\
```

其他支持 Agent Skills 的宿主同理，放到各自的 skills 目录即可。

## 目录结构

```
skill-lab/
├── todo-manager/          # 技能本体（SKILL.md + scripts + references）
├── tests/                 # 测试
└── docs/                  # 设计文档
```

每个技能都是自包含的一个目录，装哪个拷哪个。

## 开发

```bash
python -m pytest tests/       # 跑测试
```

## License

MIT
