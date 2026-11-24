# ip-text-editor（知识产权文字编辑技能）

`ip-text-editor` 是一个面向中文知识产权文稿的 Agent Skill，用于审阅和改写已有的专利、商标、著作权、商业秘密及涉外实务文稿。重点清理 AI 套话、专利翻译腔越界、生硬搭配和内部工作痕迹，同时守住技术特征关系、统计口径、引注标记与法律判断强度。

## 适用场景与核心能力

- **三档改稿尺度**：区分 `A档·高保真审校`（权利要求、合同、诉讼、FTO 比对结论）、`B档·报告定向去味`（FTO 综述、专利导航、查新与咨询报告）和 `C档·流畅沟通改写`（科普、培训、商务往来），找不到具体阅读障碍或残留问题的句段照录原文。
- **专域核对与双语纠偏**：按需加载 FTO、专利导航、科技查新、Word 修订与 7 份主题用语表（含涉外中英常见误译、IPC 五级层级、2026 修订商标法版本提示及高频红线速查）。
- **只读 DOCX 来源与净稿检查**：内置零依赖 Python 标准库脚本 [scripts/check_docx_metadata.py](scripts/check_docx_metadata.py)，排查 `.docx` 属性、XML 注释、删除修订/被删域指令、相对外链及 PNG/JPEG/XMP 图像元数据中的工具署名与本机路径；支持 `--clean-draft` 核验对外净稿中的未接受修订、残留批注和黄色审阅高亮，以及多文件/目录批量扫描和 `--json` 输出。

## 安装与使用

将本仓库复制到对应 Agent 的技能目录即可生效（示例目录名 `ip-text-editor`）：

```bash
git clone https://github.com/Minervaowl7/ip-text-editor.git
```

- **Codex / Claude Code / Antigravity 及兼容 Agent Skills 的宿主**：把本目录放入全局或项目级 `skills/ip-text-editor/`，由宿主按 [SKILL.md](SKILL.md) 的 `name` 与 `description` 自动发现或显式调用（如 `$ip-text-editor`）。

## 目录结构

- [SKILL.md](SKILL.md)：技能主入口，定义任务定界、A/B/C 三档改动尺度、排查步骤、改写守则与复核交付清单。
- [agents/openai.yaml](agents/openai.yaml)：Codex 宿主界面展示名称与默认提示词配置。
- [references/](references/genres.md)：按需加载的参考资料，包含体裁指导、FTO 与专利导航专项核对、改稿正反范例、Word 修订与去痕流程，以及专业用语表。
- [scripts/check_docx_metadata.py](scripts/check_docx_metadata.py)：只读 DOCX 交付件来源信息与对外净稿检查脚本。
- [tests/test_skill.py](tests/test_skill.py)：仓库结构、链接锚点、排版护栏、脱敏卫生与元数据检查脚本的自动化测试。

## 命令行校验与测试

检查单个或多个待交付 `.docx` 文件（支持传入目录递归扫描，自动跳过 `~$` 锁文件）：

```bash
python -X utf8 scripts/check_docx_metadata.py <交付文件或目录...> --contains "需彻底删除的字样"
```

交付**对外净稿**时追加 `--clean-draft`（同步拦截未接受修订、残留批注与黄色审阅高亮），接入自动化流程时可加 `--json`：

```bash
python -X utf8 scripts/check_docx_metadata.py <对外净稿.docx> --clean-draft --json
```

运行全部技能结构与脚本单元测试（仅依赖 Python 3.10+ 标准库）：

```bash
python -X utf8 -m unittest discover -s tests -v
```

本地提交前如需按私有词表做额外脱敏扫描，可通过环境变量注入逗号或换行分隔的敏感词清单：

```bash
IP_TEXT_EDITOR_PRIVATE_TERMS="客户名A,内部项目代号" python -X utf8 -m unittest tests.test_skill.SkillStructureTests.test_privacy_scan -v
```

## 维护规范

以下约定面向技能维护者，不参与运行时加载：

1. **词表分层维护**：词汇与规则分离的组织方式借鉴自 [Vale Vocabularies](https://docs.vale.sh/keys/vocabularies)。词表按主题拆分为独立文件，只提供核对线索，不作为全局机械替换黑名单。新增词条时写明正式名称与可用简称、易混概念、适用条件及法规或标准出处编号。
2. **范例收录与脱敏**：真实文稿中发现的共性问题，先记为候选问题或误改反例；仅将经过语义核对或用户明确认可的改法转为示例。整理入库时只保留解释所需的最小脱敏上下文，严禁写入客户名称、真实案号、内部数据库名或本机路径。

## 许可证

本项目采用 [MIT License](LICENSE) 开源。
