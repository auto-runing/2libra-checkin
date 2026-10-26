# 2Libra 每日自动签到（云端 24×7 版）

跑在 **GitHub Actions** 上，每天定时自动签到，**完全不需要电脑开机**。
签到结果可以推送到手机（企业微信 / 微信 / GitHub App）。

- 纯标准库 Python，无第三方依赖
- **签到时刻随机且可配置**：基准时间 + 1~3 小时随机窗口，不再是每天固定同一秒
- **随机的是「任务启动时刻」**（默认 `slot` 抽签模式），作业不空等，Actions 额度约 180 分钟/月
- 凭据走 GitHub Secrets，仓库里不落地任何明文 token

---

## 一、目录结构

```
2libra-checkin-cloud/
├── .github/workflows/checkin.yml   # 定时任务定义（基准时间在这里的 cron）
├── checkin.py                      # 签到脚本（本机 / 云端通用）
├── schedule.json                   # 随机延迟配置（随机区间在这里）
├── .gitignore                      # 阻止 config.json 入库
└── README.md
```

---

## 二、部署步骤

### 1. 建一个私有仓库

GitHub → **New repository** → 名称随意，例如 `2libra-checkin` → 选择 **Private** → 创建。
（私有仓库的 Actions 额度更宽松，且不会泄露你的账号信息。）

### 2. 把本目录的文件推上去

```bash
cd 2libra-checkin-cloud
git init
git add .
git commit -m "init: 2Libra 每日签到"
git branch -M main
git remote add origin https://github.com/<你的用户名>/2libra-checkin.git
git push -u origin main
```

> 不想用命令行的，直接在仓库页面 **Add file → Upload files**，把
> `checkin.py`、`schedule.json`、`.gitignore`、`README.md` 拖进去，
> 再手动新建 `.github/workflows/checkin.yml` 粘贴内容。

### 3. 配置 Secrets（关键步骤）

仓库 → **Settings → Secrets and variables → Actions → New repository secret**

| Name | 是否必须 | 说明 |
| --- | --- | --- |
| `TWOLIBRA_TOKEN` | **必须** | 浏览器里抓的 Bearer token，**不要**带 `Bearer ` 前缀 |
| `PAT` | 选填（推荐） | GitHub Personal Access Token，用于把签到日志推回仓库；不配则回退默认 `GITHUB_TOKEN`，详见下文「3.5 配置 PAT」 |
| `PUSH_URL` | 选填 | 企业微信群机器人 Webhook，用于推送到手机 |
| `PUSHPLUS_TOKEN` | 选填 | PushPlus token，推送到微信 |
| `TWOLIBRA_COOKIE` | 选填 | 一般不需要 |

抓 `TWOLIBRA_TOKEN`：登录 2libra.com → `F12` → **Network** → 筛 `Fetch/XHR` → 刷新页面 →
点任一 `2libra.com/api/...` 请求 → **Request Headers** 里的
`authorization: Bearer eyJ...`，复制 `Bearer ` 后面那段。

### 3.5（选填）配置 PAT：让日志推送更可靠

签到日志默认由 GitHub 自带的 `GITHUB_TOKEN` 推回仓库，开箱即用。
如果想让推送**更稳、归属更清晰**，可以再配一个 PAT（Personal Access Token）：

| | 默认 `GITHUB_TOKEN` | PAT |
| --- | --- | --- |
| 权限来源 | 跟随仓库 Settings → Actions → General → **Workflow permissions**；被设成只读时推送会 403 失败 | 权限长在 token 自己身上，勾了 Contents 读写就不受该设置影响 |
| 推送身份 | 提交显示为 `github-actions[bot]` | 等同你本人推送，通知、贡献记录正常 |
| 触发其他工作流 | 不会（GitHub 防递归设计，本项目目前用不到） | 会（等同一次普通 push） |
| 配置成本 | 零 | 一次性创建 token |

**申请 PAT（二选一）：**

- **细粒度 token（推荐）**：打开 <https://github.com/settings/tokens?type=beta> →
  **Generate new token** → **Repository access** 选 `Only select repositories` 并勾选你的签到仓库 →
  **Repository permissions** 把 **Contents** 设为 `Read and write`（Metadata 只读自带）→
  拉到底 **Generate token** → **马上复制**（关掉页面后就看不到完整 token 了）
- **经典 token**：打开 <https://github.com/settings/tokens> → 勾选 `repo` scope → 生成后复制

**存入 Secret**：仓库 → **Settings → Secrets and variables → Actions → New repository secret**，
Name 填 `PAT`，Secret 粘贴刚才的 token → Add secret。

> 不配 `PAT` 完全能跑：工作流会自动回退到默认 `GITHUB_TOKEN`。
> PAT 泄露或失效了就重新生成一个、更新 `PAT` Secret，不用改任何文件。

### 4. 手动测试一次

仓库 → **Actions** → 左侧选「2Libra 每日签到」→ 右上 **Run workflow** → 运行。
点进本次运行，展开「执行签到」这一步，就能看到和本地一样的输出。

