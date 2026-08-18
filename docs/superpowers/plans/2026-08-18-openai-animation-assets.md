# OpenAI Animation Assets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 使用 DeepSeek Harness 已配置的 OpenAI-compatible API 和 `gpt-image-2`，先生成并验收四张样片，再生成标准版与修长版各 50 张一致、透明、可追溯的动画帧。

**Architecture:** 独立 Node 模块负责从 DSH 配置安全解析 Base URL 与凭据、构建 OpenAI 图片编辑请求、有限重试和响应解码；批处理 CLI 消费既有 `generation-jobs.json`，按 clip 顺序将当前比例参考、Q 版动作和上一帧送入 `gpt-image-2`。原始响应写入 git 忽略的追溯目录，正式图片经 512×512 RGBA 与 alpha 验证后才进入资产目录。

**Tech Stack:** Node.js 22 ESM、内置 `fetch`/`FormData`/`Blob`、Node test runner、OpenAI-compatible Images API、Python 3.11 `rembg`/Pillow 后处理、现有 PySide6 Helper 与资产验证器。

## Global Constraints

- Base URL 读取 `C:\Users\89088\.dsh\settings.yaml` 中 `llm-pi-ai.providers.openai.baseURL`；API Key 读取 `C:\Users\89088\.dsh\.credentials.yaml` 中 `OPENAI_API_KEY`。
- API Key 只在进程内存在，不得进入仓库、命令输出、生成任务、报告、日志或 Git。
- 正式图像模型固定为 `gpt-image-2`；正式资产不得回退本地 Stable Diffusion。
- 任何批量付费生成前必须先完成并验收四张样片：标准待机、标准动作、修长待机、修长动作。
- `standard` 约四头身；`slender` 约六至七头身；Q 版参考只提供动作、视角和表情，不得覆盖当前比例。
- 标准版与修长版各 50 帧，最终总数恰好 100；通过的样片可以计入这 100 帧。
- 正式输出必须是 512×512 PNG RGBA、sRGB、完全透明背景，逻辑显示高度为 260。
- 每帧必须保留既定脸型、渐变长发、头饰、鲸耳、女仆装、围裙鲸鱼图案、鞋袜与固定分叉鲸尾，不得出现文字、边框、家具或房间。
- 请求按 clip 顺序、低并发执行；认证、权限、模型不存在和请求格式错误不重试；超时、429 和 5xx 才有限重试。
- 不运行本地 `npm publish`。

---

## File Map

- `scripts/openai-image-config.mjs`：只负责读取和验证 DSH OpenAI Base URL/API Key，错误信息永不包含 secret。
- `scripts/openai-image-client.mjs`：只负责模型探测、multipart 图片编辑请求、重试分类、响应图片解码。
- `scripts/generate-openai-assets.mjs`：只负责 CLI、job 选择、样片门槛、引用组装、顺序生成、恢复和追溯记录。
- `scripts/remove-image-background.py`：只负责单会话复用 `u2netp`、512×512 RGBA 输出和 alpha 后处理。
- `test/openai-image-generation.test.js`：使用临时文件和 fake fetch 测试配置、请求、重试、解码、dry-run、样片门槛与凭据不泄露。
- `art-references/openai-sample-approval.json`：四张样片的显式验收结果，不含凭据；只有四项全为 `approved` 才能批量。
- `art-references/openai-generation-records.jsonl`：逐帧非敏感追溯记录；如包含本机绝对路径则保持 git ignored。

### Task 1: 安全配置、Images API 客户端与能力探测

**Files:**
- Create: `scripts/openai-image-config.mjs`
- Create: `scripts/openai-image-client.mjs`
- Create: `test/openai-image-generation.test.js`
- Modify: `package.json`

**Interfaces:**
- Produces: `loadOpenAiImageConfig({ dshHome, readText? }) -> Promise<{ baseURL: string, apiKey: string, model: 'gpt-image-2' }>`
- Produces: `createOpenAiImageClient({ baseURL, apiKey, fetchImpl?, sleep? })`
- Client methods: `listModels() -> Promise<string[]>`、`editImage({ prompt, images, size, quality, background }) -> Promise<Buffer>`
- Client errors expose `status` and a redacted message; never expose Authorization header or API Key.

