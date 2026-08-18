# 三档角色头身比与动画资产实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为现有 Q 版小鲸鱼增加标准与修长两套完整动画资产，并支持设置页和右键菜单实时切换、持久化、安全回退与等高显示。

**Architecture:** 保留现有 `assets/pet` 和 `assets/pet-manifest.json` 作为 `chibi` 兼容包；新增资产包注册表、标准包和修长包。Python `asset_pack.py` 负责验证和加载，Helper 只持有当前包的模型与 pixmap 缓存，并在切换成功后原子替换。

**Tech Stack:** Node.js 22.19+ ESM、Python 3、PySide6、PNG RGBA、Node test runner、Python unittest、现有 DSH 配置端点。

## Global Constraints

- `characterProportion` 只允许 `chibi | standard | slender`，默认 `chibi`。
- 三档默认逻辑显示高度均为 `260`；切换不改变脚底位置、窗口位置、状态、活动、待办或气泡内容。
- 标准版约四头身；修长版约六至七头身；不进行单图局部拉伸。
- 标准版和修长版各包含 19 个动作片段、50 张完整透明帧，共新增 100 张 PNG RGBA。
- 新资产源画布为 `512 x 512`、sRGB、透明背景；不得包含参考图文字、边框、家具或房间。
- Q 版动作只提供动作语义；当前比例三视图和母图优先决定外观与比例。
- 单动作损坏回退当前包 `idle`；整包无效回退 `chibi`；任何资产错误不得导致 Helper 或 DSH 崩溃。
- npm 包名、环境变量前缀和内部兼容标识继续使用 `dsh-dafeiyu`。
- 不运行本地 `npm publish`。

---

## 文件结构

- Create: `assets/pet-packs.json` — 三个资产包的注册表。
- Create: `assets/pet-standard-manifest.json` — 标准版 manifest。
- Create: `assets/pet-slender-manifest.json` — 修长版 manifest。
- Create: `assets/pet-standard/**` — 标准版 50 张 PNG。
- Create: `assets/pet-slender/**` — 修长版 50 张 PNG。
- Create: `runtime/asset_pack.py` — manifest 验证、路径约束、QPixmap 加载与回退接口。
- Create: `runtime/tests/test_asset_pack.py` — 资产包单元测试。
- Create: `scripts/prepare-art-references.py` — 从用户设计稿生成结构化参考裁切。
- Create: `scripts/build-generation-jobs.mjs` — 根据 Q 版 manifest 生成 100 个可追溯生成任务。
- Create: `scripts/validate-pet-packs.mjs` — 校验动作集合、帧数、PNG、尺寸和透明颜色类型。
- Create: `test/pet-packs.test.js` — Node 资产注册表与发布清单测试。
- Modify: `src/index.js` — 配置 schema、默认值、CONFIG 消息和启动环境变量。
- Modify: `lib/client.js` — 设置页形态选择。
- Modify: `runtime/layout_store.py` — 持久化和归一化比例。
- Modify: `runtime/tests/test_layout_store.py` — 比例持久化测试。
- Modify: `runtime/helper.py` — 当前资产包、等高绘制、原子切换、右键菜单和回退。
- Modify: `test/config-endpoint.test.js` — 配置端点与设置卡测试。
- Modify: `test/plugin-integration.test.js` — CONFIG 与启动环境传递测试。
- Modify: `test/assets.test.js` — Q 版兼容和共享动作矩阵测试。
- Modify: `package.json` — 发布三套资产、注册表、加载模块和验证脚本。
- Modify: `ASSET_LICENSE.md` — 记录用户提供设计参考的用途与来源。
- Modify: `docs/ACCEPTANCE.md` — 自动与视觉验收记录。

### Task 1: 配置、设置页与持久化

**Files:**
- Modify: `src/index.js:21-61,155-193`
- Modify: `lib/client.js:125-147`
- Modify: `runtime/layout_store.py:12-59`
- Modify: `runtime/tests/test_layout_store.py`
- Modify: `test/config-endpoint.test.js`
- Modify: `test/plugin-integration.test.js`

**Interfaces:**
- Produces: `characterProportion: 'chibi' | 'standard' | 'slender'`；CONFIG 字段同名；环境变量 `DSH_DAFEIYU_PROPORTION`。
- Consumes: 现有配置注册、PATCH 端点、Helper CONFIG 消息。

- [ ] **Step 1: 写失败的 JavaScript 配置测试**

在 `test/config-endpoint.test.js` 的 fixture 默认值中加入 `characterProportion: 'chibi'`，并增加：