### 5. 完成

之后每天北京时间 **08:05** 触发，并在随后 **1~3 小时内随机挑一个时刻**执行签到（怎么改见下一节），不用再管。

---

## 三、签到时间怎么配（随机 + 可配置）

签到时刻 = **基准时间 + 随机区间**，随机区间默认 60~180 分钟（即随机推后 1~3 小时）。
随机有**两种实现模式**，由 `schedule.json` 的 `mode` 切换，**默认 `slot`**。

| | `slot` 时段抽签（默认） | `sleep` 作业内等待 |
| --- | --- | --- |
| **随机的是什么** | **任务启动时刻**（cron 每 10 分钟触发一次，抽中的那次才签到） | 触发后脚本在作业里干等 |
| 每次作业时长 | 几秒（未到点的那次直接退出，**零接口请求**） | 1~3 小时 |
| Actions 额度 | 约 **180 分钟/月** ✅ | 约 **3600 分钟/月** ⚠️ 私有仓库会超额 |
| 时间精度 | 受 cron 间隔限制（默认 10 分钟） | 精确到秒 |
| 每日运行次数 | 约 24 次（12~25 秒/次） | 1 次 |

### 1. 基准时间 → 改 workflow 里的 cron

文件 `.github/workflows/checkin.yml` 的 `on.schedule.cron`。GitHub 的 cron 用 **UTC**（北京 = UTC + 8）：

| 想在北京时间触发 | cron 这样写 |
| --- | --- |
| 06:00 | `0 22 * * *`（前一天 UTC 22:00） |
| **08:05（默认）** | **`5 0 * * *`** |
| 12:00 | `0 4 * * *` |
| 20:00 | `0 12 * * *` |

### 2. cron 必须与 mode 配套（重要）

| mode | workflow 里的 `cron` |
| --- | --- |
| `slot`（默认） | `5,15,25,35,45,55 0-3 * * *` ← 北京时间 08:05 ~ 11:55，每 10 分钟一次 |
| `sleep` | `5 0 * * *` ← 只触发一次 |

两者是绑定的：slot 靠 cron 的多次触发来实现"随机启动时刻"，
sleep 靠单次触发 + 作业内等待。改 `mode` 一定要把 cron 一起改。

### 3. 随机区间 → 改 `schedule.json`

```json
{
  "mode": "slot",
  "base_time": "08:05",
  "delay_min_minutes": 60,
  "delay_max_minutes": 180,
  "slot_min_minutes": 10,
  "jitter_seed": ""
}
```

| 想要的效果 | 怎么填 |
| --- | --- |
| 默认：基准时间后随机推 **1~3 小时** | `60` / `180` |
| 窗口收窄一半 | `0` / `90` |
| 固定时刻、不随机 | `0` / `0` |
| 随机但当天可复现（方便排错） | `jitter_seed` 填任意字符串 |
| 抽签粒度改 5 分钟（更精细，运行次数翻倍） | `slot_min_minutes: 5` + cron 改 `0,5,10,…,55 0-3 * * *` |

`base_time` 与 cron 对不上时脚本会在日志里打印**告警**（但**以 cron 为准**），
所以改了 cron 顺手把 `base_time` 也改掉，就不会看到告警。

### 4. 不改文件也能调：环境变量

优先级高于 `schedule.json`，解开 workflow 里的注释即可：

```yaml
env:
  TWOLIBRA_DELAY_MIN: "0"
  TWOLIBRA_DELAY_MAX: "60"
```

### 5. 先看会几点签（不实际执行）

```bash
python checkin.py --plan     # 只打印今日签到时刻计划后退出
```

`slot` 模式输出形如：

```
================================================
  签到时刻计划
================================================
当前时间  : 北京 2026-10-10 17:59:42（UTC 10-10 09:59:42）
调度模式  : 时段抽签 slot（cron 多次触发，到点那次才签到）
基准时间  : 08:05（北京时间）
workflow  : cron [5,15,25,35,45,55 0-3 * * *] -> 北京 08:05
随机区间  : 60 ~ 180 分钟（按日期+种子固定，当天恒定；种子对外不可预测）
今日目标  : 北京 2026-10-11 10:37:00（当天恒定，各次触发一致）
本次动作  : 未到点 -> 直接退出，不请求接口
================================================
```

`sleep` 模式则显示「本次延迟 / 计划签到」。

> **两种模式怎么选**：仓库是**私有**的、在意免费额度 → 用默认的 `slot`；仓库**公开**
> （Actions 不计时长）或想要秒级精度 → 用 `sleep`。

---

## 四、手机上怎么看结果

配置上面任一推送通道后，结果会直接发到手机：

**方式 A：企业微信群机器人（推荐，最即时）**

1. 企业微信里建一个只有自己的群（或拉个同事）
2. 群聊右上角 → **群机器人** → **添加机器人** → 创建 → 复制 **Webhook 地址**
3. 把地址填进仓库 Secret `PUSH_URL`