- [ ] **Step 1: 写配置与客户端失败测试**

在 `test/openai-image-generation.test.js` 使用 `mkdtemp` 创建 `.dsh/settings.yaml` 与 `.dsh/.credentials.yaml`，测试以下精确行为：

```js
const config = await loadOpenAiImageConfig({ dshHome })
assert.deepEqual(config, {
  baseURL: 'https://example.invalid/v1',
  apiKey: 'secret-for-test',
  model: 'gpt-image-2',
})
```

并增加：缺 Base URL、缺 Key、`listModels()` 不含 `gpt-image-2`、HTTP 错误时，`String(error)` 均不得包含 `secret-for-test`。

为 `editImage()` 提供 fake fetch，断言：

```js
assert.equal(url, 'https://example.invalid/v1/images/edits')
assert.equal(init.method, 'POST')
assert.equal(init.headers.Authorization, 'Bearer secret-for-test')
assert.ok(init.body instanceof FormData)
assert.equal(init.body.get('model'), 'gpt-image-2')
assert.equal(init.body.get('size'), '1024x1024')
assert.equal(init.body.get('quality'), 'high')
assert.equal(init.body.get('background'), 'transparent')
assert.equal(init.body.get('output_format'), 'png')
assert.equal(init.body.getAll('image[]').length, 3)
```

fake 响应返回 `{ data: [{ b64_json: Buffer.from('png').toString('base64') }] }`，断言结果等于 `Buffer.from('png')`；另测 URL 响应会进行第二次 GET 并解码。

- [ ] **Step 2: 运行测试确认 RED**

Run: `node --test test/openai-image-generation.test.js`

Expected: FAIL，模块不存在。

- [ ] **Step 3: 实现安全配置解析**

`scripts/openai-image-config.mjs` 必须：

```js
const BASE_URL_RE = /^\s*baseURL:\s*(\S+)\s*$/mu
const API_KEY_RE = /^\s*OPENAI_API_KEY:\s*["']?([^\r\n"']+)["']?\s*$/mu

export async function loadOpenAiImageConfig({ dshHome = process.env.DSH_HOME, readText = readFile } = {}) {
  if (!dshHome) throw new Error('DSH_HOME is required for OpenAI image generation')
  const settings = await readText(resolve(dshHome, 'settings.yaml'), 'utf8')
  const credentials = await readText(resolve(dshHome, '.credentials.yaml'), 'utf8')
  const baseURL = BASE_URL_RE.exec(settings)?.[1]?.replace(/\/$/u, '')
  const apiKey = API_KEY_RE.exec(credentials)?.[1]?.trim()
  if (!baseURL) throw new Error('OpenAI image baseURL is not configured in DSH settings')
  if (!apiKey) throw new Error('OPENAI_API_KEY is not configured in DSH credentials')
  return { baseURL, apiKey, model: 'gpt-image-2' }
}
```

错误包装只能报告状态和服务端非敏感 `error.message`；输出前将 `apiKey` 的任何出现替换为 `[REDACTED]`。

- [ ] **Step 4: 实现 Images API 客户端**

客户端固定使用 `/models` 与 `/images/edits`。multipart 对每个引用创建 `new Blob([buffer], { type: 'image/png' })`，字段名必须为 `image[]`。重试规则：

```js
const retryable = status === 429 || status >= 500
const delays = [1000, 2500, 5000]
```

网络 `TypeError` 与 `AbortError` 可重试；400、401、403、404 不重试。三次重试后抛出最后错误。`listModels()` 返回 `data[].id` 并由 CLI 断言包含 `gpt-image-2`。

- [ ] **Step 5: 加入脚本并验证 GREEN**

`package.json` scripts 增加：

```json
"test:openai-images": "node --test test/openai-image-generation.test.js",
"generate:openai:dry-run": "node scripts/generate-openai-assets.mjs --dry-run"
```

Run: `node --test test/openai-image-generation.test.js`

Expected: PASS。

- [ ] **Step 6: 对真实配置做非付费能力探测**

运行一个只调用 `/models` 的命令，输出只能包含 Base URL、模型总数和 `gpt-image-2 available=true`，不得输出 Key。

Expected: exit 0，`gpt-image-2 available=true`。