```js
test('character proportion is an allowed live setting', async () => {
  const settings = settingsFixture()
  const handler = createConfigHandler(settings)
  const changed = await request(handler, {
    method: 'PATCH',
    body: JSON.stringify({ characterProportion: 'standard' }),
  })
  assert.equal(changed.status, 200)
  assert.equal(changed.body.characterProportion, 'standard')
})

test('settings client exposes three character proportions before size', async () => {
  const source = await readFile(new URL('../lib/client.js', import.meta.url), 'utf8')
  assert.match(source, /label: '角色形态'/u)
  assert.match(source, /value: 'chibi'/u)
  assert.match(source, /value: 'standard'/u)
  assert.match(source, /value: 'slender'/u)
  assert.ok(source.indexOf("label: '角色形态'") < source.indexOf("label: '角色大小'"))
})
```

在 `test/plugin-integration.test.js` 对现有 settingsValue 增加：

```js
characterProportion: 'chibi',
```

并在 live CONFIG 断言中验证：

```js
assert.equal(configMessage.characterProportion, 'standard')
```

在 Helper 启动环境断言中验证：

```js
assert.equal(helperOptions.env.DSH_DAFEIYU_PROPORTION, 'chibi')
```

- [ ] **Step 2: 写失败的 Python 布局测试**

在 `runtime/tests/test_layout_store.py` 增加：

```python
def test_character_proportion_is_persisted_and_normalised(self) -> None:
    self.assertEqual(normalise_layout({"characterProportion": "standard"})["characterProportion"], "standard")
    self.assertEqual(normalise_layout({"characterProportion": "slender"})["characterProportion"], "slender")
    self.assertEqual(normalise_layout({"characterProportion": "invalid"})["characterProportion"], "chibi")
    self.assertEqual(normalise_layout({"characterProportion": True})["characterProportion"], "chibi")
```

同时在完整字典预期中加入：

```python
"characterProportion": "chibi",
```

- [ ] **Step 3: 运行测试确认失败**

Run: `node --test test/config-endpoint.test.js test/plugin-integration.test.js`

Expected: FAIL，缺少设置字段、UI 和环境变量。

Run: `py -3 -m unittest runtime.tests.test_layout_store -v`

Expected: FAIL，默认布局缺少 `characterProportion`。

- [ ] **Step 4: 实现插件配置和传递**

在 `src/index.js` 的 Config 中、`scale` 前增加：

```js
characterProportion: Schema.union([
  Schema.const('chibi').description('Q版'),
  Schema.const('standard').description('标准'),
  Schema.const('slender').description('修长'),
]).default('chibi').description('角色形态'),
```

在 `defaults`、`publicConfig`、CONFIG payload 和 Helper env 分别加入：

```js
characterProportion: 'chibi',
characterProportion: config.characterProportion ?? defaults.characterProportion,
characterProportion: next.characterProportion ?? defaults.characterProportion,
DSH_DAFEIYU_PROPORTION: String(resolved.characterProportion ?? defaults.characterProportion),
```

- [ ] **Step 5: 实现设置页选择器**

在 `lib/client.js` 的角色大小字段前增加：

```js
React.createElement(Field, {
  label: '角色形态',
  hint: '切换小鲸鱼的头身比例；三种形态保持相同显示高度和动作状态。',
},
React.createElement('select', {
  value: value.characterProportion ?? 'chibi', disabled: !writable, style: selectStyle,
  onChange: (event) => void write('characterProportion', event.target.value),
},
React.createElement('option', { value: 'chibi' }, 'Q版'),
React.createElement('option', { value: 'standard' }, '标准'),
React.createElement('option', { value: 'slender' }, '修长'))),
```

- [ ] **Step 6: 实现布局归一化**

在 `runtime/layout_store.py` 默认布局增加：

```python
"characterProportion": "chibi",
```

在 `normalise_layout` 增加：

```python
if value.get("characterProportion") in {"chibi", "standard", "slender"}:
    layout["characterProportion"] = value["characterProportion"]
```

- [ ] **Step 7: 运行配置测试确认通过**

Run: `node --test test/config-endpoint.test.js test/plugin-integration.test.js`

Expected: PASS。

Run: `py -3 -m unittest runtime.tests.test_layout_store -v`

Expected: PASS。

- [ ] **Step 8: 提交配置功能**

```powershell
git add src/index.js lib/client.js runtime/layout_store.py runtime/tests/test_layout_store.py test/config-endpoint.test.js test/plugin-integration.test.js
git commit -m "feat: add character proportion setting"
```

### Task 2: 资产包注册表与加载器

