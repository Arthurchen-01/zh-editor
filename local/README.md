# local/ —— 本地内核（零第三方依赖）

把原先跑在云端的那一层搬回本机。**不是重写，是换掉一层。**

`qingyi_executor.py` 里本来就有知乎的全部读写能力和全部注入算法。
云端那 4 个接口唯一不可替代的能力是「算出建议稿」和「回读校验」——
而这两件事的原料本地都有。所以云端被摘掉后，剩下没有任何必需能力。

## 模块

| 文件 | 行数 | 职责 |
| --- | --- | --- |
| `qynet.py` | 391 | 标准库实现的 `requests` 子集，`install()` 顶替 `sys.modules["requests"]` |
| `qystore.py` | 769 | SQLite 文档库：快照 / 修订 / 检查 / 审计 |
| `qycheck.py` | 1029 | 敏感内容检查：67 条规则 + AI 提示词 + 交叉核对 |
| `qydocx.py` | 944 | 手写 `.docx`（含图片内嵌、尺寸自解析、WebP 兜底） |
| `qyplane.py` | 698 | 本地控制面，替代 `ControlPlane` |
| `qyapp.py` | — | GUI 服务器（`http.server`，只监听 127.0.0.1） |
| `web/` | — | 前端三文件：`index.html` / `style.css` / `app.js` |

## 启动

```bash
python3 local/qyapp.py                 # 只读模式，自动开浏览器
python3 local/qyapp.py --allow-write   # 允许写回知乎
```

macOS 双击 `启动工作台.command`。**不需要 `pip install` 任何东西。**

## 自检

```bash
python3 local/qyapp.py --port 8791 --no-browser &
python3 local/test_qyapp.py --port 8791 --export   # 端到端，31 项

python3 -S -E local/prove_zero_dep.py              # 零依赖证明，33 项
```

`-S -E` 会禁掉 `site-packages`，脚本再把它从 `sys.path` 里剔一遍，
然后断言 `requests`/`lxml`/`bs4`/`docx`/`PIL` 全部 import 不到，
**并在那个环境里跑完整个业务流**（建库 → 检查 → AI 解析 → 导出 docx → 审计）。

## 安全设计

写操作有三道闸：

1. **只监听 127.0.0.1** —— 不绑 `0.0.0.0`，局域网里的别人连不上。
2. **所有 POST 要求 `X-QY-Token`** —— 防止你随便打开一个网页就被
   恶意 JS 往 localhost 发请求（CSRF）。
3. **默认只读** —— 不加 `--allow-write`，写回知乎的接口直接拒绝。

保存时还有三道：

1. 写前存 `pre_upload` 快照（**唯一约束，永不覆盖**）；
2. 对比图片清单，**丢图就中止**；
3. 逐字记录改动，保存后**回读线上复核**，不采信「本地说做完了」。

## 规则引擎的三条硬约束

这几条是踩坑换来的，改动规则前务必先读：

**一、`block` 级规则绝不能匹配裸词。** 误报的代价是丧失检测能力本身——
一旦用户被误报烦到，他会关掉整个检查。所以 `block` 必须带
`require`（同段共现的强化词）或收紧语义；拿不准就降级成 `warn`。
`warn` 可以匹配裸词。

真实账号上抓到的 4 个误报：`自己一直输出，对方只能听`（≠ 服从性要求）、
`它完全颠覆了我对【学校】的概念`（≠ 颠覆政权）、
`你要去打人的话，机器狗比你强`（≠ 暴力管教）、
`最后总结助教主要解决了`（≠ 教主）。

**二、上下文范围必须是「段落」，不是字符窗口。** 80 字符窗口曾让
第 4 段的「我自己选择吃米糊」把第 1 段的「只能吃黄豆酱配米饭」
静默豁免掉——一个真阳性凭空消失。`same_para=True` 是为此加的。

**三、`require` 不能自我满足。** `F04` 的 pattern 含「代币」、
`require` 含「币」，约束恒真，等于没有约束。

## 图片身份锚点

比对「图片还在不在」不能拿 HTML 字符串比——平台会重排 DOM。
锚点优先级：`data-original-token` > URL 里的 `v2-<hash>` > 去掉 query 的完整 URL。
取到的 token **排序后再哈希**，避免顺序抖动造成假阳性。

## 数据

- 库：`data/qyedu.db`（WAL 模式，`PRAGMA foreign_keys=ON`）
- 快照：`snapshots` 表。`original` / `pre_upload` 有**部分唯一索引**，
  「原貌永不丢失」是数据库约束，不是代码纪律。
- 导出：`exports/`，每篇一个 `.docx`，不打包。
- 图片缓存：`data/imgcache/`（按 URL 哈希分片）。

`image_count` 在正文未同步时写 `NULL` 而不是 `0`——
「0 张图」和「不知道几张图」是两件事。