- [ ] **Step 7: 提交**

```powershell
git add scripts/openai-image-config.mjs scripts/openai-image-client.mjs test/openai-image-generation.test.js package.json
git commit -m "feat: add secure OpenAI image client"
```

### Task 2: Job 编排、dry-run、样片门槛与透明后处理

**Files:**
- Create: `scripts/generate-openai-assets.mjs`
- Create: `scripts/remove-image-background.py`
- Modify: `test/openai-image-generation.test.js`
- Modify: `.gitignore`
- Depends on: 比例计划 Task 4 生成的 `art-references/generation-jobs.json` 与两个新 manifest。

**Interfaces:**
- Produces: `selectJobs(jobs, { samples, packs, clips, limit }) -> Job[]`
- Produces: `buildEditRequest(job, root) -> Promise<{ prompt: string, images: { name: string, buffer: Buffer }[] }>`
- CLI flags: `--dry-run`、`--samples`、`--packs standard,slender`、`--clips idle,head_pat`、`--limit N`、`--resume`。
- Consumes sample approval schema described below.

- [ ] **Step 1: 写 dry-run 与门槛失败测试**

测试样片选择必须固定为：

```js
[
  ['standard', 'idle', 0],
  ['standard', 'head_pat', 0],
  ['slender', 'idle', 0],
  ['slender', 'head_pat', 0],
]
```

`--samples --dry-run` 只返回四项摘要，不读取 Key、不调用 fetch。摘要包含 pack/clip/frameIndex/output/references/previousFrame，但不含任何文件字节或 secret。

批量模式读取：

```json
{
  "model": "gpt-image-2",
  "samples": {
    "standard/idle/0": "approved",
    "standard/head_pat/0": "approved",
    "slender/idle/0": "approved",
    "slender/head_pat/0": "approved"
  }
}
```

缺文件、缺任一项、值不是 `approved` 或 model 不等于 `gpt-image-2` 时，批量必须在调用 API 前失败，错误包含 `four OpenAI samples must be approved`。

- [ ] **Step 2: 运行测试确认 RED**

Run: `node --test test/openai-image-generation.test.js`

Expected: FAIL，编排模块不存在或导出不存在。

- [ ] **Step 3: 实现请求编排与连续帧引用**

`buildEditRequest` 按 generation job 的 `references` 顺序读取图片；若 `previousFrame` 非空且存在，把它追加为最后一张。提示词尾部固定追加：

```text
Q版参考只用于动作、视角和表情，不得继承二头身比例。
严格保持当前比例母图的脸型、发型、服装、围裙鲸鱼图案、鲸耳、鞋袜和分叉鲸尾。
只改变本帧动作需要变化的局部；无文字、无边框、无家具、无房间。
输出完整角色，1024x1024，透明背景 PNG。
```

图片数至少为 4；缺引用立即失败，不发送请求。每次成功后先写临时文件，再原子重命名至原始输出目录。

- [ ] **Step 4: 实现透明后处理**

`scripts/remove-image-background.py` 创建一次：

```python
session = new_session("u2netp", providers=["CPUExecutionProvider"])
```

逐个输入：Pillow 转 RGBA；若不是 512×512，用 `Image.Resampling.LANCZOS` 缩放；若 alpha 全为 255 或四角任一 alpha 大于 8，则执行 `remove(..., session=session)`；保存前断言 mode=`RGBA`、size=`(512, 512)`、alpha extrema 的最小值为 0。CLI 接收 `--input` 与 `--output`，批处理进程复用同一 session。

- [ ] **Step 5: 加入追溯、恢复和 git ignore**

每个 JSONL 记录只允许：

```json
{
  "model": "gpt-image-2",
  "pack": "standard",
  "clip": "blink",
  "frameIndex": 1,
  "output": "assets/pet-standard/idle_blink/idle_blink_512_01.png",
  "references": ["相对路径"],
  "previousFrame": "相对路径或null",
  "status": "generated|validated|failed",
  "attempts": 1,
  "error": "不含secret的摘要或null"
}
```

`.gitignore` 增加原始响应目录和含本机路径的运行记录；正式 approval 文件与最终 PNG 不忽略。`--resume` 只跳过已存在且通过 512/RGBA/alpha 验证的目标。