**Files:**
- Create: `assets/pet-packs.json`
- Create: `runtime/asset_pack.py`
- Create: `runtime/tests/test_asset_pack.py`

**Interfaces:**
- Produces: `normalise_pack_id(value) -> str`、`load_pack_descriptor(bundle_root, pack_id) -> PackDescriptor`、`load_pack_pixmaps(descriptor, QPixmap) -> dict[str, Any]`。
- `PackDescriptor` 字段：`pack_id`, `manifest`, `asset_root`, `logical_width`, `logical_height`, `foot_anchor`, `bubble_anchor`。

- [ ] **Step 1: 写失败的资产包单元测试**

创建 `runtime/tests/test_asset_pack.py`，用临时目录写最小 manifest 和 1x1 PNG 路径：

```python
import json
import tempfile
import unittest
from pathlib import Path

from runtime.asset_pack import load_pack_descriptor, normalise_pack_id


class AssetPackTests(unittest.TestCase):
    def test_pack_id_is_allowlisted(self) -> None:
        self.assertEqual(normalise_pack_id("standard"), "standard")
        self.assertEqual(normalise_pack_id("slender"), "slender")
        self.assertEqual(normalise_pack_id("unknown"), "chibi")
        self.assertEqual(normalise_pack_id(True), "chibi")

    def test_descriptor_uses_logical_metrics_and_confined_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "assets" / "pet").mkdir(parents=True)
            (root / "assets" / "pet-packs.json").write_text(json.dumps({
                "formatVersion": 1,
                "defaultPack": "chibi",
                "packs": {"chibi": {"manifest": "pet-manifest.json", "root": "pet"}},
            }), encoding="utf-8")
            (root / "assets" / "pet-manifest.json").write_text(json.dumps({
                "formatVersion": 1,
                "maxFrameWidth": 238,
                "maxFrameHeight": 260,
                "clips": {"idle": {"frames": ["idle.png"], "frameMs": 180, "loop": True}},
                "stateMap": {"IDLE": "idle"},
                "workingActivityMap": {},
                "idleMicroClips": [],
            }), encoding="utf-8")
            descriptor = load_pack_descriptor(root, "chibi")
            self.assertEqual(descriptor.logical_width, 238)
            self.assertEqual(descriptor.logical_height, 260)
            self.assertEqual(descriptor.asset_root, root / "assets" / "pet")


if __name__ == "__main__":
    unittest.main()
```

增加路径逃逸测试，registry 的 `root: "../../outside"` 必须抛出 `ValueError`，错误文本包含 `escapes assets root`。

- [ ] **Step 2: 运行测试确认失败**

Run: `py -3 -m unittest runtime.tests.test_asset_pack -v`

Expected: FAIL，缺少 `runtime.asset_pack`。

- [ ] **Step 3: 创建资产包注册表**

创建 `assets/pet-packs.json`：

```json
{
  "formatVersion": 1,
  "defaultPack": "chibi",
  "packs": {
    "chibi": { "manifest": "pet-manifest.json", "root": "pet" },
    "standard": { "manifest": "pet-standard-manifest.json", "root": "pet-standard" },
    "slender": { "manifest": "pet-slender-manifest.json", "root": "pet-slender" }
  }
}
```

- [ ] **Step 4: 实现 `runtime/asset_pack.py`**

实现不可变 descriptor 和路径约束：

