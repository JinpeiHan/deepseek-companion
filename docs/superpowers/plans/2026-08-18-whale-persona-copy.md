# 小鲸鱼人格文案实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用一个 JavaScript 与 Python 共用的 JSON 文案源统一任务状态和桌面互动文案，并将用户可见角色名称改为“小鲸鱼”。

**Architecture:** `assets/persona-copy.zh-CN.json` 是唯一文案数据源；`src/status-copy.js` 负责服务端状态选句，`runtime/persona_copy.py` 负责 Helper 互动选句。任务事实仍由 reducer 生成，不进入人格文案文件。

**Tech Stack:** Node.js 22.19+ ESM、Python 3、PySide6 Helper、Node test runner、Python unittest。

## Global Constraints

- 工作、查找、编辑与测试以信息清晰为先，只做轻度人格化。
- 只有等待、审批、成功和互动可以偶尔称“主人”。
- 项目名、阶段、待办、真实进度、错误事实和审批类型不得被角色化改写。
- 用户可见名称使用“小鲸鱼”；npm 包名、环境变量前缀和内部兼容标识继续使用 `dsh-dafeiyu`。
- 不新增聊天、API Key、聊天历史或主动对话。
- 不运行本地 `npm publish`。

---

## 文件结构

- Create: `assets/persona-copy.zh-CN.json` — JavaScript 与 Python 共用的唯一文案数据源。
- Create: `runtime/persona_copy.py` — 加载、校验、确定性选择互动文案。
- Create: `runtime/tests/test_persona_copy.py` — Python 文案加载与回退测试。
- Modify: `src/status-copy.js` — 从 JSON 读取状态文案，保留现有选句接口。
- Modify: `runtime/helper.py` — 使用共享互动文案并更新窗口标题。
- Modify: `src/index.js` — 启动消息使用人格文案并更新用户可见描述。
- Modify: `lib/client.js` — 设置卡用户可见名称改为“小鲸鱼”。
- Modify: `package.json` — 将共享 JSON 和 Python 模块加入发布文件并把 Python 测试纳入明确入口。
- Modify: `test/status-copy.test.js` — 验证人格规则、确定性和事实保留。
- Modify: `test/config-endpoint.test.js` — 验证设置卡文案不再出现用户可见“大肥鱼”。

### Task 1: 共享人格文案数据源

**Files:**
- Create: `assets/persona-copy.zh-CN.json`
- Modify: `src/status-copy.js`
- Modify: `test/status-copy.test.js`

**Interfaces:**
- Consumes: `statusCopy(group, seed)`, `activityCopy(activity, seed)`, `taskCopy(task)` 现有公开接口。
- Produces: `personaCopyLibrary: object`、`characterName: string`；现有 `statusCopyLibrary` 继续导出状态分组。

- [ ] **Step 1: 写失败的 Node 测试**

在 `test/status-copy.test.js` 增加：

```js
import { readFile } from 'node:fs/promises'
import {
  activityCopy,
  activityStage,
  characterName,
  personaCopyLibrary,
  statusCopy,
  statusCopyLibrary,
  taskCopy,
} from '../src/status-copy.js'

test('shared persona copy names the visible character and covers every state group', async () => {
  const source = JSON.parse(await readFile(new URL('../assets/persona-copy.zh-CN.json', import.meta.url), 'utf8'))
  assert.equal(characterName, '小鲸鱼')
  assert.equal(personaCopyLibrary.characterName, source.characterName)
  assert.deepEqual(statusCopyLibrary, source.status)
  assert.deepEqual(Object.keys(source.status).sort(), [
    'approval', 'commanding', 'editing', 'error', 'idle', 'limit', 'preparing',
    'result', 'searching', 'stopped', 'success', 'testing', 'thinking',
    'toolError', 'waiting', 'working',
  ])
})

test('persona copy is restrained and keeps task facts verbatim', () => {
  assert.equal(taskCopy('修改登录模块'), '正在修改登录模块呢')
  assert.match(statusCopy('waiting', 0), /主人|确认/u)
  assert.doesNotMatch(statusCopy('editing', 0), /主人/u)
  assert.doesNotMatch(Object.values(statusCopyLibrary).flat().join('\n'), /大肥鱼/u)
  assert.match(taskCopy('Ship the release'), /Ship the release/u)
})
```

