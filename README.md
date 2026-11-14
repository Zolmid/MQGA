# MQGA — 六模块 QQ 预设版与空白版

使用 [JingMatrix/LSPatch](https://github.com/JingMatrix/LSPatch) 修补 QQ arm64 APK，默认打入 QAuxiliary、QFun、QWish、XAutoDaily、TSBattery 和 ImPad。

[运行工作流](https://github.com/Zolmid/MQGA/actions/workflows/main.yml) · [下载已验证的构建](https://github.com/Zolmid/MQGA/releases)

## 模块来源

每次运行先查询最新来源，再锁定本次使用的 commit、run、artifact 或 release asset。后续下载使用锁定结果，不会在构建中途再次查询“最新版”。

| 模块 | 默认来源 | 说明 |
| --- | --- | --- |
| [QAuxiliary](https://github.com/cinit/QAuxiliary) | 最新成功上游 CI | 默认分支 push/manual 构建，arm64 release APK |
| [QFun](https://github.com/oneQAQone/QFun) | 最新官方发布包 | 当前公开仓库没有 APK 编译 CI；从官方模块分发仓库获取 |
| [QWish](https://github.com/Xposed-Modules-Repo/top.qwish.xposed) | 最新官方发布包 | 作者网站自动同步的发布 APK；GitHub 同步任务不是编译 CI |
| [XAutoDaily](https://github.com/LuckyPray/XAutoDaily) | 最新官方发布包 | 上游未提供 APK CI，直接使用官方签名 APK |
| [TSBattery](https://github.com/fankes/TSBattery) | 最新成功上游 CI | 默认分支 push/manual 构建，legacy release APK |
| [ImPad](https://github.com/NKU100/ImPad) | 最新成功上游 CI | 默认分支 APK 构建中的 impad-module-apk，release APK |

上游 CI 只选成功、未过期且来源明确的产物，不选 PR 构建。下载失败、空 APK、包名不符或不唯一的匹配均直接失败，不会把其他来源的包默认为 CI。

统一来源规则：有已验证的上游 APK CI 就取最新成功构建；没有的取最新官方发布包。QFun、QWish、XAutoDaily 的来源明确记录为官方发布包。六个上游模块的 APK 保持原字节，不自行编译模块源码；构建中编译的是 MQGA 自己的配置初始化器，并将其接入预设版的 LSPatch loader。

## 同一次发布的两个版本

| 版本 | 文件名 | 配置行为 |
| --- | --- | --- |
| 预设版 | `QQ-…-preset.apk` | 带已确认的默认配置和运行时初始化器；只对新建或空白的模块配置域首次写入 |
| 空白版 | `QQ-…-blank.apk` | 使用原 LSPatch loader，不注入 MQGA 自定义配置，沿用模块上游默认 |

两版使用同一次锁定、下载的 QQ 和六模块 APK，包名与签名一致，作为同一应用的两种更新选择，不能同时安装为两个应用。发布前核对配对关系、输入 APK、模块字节、签名和 loader 差异；全部校验通过后才发布完整的两个版本。

空白版保留上游的实际默认行为：TSBattery 基础省电策略默认运行，ImPad 成功加载并匹配规则后会尝试平板行为。覆盖安装空白版会保留 QQ 原有的 MMKV、SharedPreferences、JSON 等数据，不会清除之前的预设或用户设置。

## 预设的首次写入规则

已确认配置保存在 [`presets/default.json`](presets/default.json)。初始化器在模块消费配置之前，使用实际的存储接口和类型写入；不会把一份 JSON 文件改名为 MMKV 或 FastKV，也不依赖模块自动读取 APK 中的任意资产。

- 宿主级配置只对新建或空白的模块配置域初始化。检测到既有配置后跳过整个相应范围；某个键缺失本身不构成自动补启功能的理由。
- 账号级配置等待设备上的有效登录账号，按账号首次初始化。公开 APK 不写入固定 QQ 号、好友/群名单、登录凭据或历史数据。
- XAutoDaily 先写完整参数，再写任务开关，最后处理总闸。已有任务选择或参数时，即使 `global_enable` 键缺失，也不会自动补成 true，以免让原来暂停的任务开始执行。
- 缺少群/好友列表、步数、设备名称、素材或其他必要参数的功能保持暂缓状态。用户在本机填写后，由用户主动启用；初始化器不会根据非空字段自行启动任务。
- 初始化完成或跳过的范围有独立状态记录。更新预设版本不会反复覆盖用户后来修改的配置。要对既有数据重新应用整套预设，需要另外明确执行这一操作。

TSBattery 的停用开关是反向布尔：本预设将 `disable_all_hook` 设为 `true`，以落实“不需要后台省电/保活”的选择。XAutoDaily 独立 App 的 Shizuku 保活偏好不属于 QQ 沙箱，不会冒充已由内嵌模块修改。ImPad 没有用户设置键，预设不会为它伪造开关。

QA 自身的使用协议仍需用户在设备上确认；初始化器不代为接受协议，也不写入协议接受记录。配置已写入不等于框架加载、模块授权和每个功能的运行条件已经全部满足。

## 精确版本兼容门

来源解析继续获取最新可用 CI 或官方发布包；预设适配则受已经审阅的精确 APK / LSPatch 哈希约束。当前配置以 QQ 9.3.85、JingMatrix/LSPatch v1.2 / 487 及 `presets/default.json` 中的六模块哈希为基线。QFun、QWish、XAutoDaily 的混淆入口和存储格式不能按版本名称相近就认定兼容。

新下载的模块或打包器字节与已审阅哈希不匹配时，`prepare` 阶段失败并要求复核映射。commit/run 标识用于溯源；标识变化但 APK 字节与哈希相同，不构成新的配置映射。不会静默使用旧包冒充最新版本，也不会为继续打包而重编上游模块、取消哈希校验或跳过初始化适配。改变模块集合、QQ 或打包器版本时，同样需要检查该组合能否使用当前预设。

仓库中的静态校验和构建验证不代表已完成 Android 17 / QQ 9.3.85 实机功能验证。ImPad + QA ForcePad 保留为所选的平板尝试方案，之前的互踢问题尚未证实解决；需要在设备上检查加载、首次写入、账号切换和手机/平板同时在线的实际结果。

## 使用与分支验证

`config.yml` 控制模块打包开关和 QQ 来源；当前预设针对完整六模块组合，改变组合需要同步复核配置。提交到 main 会自动构建并发布，也可在 **Actions → Release → Run workflow** 手动执行。保留每周四 01:30 UTC 的自动运行计划；“最新”指每次运行时解析的最新可用来源。

Release 的 `publish` 输入默认为 `true`。设为 `false` 时仍执行真实下载、打包和两版校验，只保存 Actions 产物，不创建 Release。`workflow_call` 使用相同输入与默认值，main 的 push 和定时运行保持正常发布。

非 main 分支的 push 和 **Actions → Preset Check** 会调用该分支、该提交的同一条可复用构建流程，固定传入 `force: true`、`publish: false`。验证分支流程与正式流程使用同一套构建和发布前校验代码。

自定义 QQ 可在 **Actions → Custom** 填写版本号和 APK 直链，或填写 Release 的 `qq_version` 与 `qq_url` 两项。自定义输入同样经过预设兼容门，不会绕过当前已审阅的 QQ / 模块范围。

`force.enable: true` 保留每次执行均打包的行为。设为 `false` 后，只有来源、配置或构建配方变化才打包。更新判断包括 `presets/default.json`、`preset-runtime/` 下所有 Java 源码和编译用声明、`.github/scripts/ci_presets.py` 及工作流文件。

## 产物与校验

发布前先检查下载文件完整性、APK 结构和模块包名；`ci_modules.py verify-patched` 检查 `dist/` 中每个 APK 的 `assets/lspatch/modules/`，确保与选定模块的原始字节一致；`ci_presets.py verify` 再核对预设/空白配对、共同输入、签名和 loader 区别。上游提供 SHA-256 时同时验证下载摘要。两项成品检查均位于上传和发布步骤之前。

Release 包含：

- 每个 QQ 输入对应的预设版和空白版 APK。
- `sources.json`：锁定的下载与源码来源。
- `versions.json`：版本和构建配方身份，用于更新判断。
- `modules.json`：实际打入的模块、包名、版本、来源和 SHA-256。
- `variants.json`：配对版本与对应的安装包校验信息。
- `preset-effective.json`：随预设版封装的已确认配置及解析结果；它不是手机上的执行日志。
- `preset-loader.json`：本次初始化器与 loader 构建记录。
- `SHA256SUMS`：发布文件的校验和。

Actions 另保留下载的六个模块 APK，便于核对同一构建使用的文件。

这些检查验证安装包组成；模块功能是否适配当前 QQ、Android 版本，以及平板登录是否允许同时在线，仍需设备上验证。

## 跨仓库 CI 下载权限

默认使用工作流 token。当前仓库此前已成功下载 QA 与 TSBattery 的跨仓库 CI 产物；若后续 GitHub 对上游产物返回认证或权限错误，可配置具有相应上游 Actions 读取权限的 `CI_ARTIFACT_TOKEN` secret。下载失败会直接显示原因，不会发布缺模块的安装包。

## 本地验证

```bash
python3 -m unittest discover -s tests -v
```

下载器和原模块字节校验器位于 `.github/scripts/ci_modules.py`。预设构建器位于 `.github/scripts/ci_presets.py`，依次执行 `prepare`、`patch`、`verify`。`prepare` 只在精确输入通过兼容门后编译 MQGA 初始化器，使用固定并验证过的 JADX 1.5.6 处理 loader；`patch` 从同一组输入生成两版；`verify` 生成版本清单与校验和。

构建使用 JDK 21、Android SDK platform / build-tools；SDK 的 `android.jar` 用于编译，`aapt2` / `apkanalyzer` 用于检查 APK 身份，签名验证使用 Android 构建工具。六个上游 APK 的原始字节在这一过程保持不变。

本分支基于 [Hakuin123/MQGA](https://github.com/Hakuin123/MQGA)。QQ 和模块由各自上游提供。