```python
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

PACK_IDS = frozenset({"chibi", "standard", "slender"})


@dataclass(frozen=True)
class PackDescriptor:
    pack_id: str
    manifest: dict[str, Any]
    asset_root: Path
    logical_width: int
    logical_height: int
    foot_anchor: tuple[float, float]
    bubble_anchor: tuple[float, float]


def normalise_pack_id(value: Any) -> str:
    return value if isinstance(value, str) and value in PACK_IDS else "chibi"


def _confined(assets_root: Path, relative: str) -> Path:
    resolved = (assets_root / relative).resolve()
    if resolved != assets_root and assets_root not in resolved.parents:
        raise ValueError(f"asset path escapes assets root: {relative}")
    return resolved


def load_pack_descriptor(bundle_root: Path, pack_id: Any) -> PackDescriptor:
    assets_root = (bundle_root / "assets").resolve()
    registry = json.loads((assets_root / "pet-packs.json").read_text(encoding="utf-8"))
    selected = normalise_pack_id(pack_id)
    entry = registry["packs"].get(selected) or registry["packs"][registry["defaultPack"]]
    manifest_path = _confined(assets_root, entry["manifest"])
    asset_root = _confined(assets_root, entry["root"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    logical_width = int(manifest.get("logicalWidth", manifest["maxFrameWidth"]))
    logical_height = int(manifest.get("logicalHeight", manifest["maxFrameHeight"]))
    foot = tuple(manifest.get("footAnchor", [0.5, 1.0]))
    bubble = tuple(manifest.get("bubbleAnchor", [0.5, 0.0]))
    if logical_height != 260 or logical_width <= 0:
        raise ValueError(f"invalid logical dimensions for {selected}")
    if len(foot) != 2 or len(bubble) != 2:
        raise ValueError(f"invalid anchors for {selected}")
    return PackDescriptor(selected, manifest, asset_root, logical_width, logical_height, foot, bubble)


def load_pack_pixmaps(descriptor: PackDescriptor, pixmap_type: Callable[[str], Any]) -> dict[str, Any]:
    pixmaps: dict[str, Any] = {}
    for clip in descriptor.manifest["clips"].values():
        for frame in clip["frames"]:
            frame_path = (descriptor.asset_root / frame).resolve()
            if descriptor.asset_root.resolve() not in frame_path.parents:
                raise ValueError(f"frame escapes pack root: {frame}")
            pixmap = pixmap_type(str(frame_path))
            if pixmap.isNull():
                raise ValueError(f"unable to load frame: {descriptor.pack_id}/{frame}")
            pixmaps[frame] = pixmap
    return pixmaps
```

- [ ] **Step 5: 运行资产包测试确认通过**

Run: `py -3 -m unittest runtime.tests.test_asset_pack -v`

Expected: PASS。

- [ ] **Step 6: 提交资产包加载器**

```powershell
git add assets/pet-packs.json runtime/asset_pack.py runtime/tests/test_asset_pack.py
git commit -m "feat: add validated pet asset packs"
```

### Task 3: Helper 原子切换、等高绘制与右键菜单

**Files:**
- Modify: `runtime/helper.py:129-236,305-334,447-467,553-565,840-911,957-999`
- Modify: `runtime/tests/test_asset_pack.py`

**Interfaces:**
- Consumes: Task 2 的 `PackDescriptor`、`load_pack_descriptor`、`load_pack_pixmaps`、`normalise_pack_id`。
- Produces: `CompanionWindow._switch_pack(pack_id) -> bool`；成功后原子替换 descriptor/model/pixmaps，失败保持旧画面。

- [ ] **Step 1: 为原子加载增加失败不污染测试**

在 `runtime/tests/test_asset_pack.py` 增加 fake pixmap：

```python
class FakePixmap:
    def __init__(self, path: str) -> None:
        self.path = path
    def isNull(self) -> bool:
        return self.path.endswith("missing.png")
```

增加测试，manifest 同时含 `idle.png` 和 `missing.png` 时 `load_pack_pixmaps` 抛出 `ValueError`，调用方尚未收到部分字典。

- [ ] **Step 2: 修改 Helper 初始资产加载**

导入：

```python
from runtime.asset_pack import load_pack_descriptor, load_pack_pixmaps, normalise_pack_id
```

用以下初始化替换全局单 manifest 加载：

```python
configured_proportion = os.environ.get("DSH_DAFEIYU_PROPORTION")
self.character_proportion = normalise_pack_id(
    configured_proportion if configured_proportion is not None else self.layout.get("characterProportion")
)
self.pack = load_pack_descriptor(bundle_root(), self.character_proportion)
self.model = AnimationModel(self.pack.manifest)
self.pixmaps = load_pack_pixmaps(self.pack, QPixmap)
```

若非 Q 包加载失败，捕获异常并加载 `chibi`；若 Q 包也失败，按现有启动失败路径返回错误。

- [ ] **Step 3: 实现原子 `_switch_pack`**

在 `CompanionWindow` 增加：

```python
def _switch_pack(self, pack_id: Any) -> bool:
    target = normalise_pack_id(pack_id)
    if target == self.character_proportion:
        return True
    try:
        next_pack = load_pack_descriptor(bundle_root(), target)
        next_model = AnimationModel(next_pack.manifest)
        next_pixmaps = load_pack_pixmaps(next_pack, QPixmap)
        next_model.apply_state(self.model.base_state, self.model.base_activity)
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        print(f"Unable to switch pet pack to {target}: {error}", file=sys.stderr)
        return False
    self.fade_from_pixmap = self.pixmaps.get(self.model.frame)
    self.fade_started = time.monotonic()
    self.pack = next_pack
    self.model = next_model
    self.pixmaps = next_pixmaps
    self.character_proportion = target
    self._apply_window_size()
    self._move_to_pet(self.pet_x, self.pet_y)
    self._save_layout()
    self.update()
    return True
```