- [ ] **Step 2: 运行测试确认失败**

Run: `node --test test/status-copy.test.js`

Expected: FAIL，提示缺少 `assets/persona-copy.zh-CN.json` 或缺少 `characterName` 导出。

- [ ] **Step 3: 创建完整共享文案 JSON**

创建 `assets/persona-copy.zh-CN.json`：

```json
{
  "characterName": "小鲸鱼",
  "status": {
    "idle": ["小鲸鱼在这里等新任务哦", "暂时没任务，刚好歇一下尾巴", "待命中，有事再叫我就好啦"],
    "preparing": ["新任务正在梳理中哦", "先让我看看这个项目", "正在理清接下来要做什么"],
    "thinking": ["正在认真想下一步", "让我把思路理一理", "正在整理刚才的结果"],
    "searching": ["正在帮你找相关内容", "我在项目里仔细找找", "正在查看相关文件"],
    "editing": ["这部分正在修改中", "正在把改动稳稳写进去", "正在认真调整实现"],
    "testing": ["正在认真检查结果", "正在跑测试确认一下", "正在验证改动有没有问题"],
    "commanding": ["正在执行项目命令", "正在让项目跑起来", "正在查看命令执行结果"],
    "working": ["正在继续处理任务", "这一步还在进行中", "小鲸鱼正在认真干活"],
    "result": ["正在整理刚才的结果", "这一步处理好了，再确认一下", "正在判断下一步怎么做"],
    "waiting": ["主人，这里需要你确认一下", "这里要等你看一下哦", "轮到主人决定下一步啦"],
    "approval": ["主人，这项操作需要审批", "这里在等你的批准", "有个权限操作需要主人确认"],
    "success": ["任务搞定啦，主人可以来验收了", "这一轮顺利完成", "完成啦，这次可没偷懒哦"],
    "toolError": ["这一步没有跑通", "刚才的操作遇到一点问题", "这里卡住了，需要回来看看"],
    "error": ["任务遇到问题了", "这里需要回来检查一下", "这次没有顺利跑完"],
    "stopped": ["任务已经停下来了", "这次任务先停在这里"],
    "limit": ["内容达到本轮上限了", "这次输出已经到上限"]
  },
  "interaction": {
    "headPat": ["摸摸可以，但任务还是要做完哦", "好啦，给主人摸一下"],
    "tail": ["尾巴不是进度条啦", "主人，不许一直戳尾巴"],
    "poke": ["戳我做什么，我还在工作呢", "再戳就要用尾巴拍你了哦"],
    "doubleClick": ["好啦好啦，知道主人喜欢我", "这次就多给主人摸一下"]
  }
}
```

- [ ] **Step 4: 修改 `src/status-copy.js` 从 JSON 读取**

在文件顶部增加并替换内联 `COPY`：

```js
import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)
const PERSONA_COPY = Object.freeze(require('../assets/persona-copy.zh-CN.json'))
const COPY = Object.freeze(PERSONA_COPY.status)

export const characterName = PERSONA_COPY.characterName
export const personaCopyLibrary = PERSONA_COPY
```

保留 `seedNumber`、`statusCopy`、`activityCopy`、`activityStage`、`taskCopy` 和 `statusCopyLibrary` 的现有签名。

- [ ] **Step 5: 运行 Node 测试确认通过**

Run: `node --test test/status-copy.test.js`

Expected: PASS，全部文案测试通过。

- [ ] **Step 6: 提交共享状态文案**

```powershell
git add assets/persona-copy.zh-CN.json src/status-copy.js test/status-copy.test.js
git commit -m "feat: centralize whale persona status copy"
```

### Task 2: Python Helper 共享互动文案

**Files:**
- Create: `runtime/persona_copy.py`
- Create: `runtime/tests/test_persona_copy.py`
- Modify: `runtime/helper.py:129-229,938-955`

**Interfaces:**
- Consumes: `assets/persona-copy.zh-CN.json`。
- Produces: `load_persona_copy(path: Path) -> dict[str, Any]`、`interaction_copy(copy, group, seed=0) -> str`。

- [ ] **Step 1: 写失败的 Python 测试**

创建 `runtime/tests/test_persona_copy.py`：