- [ ] **Step 6: 运行测试与 dry-run**

Run:

```powershell
node --test test/openai-image-generation.test.js
node scripts/generate-openai-assets.mjs --samples --dry-run
```

Expected: PASS；dry-run 恰好列出四张样片且不发送 API 请求。

- [ ] **Step 7: 提交**

```powershell
git add scripts/generate-openai-assets.mjs scripts/remove-image-background.py test/openai-image-generation.test.js .gitignore
git commit -m "feat: orchestrate OpenAI animation generation"
```

### Task 3: 生成并验收四张 gpt-image-2 样片

**Files:**
- Create: `art-references/openai-samples/standard-idle.png`
- Create: `art-references/openai-samples/standard-head-pat.png`
- Create: `art-references/openai-samples/slender-idle.png`
- Create: `art-references/openai-samples/slender-head-pat.png`
- Create: `art-references/openai-sample-approval.json`
- Modify: `.superpowers/sdd/openai-sample-report.md` (git ignored report)

**Interfaces:**
- Consumes: Task 2 CLI and `generation-jobs.json`。
- Produces: 四张 512×512 RGBA 样片和四项显式 approval；后续批量生成的强制门槛。

- [ ] **Step 1: 运行非付费预检**

Run:

```powershell
node scripts/generate-openai-assets.mjs --samples --dry-run
```

Expected: 恰好四项；每项引用当前比例三视图／母图／细节／对应 Q 动作；不含凭据。

- [ ] **Step 2: 生成四张付费样片**

Run:

```powershell
node scripts/generate-openai-assets.mjs --samples
```

Expected: 四次成功的 `gpt-image-2` 图片编辑响应；失败时按规则有限重试，不启动其他批量。

- [ ] **Step 3: 后处理并自动验证**

Run:

```powershell
& 'D:\Anaconda3\envs\deepseek-companion-art\python.exe' scripts/remove-image-background.py --input art-references/openai-samples --output art-references/openai-samples
node scripts/validate-pet-packs.mjs --samples art-references/openai-samples
```

Expected: 四图均为 512×512 RGBA，存在透明像素，角色未触边。

- [ ] **Step 4: 人工视觉门槛**

逐张检查标准约四头身、修长约六至七头身、角色识别特征、鲸尾、双手、无背景／文字／家具、无裁切。把结果写入 `art-references/openai-sample-approval.json`；任何失败项写 `rejected` 并只重生成该项，四项全为 `approved` 前不得执行 Task 4/5。

- [ ] **Step 5: 提交样片与 approval**

```powershell
git add art-references/openai-samples art-references/openai-sample-approval.json
git commit -m "art: approve OpenAI proportion samples"
```

### Task 4: 使用 gpt-image-2 生成标准版 50 帧

**Files:**
- Create: `assets/pet-standard/**/*.png`（恰好 50 张）
- Modify: `.superpowers/sdd/openai-standard-generation-report.md`（git ignored）

**Interfaces:**
- Consumes: 四项 approval、standard jobs、standard manifest。
- Produces: 标准版约四头身 50 帧正式 RGBA 资产。

- [ ] **Step 1: 验证 approval 与 standard dry-run**

Run: `node scripts/generate-openai-assets.mjs --packs standard --dry-run`

Expected: approval 通过；恰好 50 项、19 个 clip、output 唯一。

- [ ] **Step 2: 按 clip 顺序生成**

Run: `node scripts/generate-openai-assets.mjs --packs standard --resume`

Expected: 每个 clip 首帧使用母图与 Q 动作；后续帧额外使用上一帧；最终成功 50，失败 0。

- [ ] **Step 3: 批量透明后处理**

Run:

```powershell
& 'D:\Anaconda3\envs\deepseek-companion-art\python.exe' scripts/remove-image-background.py --input assets/pet-standard --output assets/pet-standard
```

Expected: 50 张 512×512 RGBA，alpha 最小值 0。

- [ ] **Step 4: 自动与视觉验证**

Run:

```powershell
pnpm run test:assets:packs
node --test test/pet-packs.test.js
```

逐 clip 检查身份、服装、鲸尾、脚底、帧间变化和循环；不合格帧仅重生成对应帧及依赖它的后续帧。