`AnimationModel.base_activity` 已是公开字段，直接复用它，不增加重复属性。

- [ ] **Step 4: CONFIG 消息支持实时切换**

在 `_apply_config` 开头处理：

```python
proportion = message.get("characterProportion")
if isinstance(proportion, str):
    self._switch_pack(proportion)
```

其余配置继续应用，即使比例包切换失败也不影响 scale、bubble 或 reduced motion。

- [ ] **Step 5: 改为逻辑尺寸绘制**

将所有全局 `manifest["maxFrameWidth"]` / `manifest["maxFrameHeight"]` 读取替换为：

```python
self.pack.logical_width
self.pack.logical_height
```

`draw_pet` 使用逻辑尺寸而非源 pixmap 尺寸：

```python
base_width = self.pack.logical_width * self.scale
base_height = self.pack.logical_height * self.scale
```

源区域仍使用完整 `pix.width()`、`pix.height()`，从而将 512x512 新帧绘制进逻辑尺寸，而不让高分辨率扩大桌面占用。

- [ ] **Step 6: 保存当前比例**

在 `_save_layout` 字典加入：

```python
"characterProportion": self.character_proportion,
```

- [ ] **Step 7: 增加右键形态菜单**

在大小菜单前增加：

```python
proportion_menu = menu.addMenu("角色形态")
proportion_actions = {}
for label, pack_id in (("Q版", "chibi"), ("标准", "standard"), ("修长", "slender")):
    action = proportion_menu.addAction(label)
    action.setCheckable(True)
    action.setChecked(self.character_proportion == pack_id)
    proportion_actions[action] = pack_id
```

在菜单选择分支最前增加：

```python
if selected in proportion_actions:
    self._switch_pack(proportion_actions[selected])
elif selected in size_actions:
```

- [ ] **Step 8: 运行 Python 测试**

Run: `pnpm run test:python`

Expected: PASS。

- [ ] **Step 9: 运行 Helper 生命周期测试**

Run: `node --test test/helper-lifecycle.test.js test/helper-restart.test.js test/plugin-integration.test.js`

Expected: PASS。

- [ ] **Step 10: 提交运行时切换**

```powershell
git add runtime/helper.py runtime/tests/test_asset_pack.py
git commit -m "feat: switch character proportions live"
```

### Task 4: 参考图裁切、生成任务与资产验证工具

**Files:**
- Create: `scripts/prepare-art-references.py`
- Create: `scripts/build-generation-jobs.mjs`
- Create: `scripts/validate-pet-packs.mjs`
- Create: `test/pet-packs.test.js`
- Modify: `package.json`
- Modify: `ASSET_LICENSE.md`

**Interfaces:**
- Produces: `art-references/**`、`art-references/generation-jobs.json`、严格资产验证命令 `pnpm run test:assets:packs`。
- Consumes: 用户上传目录、Q 版 manifest、设计稿映射和两个新 manifest。

- [ ] **Step 1: 写失败的注册表与验证测试**

创建 `test/pet-packs.test.js`，断言：

```js
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const readJson = async (relative) => JSON.parse(await readFile(new URL(relative, import.meta.url), 'utf8'))

test('registry declares three stable proportion packs', async () => {
  const registry = await readJson('../assets/pet-packs.json')
  assert.equal(registry.defaultPack, 'chibi')
  assert.deepEqual(Object.keys(registry.packs), ['chibi', 'standard', 'slender'])
})

test('new manifests match the q action matrix and contain fifty frames', async () => {
  const q = await readJson('../assets/pet-manifest.json')
  for (const file of ['../assets/pet-standard-manifest.json', '../assets/pet-slender-manifest.json']) {
    const manifest = await readJson(file)
    assert.deepEqual(Object.keys(manifest.clips), Object.keys(q.clips))
    assert.equal(Object.values(manifest.clips).reduce((sum, clip) => sum + clip.frames.length, 0), 50)
    assert.equal(manifest.logicalHeight, 260)
    assert.equal(manifest.sourceWidth, 512)
    assert.equal(manifest.sourceHeight, 512)
  }
})
```

- [ ] **Step 2: 运行测试确认失败**

Run: `node --test test/pet-packs.test.js`

Expected: FAIL，两个新 manifest 不存在。

- [ ] **Step 3: 创建两个新 manifest**

以现有 `assets/pet-manifest.json` 为动作结构源，创建标准和修长 manifest：

