# 视觉小说引擎适配调研

调研日期：2026-09-01。目标不是制作一个无边界的“万能解包器”，而是为 Nagi
建立可测试、可替换、许可证边界清晰的引擎适配层。

## GitHub 参考方案

| 项目 | 适用范围 | 可借鉴内容 | 约束与判断 |
| --- | --- | --- | --- |
| [GARbro](https://github.com/morkt/GARbro) | QLIE、KiriKiri、CatSystem 等大量 VN 资源格式 | 格式检测、只读归档、版本分支；QLIE 3.0/3.1 是本次主要证据 | MIT，可在保留 notice 后独立重实现；不少格式只支持读取 |
| [FuckGalEngine/exfp3](https://github.com/Inori/FuckGalEngine/blob/master/QLIE/QlieKit/exfp3/exfp3.cpp) | 多引擎工具集合 | 明确交叉验证 QLIE 3.0/3.1 文件名长度、编码和 seed 差异 | 仓库许可证不清晰，只作为格式证据，不复制代码 |
| [VNTranslationTools](https://github.com/arcusmaximus/VNTranslationTools) | KiriKiri、BGI、CatSystem2、Majiro、QLIE、YU-RIS、Ren'Py 等脚本 | 文本提取/回插、Gettext 工作流、按脚本格式拆 adapter | 已归档；同引擎不同游戏仍可能有脚本方言差异 |
| [arc_unpacker](https://github.com/vn-tools/arc_unpacker) | 多引擎资源提取 | 插件式格式/密钥选择和 CLI 集成方式 | GPL-3.0，且项目明确不以重新打包为目标；不把源码并入 Nagi |
| [GalArc](https://github.com/detached64/GalArc) | 现代多格式归档解包/打包 | 可作为外部进程式 archive backend 参考 | GPL-3.0；若集成，优先保持进程和分发边界 |
| [krkrxp3](https://github.com/DarlingGoose/krkrxp3) | KiriKiri XP3 | 窄范围 XP3 解包/重打包及部分加密模式 | 覆盖面较窄，适合作为交叉验证，不作为唯一依据 |
| [GARbro YU-RIS issue #452](https://github.com/morkt/GARbro/issues/452) | YU-RIS 版本差异案例 | 说明同一引擎也可能改变 offset 宽度、校验和或加密 | 必须按版本/作品 fingerprint 路由，不能只按引擎名判断 |

## 推荐架构

```text
Game directory
  -> EngineDetector (证据、置信度、版本 fingerprint)
  -> ArchiveAdapter (list/read；write 能力单独声明)
  -> ScriptAdapter (方言识别、Segment v1、无损回插)
  -> Translation/RAG pipeline
  -> Sidecar output / engine-specific packer
```

每个 adapter 都需要声明 `list`、`read`、`write` 三项能力；能解包不等于能重打包。
外部工具输出必须先转换成 Nagi 的稳定 manifest，再进入脚本解析，避免把命令行文本或
临时目录当作内部协议。

实现策略分三层：

1. 战略引擎使用 Nagi 原生、范围收敛的 adapter，例如目前的 QLIE。
2. 许可证和部署允许时，把成熟工具作为外部 backend，并对版本、哈希和输出做二次验证。
3. 用户已自行提取资源时，允许跳过 archive 层，只运行 ScriptAdapter 与翻译流水线。

## 引擎优先级

1. **KiriKiri/XP3**：工具成熟、脚本通常为 `.ks`，最适合作为第二个完整 adapter；先做未加密/已知加密模式的只读提取与脚本方言调查。
2. **CatSystem2 或 BGI/Ethornell**：VNTranslationTools 已提供成熟脚本工作流，可先做“已提取脚本”模式，再决定是否原生实现归档层。
3. **Ren'Py**：资源和脚本生态成熟，落地较快，但作为求职项目的逆向工程区分度较低，适合补充兼容面。
4. **YU-RIS**：版本和校验差异明显，应推迟到已有 fingerprint/adapter registry 后，优先采用外部工具加严格验证。

## 2026-09-01 五引擎选择结论

- [Ren'Py 官方说明](https://www.renpy.org/why.html)称已有数千款作品，是国际视觉小说生态中使用最广的引擎之一；其文本源脚本格式也适合无损定位和回写。
- [TyranoScript 官方站](https://tyranoscript.com/)称作品数已超过 20,000，且标准项目以 `data/scenario/*.ks` 保存场景脚本，适合作为仍活跃的日系同人引擎补充。
- [KiriKiri/KAG 文档](https://kirikirikag.sourceforge.net/contents/Prepare.html)确认 `.ks` 场景与 XP3 发行格式；它在日系商业与同人作品中有重要存量，且已有多种独立工具交叉验证格式。

因此在既有 QLIE、YU-RIS 之外选择 KiriKiri/KAG、Ren'Py、TyranoScript。首版能力边界是：
KiriKiri 散装 `.ks` 和标准未加密 XP3；Ren'Py 保留 `.rpy` 的发行包；TyranoScript 标准
`data/scenario/*.ks`。所有引擎继续使用公共三阶段工作流、安全目录、任务恢复、费用确认和发布归档。
自定义加密 XP3、仅含 `.rpyc` 的 Ren'Py 包和偏离标准布局的作品必须失败关闭。

## 下一步产品任务

- 定义引擎无关 `EngineFingerprint`、`ArchiveCapabilities` 和 `ArchiveAdapter` 协议。
- 将现有 QLIE 入口注册为第一个 adapter，保持现有 CLI 行为兼容。
- 为 KiriKiri 建立只读 Phase 0 fixture：XP3 版本、加密状态、脚本路径和编码。
- 只使用原创/合成 fixture 做 CI；商业资源、密钥、脚本正文和真实 manifest 不进入仓库。
- 重打包能力必须独立评审：路径、优先级、校验和、字符渲染与游戏内冒烟测试缺一不可。
