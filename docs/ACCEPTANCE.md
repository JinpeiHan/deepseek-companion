# Windows MVP acceptance baseline

Date: 2026-08-14

## Environment

- Windows 10 build 26200
- Node.js 24.15.0
- DeepSeek Harness `@deepseek-ai/dsh@0.1.0-rc.6`
- PySide6 6.11.1, PyInstaller 6.21.0
- Package candidate: `dsh-dafeiyu@0.1.0-alpha.4`

`0.1.0-alpha.5` 在同一 Windows 环境中追加验证了：

- 结构化项目名、当前待办和 `completed/total` 进度传递
- 分析、查找、实现、验证、等待、完成和错误状态文案轮换
- 两层白色圆角状态卡、柔和阴影、文本截断与状态色图标
- 源码 Helper 与预构建 Windows Helper 的真实透明窗口截图
- GitHub Release `.tgz` 覆盖更新、旧包回退和卸载说明

`0.1.0-alpha.6` 补充了中文和英文 GitHub 用户文档、两张 DSH 实机截图及五种
桌面状态图；运行时代码仅同步插件版本号，沿用 alpha.5 已验收的 Windows Helper。

## Functional acceptance

- Real DSH session sequence: `IDLE → THINKING → THINKING → WORKING → SUCCESS`.
- DSH Web settings card rendered with three checkboxes, one size slider and one activity selector.
- The WebUI successfully disabled and re-enabled the helper; the choice was persisted under
  `dsh-dafeiyu` in DSH's own `settings.yaml`.
- The prebuilt helper rendered the bundled 49-frame asset manifest without Python installed.
- Forced DSH Host termination and normal helper shutdown both left zero helper processes.
- Browser console, DSH Host stderr and helper stderr were empty in the final acceptance runs.

## Package baseline

- npm archive: approximately 54.2 MB compressed and 54.6 MB unpacked.
- Windows helper executable: approximately 50.9 MB.
- Final package inventory: 72 files; the real-DSH acceptance driver remains repository-only.

## Runtime baseline

- Warm Windows helper readiness: 1.195 seconds.
- PyInstaller parent and visual child combined working set: 74.2 MB.
- Combined private memory: 32.1 MB.
- Five-second idle CPU sample, normalized across logical processors: 0.14%.
- One first-run Windows security scan delayed readiness beyond 30 seconds. The protocol uses an
  explicit `ready` handshake and allows 60 seconds before treating startup as failed.

These measurements are a local alpha baseline, not a cross-machine performance guarantee.

## 小鲸鱼人格文案

Date: 2026-08-18

- 状态与互动使用 `assets/persona-copy.zh-CN.json` 作为唯一文案源。
- 工作文案不频繁称“主人”，等待、成功与互动可偶尔称呼。
- 项目、阶段、待办、进度、错误与审批事实保持原样。
- 设置卡和窗口标题使用“小鲸鱼”；内部包名仍为 `dsh-dafeiyu`。
- `pnpm test` 与 `pnpm run test:python` 均通过。

## 比例包动画验收

`pnpm run test:assets:animation` 自动检查两类"每帧单看都合格、连起来却是坏的"缺陷：

- **缩放跳变**：`remove-image-background.py` 每组只用一个缩放系数并保留组内相对大小，所以一张本来就画大了的帧会一直大下去。检查以每个 clip 的**中位**角色高度为基准，容差 12%（下蹲、双脚离地等姿势本来就会改变高度）。
- **死帧**：某个 clip 的相邻帧几乎完全相同，说明它根本没有在动。单张重复帧只提示（`chibi/glance` 故意在视线极限处停顿两次），超过一半的过渡都是死帧才判失败。
- **跨包等高**：三个比例包必须在屏幕上一样高，比较前先换算成逻辑像素（画布大小不同，直接比原始像素没有意义）。

### 只能人工检查的项

**眨眼必须是左右双眼同时开合。** 定位眼睑需要仓库里没有的人脸关键点，而镜像对称启发式会被角色刻意不对称的头发淹没。第一批生成的 `standard` 包五帧 `blink` 全都是同一个**单眼**wink，自动检查无法发现——因为每帧之间确实有像素差异，只是差异来自重绘抖动而不是眨眼。

因此每次重新生成后，必须人工逐帧确认：

1. `blink` 从睁眼 → 双眼同时闭合 → 回到睁眼，且首尾帧表情一致；
2. 全程没有单眼 wink，两只眼睛开合程度始终相同；
3. `head_pat` / `poke` / `tail` 的表情变化符合各帧的 `FRAME_STAGES` 描述。

用 `pnpm run art:preview -- --source clip:<pack>/<clip> --formats gif` 导出后逐帧看最快。