```json
{
  "formatVersion": 2,
  "characterId": "whale-girl-standard",
  "sourceWidth": 512,
  "sourceHeight": 512,
  "maxFrameWidth": 512,
  "maxFrameHeight": 512,
  "logicalWidth": 260,
  "logicalHeight": 260,
  "footAnchor": [0.5, 0.97],
  "bubbleAnchor": [0.5, 0.04]
}
```

修长版 `characterId` 使用 `whale-girl-slender`。两个 manifest 完整复制 Q 版的 clips、frameMs、loop、motion、stateMap、workingActivityMap 和 idleMicroClips；所有帧文件名把 `_238.png` 精确替换为 `_512.png`。

- [ ] **Step 4: 实现参考裁切脚本**

`scripts/prepare-art-references.py` 使用 PySide6 `QImage`，命令：

```powershell
py -3 scripts/prepare-art-references.py --source "D:\DeepSeek_workspace\deepseek" --output art-references
```

脚本必须：

- 读取实际 JPEG 字节，即使源扩展名错误；
- 保留完整源图副本到 `art-references/source`；
- 对设计1输出 Q 版 front/side/back/expression；
- 对设计2、3输出 standard front/side/back/details/expressions；
- 对设计5、6输出 slender front/side/back/details/expressions；
- 将其余时间戳图片复制到 `chibi/poses`；
- 所有输出使用正确 `.jpg` 扩展名；
- 写出 `art-references/reference-index.json`，每项包含源文件、比例、用途和裁切矩形。

裁切矩形采用 960 宽设计稿坐标：Q版 front `[40,50,300,390]`、side `[320,50,280,390]`、back `[610,50,260,390]`、expressions `[25,480,910,150]`；standard front `[35,45,290,420]`、side `[315,45,285,420]`、back `[595,45,275,420]`、details `[20,490,920,135]`；slender front `[20,45,300,450]`、side `[315,45,290,450]`、back `[600,45,280,450]`、details `[20,500,920,130]`。超出图像边界时应裁剪到有效区域，不得抛出越界错误。

- [ ] **Step 5: 实现 100 个生成任务清单**

`scripts/build-generation-jobs.mjs` 读取 Q、standard、slender manifests 和 reference-index，输出 `art-references/generation-jobs.json`。每个新帧一项，字段必须是：

```json
{
  "pack": "standard",
  "clip": "blink",
  "frameIndex": 1,
  "output": "assets/pet-standard/idle_blink/idle_blink_512_01.png",
  "references": [
    "art-references/standard/turnaround/front.jpg",
    "art-references/standard/turnaround/side.jpg",
    "art-references/standard/turnaround/back.jpg",
    "art-references/standard/details/details.jpg",
    "art-references/chibi/poses/<对应动作参考>.jpg"
  ],
  "previousFrame": "assets/pet-standard/idle_blink/idle_blink_512_00.png",
  "prompt": "四头身小鲸鱼女仆，保持标准版三视图的脸型、发型、服装、围裙鲸鱼图案、鲸耳与尾鳍；仅执行眨眼第2/5阶段；512x512，完整角色，透明背景，无文字，无家具。"
}
```

首帧 `previousFrame` 为 `null`。脚本必须断言恰好产生 100 项、每个 pack 50 项、所有 output 唯一。

- [ ] **Step 6: 实现严格 PNG 验证脚本**

`scripts/validate-pet-packs.mjs` 对两个新包逐帧验证：

- PNG 签名为 `89 50 4E 47 0D 0A 1A 0A`；
- IHDR 宽高严格为 512x512；
- IHDR color type 为 6（RGBA）；
- 文件路径限制在各自 root；
- manifest 声明文件与目录中 PNG 集合完全相同；
- 两包动作名、帧数、frameMs、loop、motion 与 Q 包一致；
- 总数严格为每包 50。

成功输出：

```text
standard: 19 clips, 50 RGBA frames OK
slender: 19 clips, 50 RGBA frames OK
```

- [ ] **Step 7: 更新 package scripts 和 files**

加入：

```json
"test:assets:packs": "node scripts/validate-pet-packs.mjs",
"art:references": "py -3 scripts/prepare-art-references.py",
"art:jobs": "node scripts/build-generation-jobs.mjs"
```

发布 files 加入：

```json
"runtime/asset_pack.py",
"assets/pet-packs.json",
"assets/pet-standard-manifest.json",
"assets/pet-slender-manifest.json",
"assets/pet-standard/",
"assets/pet-slender/"
```

将 `test/pet-packs.test.js` 加入现有 `test` 脚本的 Node 测试文件列表。`art-references/` 保留在 Git 仓库用于追溯，但不进入 npm 发布包；在 `ASSET_LICENSE.md` 增加“用户提供的小鲸鱼设计参考仅用于生成本项目角色资产”的来源说明。