- [ ] **Step 5: 提交**

```powershell
git add assets/pet-standard
git commit -m "art: add standard whale animation pack"
```

### Task 5: 使用 gpt-image-2 生成修长版 50 帧

**Files:**
- Create: `assets/pet-slender/**/*.png`（恰好 50 张）
- Modify: `.superpowers/sdd/openai-slender-generation-report.md`（git ignored）

**Interfaces:**
- Consumes: 四项 approval、slender jobs、slender manifest。
- Produces: 修长版约六至七头身 50 帧正式 RGBA 资产。

- [ ] **Step 1: 验证 approval 与 slender dry-run**

Run: `node scripts/generate-openai-assets.mjs --packs slender --dry-run`

Expected: approval 通过；恰好 50 项、19 个 clip、output 唯一。

- [ ] **Step 2: 按 clip 顺序生成**

Run: `node scripts/generate-openai-assets.mjs --packs slender --resume`

Expected: 最终成功 50，失败 0；Q 参考不得把比例拉回二头身。

- [ ] **Step 3: 批量透明后处理**

Run:

```powershell
& 'D:\Anaconda3\envs\deepseek-companion-art\python.exe' scripts/remove-image-background.py --input assets/pet-slender --output assets/pet-slender
```

Expected: 50 张 512×512 RGBA，alpha 最小值 0。

- [ ] **Step 4: 自动与视觉验证**

Run:

```powershell
pnpm run test:assets:packs
node --test test/pet-packs.test.js
```

逐 clip 检查六至七头身、年龄感不增加、服装和鲸尾一致、脚底稳定、连续帧无漂移。

- [ ] **Step 5: 提交**

```powershell
git add assets/pet-slender
git commit -m "art: add slender whale animation pack"
```

### Task 6: OpenAI 资产追溯、整体验证与验收记录

**Files:**
- Modify: `ASSET_LICENSE.md`
- Modify: `docs/ACCEPTANCE.md`
- Modify: `package.json`
- Modify: `test/pet-packs.test.js`

**Interfaces:**
- Consumes: 两个 50 帧 pack、generation records、现有 Helper 切换功能。
- Produces: 可发布清单、生成来源声明、完整自动测试和 Helper 视觉验收记录。

- [ ] **Step 1: 强化最终资产断言**

`test/pet-packs.test.js` 对每帧断言：PNG、512×512、color type 6 RGBA、alpha 存在 0、四角 alpha ≤8、文件不为空；两个 pack 各 50、总计 100。

- [ ] **Step 2: 更新来源与发布清单**

`ASSET_LICENSE.md` 记录：角色设计由用户提供，标准／修长动画由 `gpt-image-2` 基于这些参考生成并经人工审核；不得写入 API Key、Base URL 私有凭据或兼容代理账单信息。`package.json.files` 包含两个 manifest、两个正式资产目录和运行时需要的 registry/loader，不包含原始响应与本机生成记录。

- [ ] **Step 3: 运行完整自动测试**

Run:

```powershell
$env:DSH_DAFEIYU_PYTHON='D:\Anaconda3\envs\deepseek-companion\python.exe'
pnpm test
& 'D:\Anaconda3\envs\deepseek-companion\python.exe' -m unittest discover -s runtime/tests -t .
pnpm run test:assets:packs
```

Expected: 全部 PASS。

- [ ] **Step 4: Helper 视觉验收**

在 offscreen 自动冒烟后，以实际 Helper 依次切换 chibi/standard/slender，在 70%、100%、140% 尺寸检查等高、脚底、气泡、裁切和状态保持；播放 19 个 clip，记录所有循环与互动序列通过。不得把未安装到当前 DSH GUI 的源代码测试描述成 GUI 验收。

- [ ] **Step 5: 更新验收记录**

`docs/ACCEPTANCE.md` 增加 OpenAI 资产章节，记录模型 `gpt-image-2`、四样片门槛、100 帧数量、自动测试结果和 Helper 视觉检查，不记录 Key、完整绝对路径或费用猜测。

- [ ] **Step 6: 提交**

```powershell
git add ASSET_LICENSE.md docs/ACCEPTANCE.md package.json test/pet-packs.test.js
git commit -m "docs: accept OpenAI animation packs"
```