```python
import json
import tempfile
import unittest
from pathlib import Path

from runtime.persona_copy import interaction_copy, load_persona_copy


class PersonaCopyTests(unittest.TestCase):
    def test_loads_character_and_deterministic_interactions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "copy.json"
            path.write_text(json.dumps({
                "characterName": "小鲸鱼",
                "status": {"idle": ["待命"]},
                "interaction": {"poke": ["第一句", "第二句"]},
            }, ensure_ascii=False), encoding="utf-8")
            copy = load_persona_copy(path)
            self.assertEqual(copy["characterName"], "小鲸鱼")
            self.assertEqual(interaction_copy(copy, "poke", 1), "第二句")
            self.assertEqual(interaction_copy(copy, "poke", 1), "第二句")

    def test_rejects_missing_interaction_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "copy.json"
            path.write_text('{"characterName":"小鲸鱼","status":{},"interaction":{}}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "headPat"):
                load_persona_copy(path)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `py -3 -m unittest runtime.tests.test_persona_copy -v`

Expected: FAIL，`ModuleNotFoundError: No module named 'runtime.persona_copy'`。

- [ ] **Step 3: 实现加载、校验和确定性选句**

创建 `runtime/persona_copy.py`：

```python
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_REQUIRED_INTERACTIONS = ("headPat", "tail", "poke", "doubleClick")