- [ ] **Step 8: 记录参考资产来源**

在 `ASSET_LICENSE.md` 增加：

```markdown
## 小鲸鱼比例与动作参考

`art-references/` 中的角色设计与动作参考由项目委托方提供，仅用于生成本项目的标准版与修长版角色资产；这些参考文件不进入 npm 发布包。生成后的运行时资产继续遵循本文件规定的角色资产许可边界。
```

- [ ] **Step 9: 运行工具测试**

Run: `node --test test/pet-packs.test.js`

Expected: PASS（manifest 结构通过；此时 PNG 严格验证会在资产生成前失败是预期状态）。

Run: `py -3 scripts/prepare-art-references.py --source "D:\DeepSeek_workspace\deepseek" --output art-references`

Expected: 输出 reference-index，无扩展名格式错误。

Run: `node scripts/build-generation-jobs.mjs`

Expected: `generated 100 image jobs (50 standard, 50 slender)`。

- [ ] **Step 10: 提交工具、manifest 与可追溯参考包**

```powershell
git add assets/pet-packs.json assets/pet-standard-manifest.json assets/pet-slender-manifest.json scripts/prepare-art-references.py scripts/build-generation-jobs.mjs scripts/validate-pet-packs.mjs test/pet-packs.test.js package.json ASSET_LICENSE.md art-references
git commit -m "feat: add proportion asset production pipeline"
```

### Task 5: 生成并接入标准版 50 帧

**Files:**
- Create: `assets/pet-standard/**/*.png`（严格 50 张）
- Create: `art-references/standard/mother/front-512.png`
- Create: `art-references/standard/generation-log.json`

**Interfaces:**
- Consumes: `generation-jobs.json` 中 `pack=standard` 的 50 项及结构化参考包。
- Produces: 与 `pet-standard-manifest.json` 完全匹配的 50 张 512x512 RGBA PNG。

- [ ] **Step 1: 生成标准版母图**

向图像生成模型同时提供标准版 front/side/back/details、统一配色和 Q 版 idle，使用固定要求：四头身、蓝色渐变长发、蓝白鲸耳、深蓝女仆装、白围裙鲸鱼图案、固定鲸尾、完整全身、正面自然站立、512x512、透明背景、无文字、无家具。输出 `art-references/standard/mother/front-512.png`。

- [ ] **Step 2: 审核标准版母图**

逐项确认：约四头身；头饰、呆毛、耳鳍、领结、纽扣、围裙图案、袖口、裙摆、鞋袜、后腰蝴蝶结和鲸尾与设计2/3一致；手指数目正确；无背景和白边。任何一项失败都重新生成母图，不进入动作生成。

- [ ] **Step 3: 按任务清单生成 50 帧**

对 `generation-jobs.json` 中每个 standard 项，向模型提供：标准母图、标准三视图、局部细节、对应 Q 动作、上一帧（非首帧）和该项 prompt。严格写入该项 `output`，不得改变文件名或跳过帧。

- [ ] **Step 4: 记录逐帧来源**

创建 `art-references/standard/generation-log.json`，每项记录 `output`、`references`、`previousFrame`、实际生成模型标识和生成时间；条目数必须为 50。

- [ ] **Step 5: 运行严格验证**

Run: `pnpm run test:assets:packs`

Expected: standard 显示 `19 clips, 50 RGBA frames OK`；slender 尚未生成时明确报告 slender 缺失。不得忽略 standard 的任何错误。

- [ ] **Step 6: 运行标准版 Helper 快照**

Run: `$env:DSH_DAFEIYU_PROPORTION='standard'; pnpm run test:ui`

Expected: 生成标准版状态快照；角色未裁切，总逻辑高度与 Q 版一致。

- [ ] **Step 7: 提交标准版资产**

```powershell
git add assets/pet-standard art-references/standard/mother/front-512.png art-references/standard/generation-log.json
git commit -m "feat: add standard proportion animation pack"
```

### Task 6: 生成并接入修长版 50 帧

**Files:**
- Create: `assets/pet-slender/**/*.png`（严格 50 张）
- Create: `art-references/slender/mother/front-512.png`
- Create: `art-references/slender/generation-log.json`

**Interfaces:**
- Consumes: `generation-jobs.json` 中 `pack=slender` 的 50 项及结构化参考包。
- Produces: 与 `pet-slender-manifest.json` 完全匹配的 50 张 512x512 RGBA PNG。