之后每天会在那个群里收到一条消息，内容形如：

```
2Libra 每日签到 ✅
──────────────
账号：felix_pro（8522 号）
结果：签到成功，本次获得 120 金币
奖励：120 金币
连签：15 天（历史最长 15 天）
金币：6476
等级：5 级 1201 / 1800（还差 599 升级）
检定：d20 掷 15 + 勤勉 1 = 16，对难度 5 -> 检定成功
属性：阅历 1 ｜ 魅力 1 ｜ 幸运 2 ｜ 勤勉 1 ｜ 技艺 0 ｜ 财富 4
凭据：2026-11-16 14:50（剩 36 天）
──────────────
2026-10-11 09:23:41
```

> 消息是**纯文本**的：企业微信走 `text` 类型、PushPlus 走 `txt` 模板，
> 不含任何链接。即使接口返回里夹带了 URL（例如异常时的报错页面），
> 脚本也会替换成「（链接已省略）」，避免被聊天软件自动转成可点链接。
> 想改回带加粗的 markdown 样式，把 `render_text` 换回 `render_markdown` 即可。

**方式 B：PushPlus（推到微信）**

1. <https://www.pushplus.plus> 微信扫码登录 → 复制 token
2. 填进 Secret `PUSHPLUS_TOKEN`

**方式 C：GitHub App**

手机装 **GitHub** App → 打开仓库 → Actions → 点最近一次运行 → 底部 **Summary**
（脚本会把结果写进任务摘要，这个不需要任何额外配置，开箱可用）。

---

## 五、本地也能跑

同一份脚本，本地把 token 放进 `config.json` 即可（参考本地版的 `config.json` 格式）：

```bash
# 环境变量方式
TWOLIBRA_TOKEN=eyJ...  python checkin.py

# 或 config.json 方式
echo '{"access_token":"eyJ..."}' > config.json
python checkin.py
```

其他参数：

```bash
python checkin.py --check          # 只查询状态，不签到
python checkin.py --notify         # 手动推送一次到手机
python checkin.py --debug          # 打印各接口原始响应
python checkin.py --plan           # 只打印本次随机签到计划，不等待不签到
python checkin.py --no-random      # 忽略随机延迟，立即签到
python checkin.py --delay-seconds 5  # 手动指定等待秒数（调试用）
```

---

## 六、注意事项

- **配置成对出现**：`schedule.json` 的 `mode` 与 workflow 的 `cron` 必须配套
  （`slot` ↔ `5,15,25,35,45,55 0-3 * * *`；`sleep` ↔ `5 0 * * *`）。见第三节表格。
- **时区**：GitHub 定时任务用 **UTC**，北京时间 − 8 小时即 UTC。默认 cron 对应北京
  08:05 起第一次触发，实际签到时刻再在 09:05~11:05 之间随机落点。
- **额度**：`slot` 模式约 180 分钟/月（安全）；`sleep` 模式约 3600 分钟/月，
  私有仓库免费额度 2000 分钟/月会超额 —— 这就是默认用 `slot` 的原因。
- **`timeout-minutes` 必须大于 `delay_max_minutes`**：workflow 里保留为 350 分钟
  （GitHub 单作业上限 360），纯粹是为兼容 `sleep` 模式，`slot` 模式用不到。
  手工点「Run workflow」时可正常看到输出，不用担心被超时打断。
- **定时任务可能延迟几分钟**，高峰期（UTC 整点）尤其明显。所以 `slot` 模式的 cron
  刻意从 **08:05** 起步而不是整点；也因为如此，`slot` 模式的时间精度不要指望到分钟级。
- **60 天无提交，定时任务会被 GitHub 自动禁用**（该规则仅针对**公开**仓库；
  按本文档部署的私有仓库不受影响，但「保活心跳」仍会把日志留档在仓库里）。
  本仓库的「保活心跳」步骤在每次**成功签到**后把 `logs/` 目录的变更
  （`last-signed.txt` 与当月日志 `checkin-YYYY-MM.log`）提交回仓库
  （未到点的那些触发不会产生提交，所以不会一天刷 24 个 commit），以此规避。
  不想要这些提交就删掉该 step，但要记得每两个月手动点一次 Run workflow。
- **token 有效期 60 天**，到期后签到会失败并推送一条 `401` 告警到手机，
  按上面的方法重新抓一个，更新 `TWOLIBRA_TOKEN` Secret 即可。
- 签到是**幂等**的：当天重复运行只会返回「今天已经签到过了」，不会重复发币。

---

## 七、接口速查

| 用途 | 方法 | 路径 |
| --- | --- | --- |
| 签到 | POST | `/api/sign` |
| 账户信息 | GET | `/api/users/info?fields=info,exp,coins` |
| 连续签到 | GET | `/api/sign/stats` |
| 今日签到明细 | GET | `/api/coins/today-transaction` |