def load_persona_copy(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("characterName") != "小鲸鱼":
        raise ValueError("persona copy must name 小鲸鱼")
    interaction = value.get("interaction")
    if not isinstance(interaction, dict):
        raise ValueError("persona copy interaction must be an object")
    for group in _REQUIRED_INTERACTIONS:
        variants = interaction.get(group)
        if not isinstance(variants, list) or not variants or not all(isinstance(item, str) and item for item in variants):
            raise ValueError(f"persona interaction group is invalid: {group}")
    return value


def interaction_copy(copy: dict[str, Any], group: str, seed: int = 0) -> str:
    variants = copy["interaction"][group]
    return variants[abs(int(seed)) % len(variants)]
```

- [ ] **Step 4: 运行 Python 测试确认通过**

Run: `py -3 -m unittest runtime.tests.test_persona_copy -v`

Expected: PASS，2 tests。

- [ ] **Step 5: 修改 Helper 使用共享文案**

在 `runtime/helper.py` 导入：

```python
from runtime.persona_copy import interaction_copy, load_persona_copy
```

在 manifest 加载后增加：

```python
persona_copy = load_persona_copy(bundle_root() / "assets" / "persona-copy.zh-CN.json")
```

在 `CompanionWindow.__init__` 增加：

```python
self.interaction_seed = 0
self.setWindowTitle("DSH 小鲸鱼")
```

增加方法：

```python
def _interaction_copy(self, group: str) -> str:
    message = interaction_copy(persona_copy, group, self.interaction_seed)
    self.interaction_seed += 1
    return message
```

将四个硬编码互动消息分别替换为：

```python
self._show_overlay(self._interaction_copy("headPat"), self.status_detail, self.status_state, 1800)
self._show_overlay(self._interaction_copy("tail"), self.status_detail, self.status_state, 1500)
self._show_overlay(self._interaction_copy("poke"), self.status_detail, self.status_state, 1500)
self._show_overlay(self._interaction_copy("doubleClick"), self.status_detail, self.status_state, 1800)
```

- [ ] **Step 6: 运行 Python 全套测试**

Run: `pnpm run test:python`

Expected: PASS，现有布局和动画模型测试以及新人格测试全部通过。

- [ ] **Step 7: 提交 Helper 互动文案**

```powershell
git add runtime/persona_copy.py runtime/tests/test_persona_copy.py runtime/helper.py
git commit -m "feat: share whale interaction copy with helper"
```

### Task 3: 用户可见名称与发布清单

**Files:**
- Modify: `src/index.js:21-49,197-209`
- Modify: `lib/client.js:116-127`
- Modify: `package.json:15-34`
- Modify: `test/config-endpoint.test.js`

**Interfaces:**
- Consumes: `characterName`、`statusCopy('idle', 0)`。
- Produces: 设置卡、schema 描述、Helper 初始状态统一使用“小鲸鱼”。

- [ ] **Step 1: 写失败的设置卡与发布清单测试**

在 `test/config-endpoint.test.js` 增加：

```js
test('settings card presents the character as 小鲸鱼', async () => {
  const source = await readFile(new URL('../lib/client.js', import.meta.url), 'utf8')
  assert.match(source, /小鲸鱼桌面伴侣/u)
  assert.doesNotMatch(source, /大肥鱼桌面伴侣|启用大肥鱼|大肥鱼设置/u)
})

test('package publishes the shared persona copy and loader', async () => {
  const pkg = JSON.parse(await readFile(new URL('../package.json', import.meta.url), 'utf8'))
  assert.ok(pkg.files.includes('assets/persona-copy.zh-CN.json'))
  assert.ok(pkg.files.includes('runtime/persona_copy.py'))
})
```

- [ ] **Step 2: 运行测试确认失败**

Run: `node --test test/config-endpoint.test.js`

Expected: FAIL，设置卡仍含“大肥鱼”，发布文件尚未声明。

- [ ] **Step 3: 修改插件和设置卡名称**

在 `src/index.js` 导入：

```js
import { characterName, statusCopy } from './status-copy.js'
```

将 schema 用户文案改成“启用桌面小鲸鱼”和“由 DeepSeek Harness 状态驱动的桌面小鲸鱼伴侣”。将 HELLO 文案改成：

```js
message: `${characterName} connected to DSH`,
```

将初始 STATE 文案改成：

```js
message: statusCopy('idle', 0),
```

在 `lib/client.js` 精确替换：

```text
大肥鱼桌面伴侣 -> 小鲸鱼桌面伴侣
入口和状态属于 DSH，鱼始终显示在 Windows 桌面最上层。 -> 入口和状态属于 DSH，小鲸鱼始终显示在 Windows 桌面最上层。
大肥鱼设置尚未连接到 DSH Host。 -> 小鲸鱼设置尚未连接到 DSH Host。
启用大肥鱼 -> 启用小鲸鱼
```

- [ ] **Step 4: 更新发布文件列表**

在 `package.json` 的 `files` 中加入：

```json
"runtime/persona_copy.py",
"assets/persona-copy.zh-CN.json"
```

- [ ] **Step 5: 运行人格相关测试**

Run: `node --test test/status-copy.test.js test/config-endpoint.test.js`

Expected: PASS。

Run: `pnpm run test:python`

Expected: PASS。

- [ ] **Step 6: 运行完整 Node 测试**

Run: `pnpm test`

Expected: PASS，退出码 0。

- [ ] **Step 7: 提交用户可见名称改造**

```powershell
git add src/index.js lib/client.js package.json test/config-endpoint.test.js
git commit -m "feat: present companion as 小鲸鱼"
```

### Task 4: 文案功能验收

**Files:**
- Modify: `docs/ACCEPTANCE.md`

**Interfaces:**
- Consumes: 前三项任务的运行结果。
- Produces: 可复现的文案验收记录。

- [ ] **Step 1: 运行全套自动测试并记录摘要**

Run: `pnpm test`

Expected: PASS。

Run: `pnpm run test:python`

Expected: PASS。

- [ ] **Step 2: 运行 Headless Helper 验证共享 JSON 可加载**

Run: `pnpm run helper:headless`

Expected: Helper 启动后输出 ready；发送 shutdown 消息后正常退出，不出现 persona copy 加载错误。

- [ ] **Step 3: 在验收文档增加人格文案记录**

在 `docs/ACCEPTANCE.md` 增加日期为 `2026-08-18` 的章节，明确记录：

```markdown
## 小鲸鱼人格文案

- 状态与互动使用 `assets/persona-copy.zh-CN.json` 作为唯一文案源。
- 工作文案不频繁称“主人”，等待、成功与互动可偶尔称呼。
- 项目、阶段、待办、进度、错误与审批事实保持原样。
- 设置卡和窗口标题使用“小鲸鱼”；内部包名仍为 `dsh-dafeiyu`。
- `pnpm test` 与 `pnpm run test:python` 均通过。
```

- [ ] **Step 4: 提交验收记录**

```powershell
git add docs/ACCEPTANCE.md
git commit -m "docs: record whale persona acceptance"
```