- [ ] **Step 1: 生成修长版母图**

向图像生成模型同时提供修长版 front/side/back/details、统一配色和 Q 版 idle，固定要求：六至七头身、较小头部、修长四肢和长裙、保持可爱二次元年龄感、不写实化、不增加性感化细节、固定鲸耳与鲸尾、完整全身、512x512、透明背景、无文字、无家具。输出 `art-references/slender/mother/front-512.png`。

- [ ] **Step 2: 审核修长版母图**

逐项确认：六至七头身；总体高度与标准包逻辑高度相同；角色身份、脸型、头饰、发型、服装、围裙图案和鲸尾与设计5/6一致；手指正确；无背景和白边。失败则重新生成。

- [ ] **Step 3: 按任务清单生成 50 帧**

逐项提供修长母图、修长三视图、局部细节、Q 动作、上一帧和 prompt。Q 图只决定姿势，不能把修长版变回 Q 版。严格写入清单 output。

- [ ] **Step 4: 记录逐帧来源**

创建 `art-references/slender/generation-log.json`，字段与标准版相同，严格 50 条。

- [ ] **Step 5: 运行完整资产验证**

Run: `pnpm run test:assets:packs`

Expected:

```text
standard: 19 clips, 50 RGBA frames OK
slender: 19 clips, 50 RGBA frames OK
```

- [ ] **Step 6: 运行修长版 Helper 快照**

Run: `$env:DSH_DAFEIYU_PROPORTION='slender'; pnpm run test:ui`

Expected: 修长版完整显示，无裁切；桌面逻辑高度与 Q、标准一致。

- [ ] **Step 7: 提交修长版资产**

```powershell
git add assets/pet-slender art-references/slender/mother/front-512.png art-references/slender/generation-log.json
git commit -m "feat: add slender proportion animation pack"
```

### Task 7: 集成、视觉验收与文档

**Files:**
- Modify: `test/assets.test.js`
- Modify: `docs/ACCEPTANCE.md`

**Interfaces:**
- Consumes: 三个可加载资产包和实时配置。
- Produces: 全套自动验证与可复现视觉验收记录。

- [ ] **Step 1: 扩展资产测试覆盖三个包**

在 `test/assets.test.js` 保留现有 Q 版断言，并读取 registry，断言三个 manifest 的 clip key、帧数、frameMs、loop、motion、stateMap、workingActivityMap 和 idleMicroClips 一致；标准、修长各严格 50 帧。

- [ ] **Step 2: 运行全部 Node 测试**

Run: `pnpm test`

Expected: PASS，退出码 0。

- [ ] **Step 3: 运行全部 Python 测试**

Run: `pnpm run test:python`

Expected: PASS，退出码 0。

- [ ] **Step 4: 运行严格资产验证**

Run: `pnpm run test:assets:packs`

Expected: 两包均为 19 clips、50 RGBA frames OK。

- [ ] **Step 5: 运行三档 UI 快照**

分别执行：

```powershell
$env:DSH_DAFEIYU_PROPORTION='chibi'; pnpm run test:ui
$env:DSH_DAFEIYU_PROPORTION='standard'; pnpm run test:ui
$env:DSH_DAFEIYU_PROPORTION='slender'; pnpm run test:ui
```

Expected: 三档总逻辑高度一致；头身比分别符合 Q、约四头身、约六至七头身；角色、尾鳍和特效不被裁切。

- [ ] **Step 6: 手动切换验收**

在 DSH 设置页依次切换 Q版→标准→修长，并验证：当前 WORKING/testing 状态不变；脚底位置不跳；气泡不跳；设置重启后恢复。将 standard manifest 临时改名后尝试切换，验证旧画面保留且 DSH 任务不受影响，随后立即恢复文件。

- [ ] **Step 7: 更新验收记录**

在 `docs/ACCEPTANCE.md` 增加 `2026-08-18` 三档比例章节，记录：

- 三档设置、右键菜单和重启恢复通过；
- 标准／修长各 19 clips、50 RGBA 帧；
- 三档等高、脚底稳定、气泡稳定；
- 坏包安全回退；
- `pnpm test`、`pnpm run test:python`、`pnpm run test:assets:packs` 和三档 UI 快照结果。

- [ ] **Step 8: 最终提交**

```powershell
git add test/assets.test.js docs/ACCEPTANCE.md
git commit -m "test: verify character proportion packs"
```

- [ ] **Step 9: 确认工作树与提交历史**

Run: `git status --short --branch`

Expected: 工作树干净，当前分支仅包含设计、计划和实施提交；不得存在未追踪的生成临时文件。
