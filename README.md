# MQGA — 六模块 QQ 自动打包

使用 [JingMatrix/LSPatch](https://github.com/JingMatrix/LSPatch) 修补 QQ arm64 APK，默认打入 QAuxiliary、QFun、QWish、XAutoDaily、TSBattery 和 ImPad。

[运行工作流](https://github.com/Zolmid/MQGA/actions/workflows/main.yml) · [下载已验证的构建](https://github.com/Zolmid/MQGA/releases)

## 模块来源

每次运行先查询最新来源，再锁定本次使用的 commit、run、artifact 或 release asset。后续下载使用锁定结果，不会在构建中途再次查询“最新版”。

| 模块 | 默认来源 | 说明 |
| --- | --- | --- |
| [QAuxiliary](https://github.com/cinit/QAuxiliary) | 最新成功上游 CI | 默认分支 push/manual 构建，arm64 release APK |
| [QFun](https://github.com/oneQAQone/QFun) | 最新官方发布包 | 当前公开仓库没有 APK 编译 CI；从官方模块分发仓库获取 |
| [QWish](https://github.com/Xposed-Modules-Repo/top.qwish.xposed) | 最新官方发布包 | 作者网站自动同步的发布 APK；GitHub 同步任务不是编译 CI |
| [XAutoDaily](https://github.com/LuckyPray/XAutoDaily) | 本仓库 CI 编译最新源码 | 锁定最新 master 与递归子模块，构建 Debug APK |
| [TSBattery](https://github.com/fankes/TSBattery) | 最新成功上游 CI | 默认分支 push/manual 构建，legacy release APK |
| [ImPad](https://github.com/NKU100/ImPad) | 最新成功上游 CI | 默认分支 APK 构建中的 impad-module-apk，release APK |

上游 CI 只选成功、未过期且来源明确的产物，不选 PR 构建。下载失败、空 APK、包名不符或不唯一的匹配均直接失败，不会把其他来源的包默认为 CI。

XAutoDaily 当前未提供官方 APK CI。它的 Release 构建检查作者签名，因此使用上游现成的 Debug 构建模式，不修改其签名校验代码。QFun 和 QWish 的来源明确记录为官方发布包。

## 使用

修改 `config.yml` 的六个模块开关和 QQ 来源后提交到 main，会自动构建。也可在 **Actions → Release → Run workflow** 手动执行。保留每周四 01:30 UTC 的自动运行计划；“最新”指每次运行时解析的最新可用来源。

自定义 QQ 可在 **Actions → Custom** 填写版本号和 APK 直链，或填写 Release 工作流的 `qq_version` 与 `qq_url` 两项。两个入口使用同一条模块下载、构建与验证流程。

`force.enable: true` 保留当前仓库每次运行均打包的行为。设为 `false` 后，只有 QQ、模块、LSPatch 或构建配方变化才会打包。

模块开关只控制打入哪些模块。模块内部功能的默认设置、重复功能归属和预置方式需另外确认；打包不会自动把每个模块的全部功能打开。

## 产物与校验

发布前检查下载文件完整性、APK 结构、模块包名，以及成品 `assets/lspatch/modules/` 中是否包含每个选定模块的原始 APK 字节。上游提供 SHA-256 时同时验证下载摘要。

Release 包含：

- 修补后的 QQ APK。
- `sources.json`：锁定的下载与源码来源。
- `versions.json`：版本和构建配方身份，用于更新判断。
- `modules.json`：实际打入的模块、包名、版本、来源和 SHA-256。

Actions 另保留下载的模块 APK，以及本仓库生成的 XAutoDaily CI APK，便于核对同一构建使用的文件。

这些检查验证安装包组成；模块功能是否适配当前 QQ、Android 版本，以及平板登录是否允许同时在线，仍需设备上验证。

## 跨仓库 CI 下载权限

默认使用工作流 token。当前仓库此前已成功下载 QA 与 TSBattery 的跨仓库 CI 产物；若后续 GitHub 对上游产物返回认证或权限错误，可配置具有相应上游 Actions 读取权限的 `CI_ARTIFACT_TOKEN` secret。下载失败会直接显示原因，不会发布缺模块的安装包。

## 本地验证

```bash
python3 -m unittest discover -s tests -v
```

下载器和校验器位于 `.github/scripts/ci_modules.py`。XAutoDaily 编译使用 JDK 17，LSPatch 使用 JDK 21。

本分支基于 [Hakuin123/MQGA](https://github.com/Hakuin123/MQGA)。QQ 和模块由各自上游提供。

