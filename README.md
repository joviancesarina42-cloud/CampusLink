# CampusLink

**Windows 上的 IPv6 优先出口管理器。** 把能走 IPv6 的通信留在 IPv6，只给确实需要 IPv4 的目标开口子；每次开启都会先落一份完整回滚点，失败自动还原。

> **English:** CampusLink is a Windows desktop app that makes IPv6 the default egress for a mihomo kernel, with a pinned IPv6 first hop, per-service exit selection, physical-NIC traffic accounting, and verified rollback. It requires Windows 10/11, a `mihomo` binary, and a full mihomo profile. WARP (via `warpscout`) is optional. UI is Chinese. MIT licensed.

![链路概览](docs/overview.png)

---

## 它解决什么问题

国内不少校园网和园区网只对 IPv4 计费、对 IPv6 不计费或单独限速。多数人的做法是手改一份 Clash 配置，把 `ip-version` 调成 `ipv6`。这类改法有三个绕不开的坑：

| 坑 | 直接改配置的后果 | CampusLink 的做法 |
|---|---|---|
| 改坏了怎么回去 | 只能凭记忆把文件抄回来 | 开启前保存内核配置、TUN 状态、系统代理与策略组选择；一键回滚并逐项校验 |
| 谁在偷偷走 IPv4 | 只有总量，看不出是哪个程序、哪个域名 | 物理网卡抓包与内核连接列表分别记账，IPv4/IPv6/隧道/无法确认四类分开显示 |
| IPv6 与 UDP 的差异 | `ipv6` 只对 TCP 生效，UDP 静默走 IPv4 | 未确认地址族的 UDP 单独交给支持 UDP 的 IPv6 出口，而不是让它偷偷回落 |

它不生成"一份更聪明的配置"就完事，而是把**接管、验证、回滚**当成一件事来做。

## 三条并行路径

| 路径 | 走法 | 适用 |
|---|---|---|
| 原生 IPv6 | 本机直连 IPv6，不经过任何隧道 | 有 AAAA 且线路可达的目标 |
| IPv6 机场 | 固定 IPv6 首跳 → 所选出口 | 需要换地区、或目标没有 IPv6 但有 IPv6 入口的机场 |
| IPv6 WARP | 本机 `warpscout` SOCKS → Cloudflare MASQUE | 需要一条独立的 IPv6 出口，且不占机场流量 |

默认**不自动回退公网 IPv4**：没有 IPv4 例外时，一个只有 A 记录的目标就是失败，而不是悄悄走 IPv4。这是刻意的——否则"是否真的走了 IPv6"永远说不清。

![流量与连接](docs/traffic.png)

## 为什么不是又一份配置生成器

1. **回滚是可验证的，不是"应该能恢复"。** 开启前把内核配置、TUN、系统代理、策略组选择写成 `data/recovery.json`；恢复时校验备份路径、校验和、端口快照与标记，任何一项不符就报错并保留记录，不假装成功。
2. **只结束自己启动的进程。** 恢复记录里存 PID + 可执行文件路径 + 进程创建时间，三者全中才终止，避免误杀复用了 PID 的程序。
3. **口径分开。** 物理网卡字节（包含绕过代理的流量）与业务负载（内核连接增量）分别计算、不相加；名称里带 WARP 或 `[v6]` 不作地址证据，`DIRECT` 也不等于 IPv6。
4. **失败即关闭。** 节点断言、地址断言、`mihomo -t` 校验、端口占用检查，任何一步不过都不发布候选配置。

## 安装

需要 Windows 10/11 x64、一份完整的 mihomo 配置（至少一个带公网 IPv6 入口的节点）。WARP 可选。

**方式一：发布版（推荐）**

下载 Releases 里的 `CampusLink-windows-x64.zip`，解压到任意目录，双击 `CampusLink.exe`。首次启动会请求管理员权限，因为它要接管 TUN 和系统代理。

**方式二：源码运行**

```powershell
git clone <本仓库>
cd CampusLink
python -m pip install -r requirements.txt
powershell -ExecutionPolicy Bypass -File tools\build_desktop.ps1   # 编译原生窗口与托盘
python tools/selfcheck.py      # 先看缺什么：内核、配置、Npcap、管理员权限
python app.py
```

没有编译 `_desktop` 时界面会退回默认浏览器——功能一样，只是没有托盘。`tools\build_desktop.ps1` 只用本机 .NET Framework 的 `csc.exe` 加 WebView2 SDK，不安装任何东西。

首次打开后：

1. 点 **重新导入本机配置**。工具会自动查找 Clash Verge 的运行时配置与内核；找不到就在「高级」里填配置路径和 `mihomo.exe` 路径，或直接放到 `data/source.yaml` 与 `vendor/mihomo.exe`。
2. 点 **校验并查看覆盖内容**，确认规则、DNS、代理列表符合预期。
3. 点 **开启 IPv6 链路**。TUN 模式需要管理员权限。
4. 出问题时点 **停止并回滚**，会恢复开启前的内核配置、TUN 与系统代理。

> `data/`、`vendor/` 不入库。`data/` 里是本机凭据、订阅快照、SQLite 与回滚点，`vendor/` 里是内核与规则数据。迁移时两个目录一起带走。

## 自己构建

```powershell
powershell -ExecutionPolicy Bypass -File build.ps1
```

产物在 `dist/CampusLink/`。`.github/workflows/release.yml` 会在打 `v*` 标签时自动跑测试、构建并附到 Release。

## 配置项

设置页可以直接改，「高级」里的路径类配置也可以写进 `data/settings.json`。

| 键 | 含义 |
|---|---|
| `mode` | `standalone` 自带内核；`attach` 接管已有内核（热加载配置，不改订阅文件） |
| `source_config` | 完整 mihomo 配置路径；留空则自动查找 Clash Verge 运行时配置 |
| `kernel_path` / `warp_path` / `warp_account` | 内核、`warpscout`、WARP 账户路径；留空自动查找 |
| `airport_ipv6` | 固定 IPv6 首跳入口，导入时从配置里挑一个带公网 IPv6 的节点 |
| `campus_domains` | 内网直连域名（校园门户、统一认证等），走本机直连并优先 IPv6，只有 A 记录也能用 |
| `campus_cidrs` | 额外直连的 IPv6 网段 |
| `native_domains` | 强制原生 IPv6 的额外域名 |
| `native_cn` | 国内域名尽量走原生 IPv6（依赖 `geosite.dat`） |
| `warp_domains` / `warp_processes` | 走 WARP 的域名与进程 |
| `warp_udp` | 这些目标的 UDP：`block` 阻断促使回退 TCP，或 `airport` 交给 IPv6 机场 |
| `warp_enable` | 关闭后不加载 `CL-WARP`，也不生成相关规则 |
| `exceptions` | 逐项放宽：`domain`/`process` × `airport`/`ai`/`warp`/`native`/`ipv4`/`block` |
| `default_node` / `ai_node` | 默认出口与 AI 服务组各自选择的节点 |
| `dns` / `endpoints` | IPv6 DNS 上游与 WARP 端点，均为 `[IPv6]:端口` |
| `socks_port` / `mixed_port` / `core_api_port` | WARP SOCKS、独立代理、独立控制端口 |

### 典型配置

**校园网门户走内网**：`campus_domains: ["example.edu.cn"]`。这些域名的 fake-ip 会被排除，用真实地址解析，规则排在 AI 与强制原生 IPv6 之前。

**视频站走 WARP 且不吃 UDP**：`warp_domains: ["bilibili.com", "bilivideo.com", "hdslb.com"]`、`warp_processes: ["哔哩哔哩.exe"]`、`warp_udp: "block"`。TCP 走 WARP，UDP 被拒绝，播放器回退到 TCP。

**只有 IPv4 的服务**：在例外里加 `{"kind":"domain","value":"example.com","route":"ipv4"}`。这是唯一会让该目标走公网 IPv4 的入口，删除即撤销。

**AI 走另一个地区**：在「节点分流」里给 AI 服务组单独选节点；未选中 IPv6 入口的普通节点会自动经固定 IPv6 首跳中转。

![策略与配置](docs/settings.png)

## 两种运行方式

- **独立内核（默认）**：内核与 WARP 均由工具管理，用 `17897` 代理、`17979` 控制、`11081` WARP SOCKS。会临时暂停本机已有的 TUN 并接管系统代理，停止时恢复。
- **接管现有内核**：通过本机 `external-controller` 热加载覆盖策略，复用现有客户端的管理员能力。开启前校对运行规则与文件、保存原配置与策略组选择。

工具不按过期域名模糊匹配节点：首跳必须唯一命中导入时的节点名与入口，所选出口也必须仍然存在。订阅改名或换入口后，先停止回滚、更新订阅、重新导入。

## 监控口径

- **物理网卡**：从已连接的 WLAN/以太网读 Ethernet/IP 头，按远端地址区分公网 IPv4、公网 IPv6、局域网，累计 IP 层长度。包含未经过代理的流量。需要 Npcap 和管理员权限。
- **业务负载**：每秒读内核 `/connections`，用连接 UUID 与开始时间算真实增量。排除隧道进程自身，避免里外双计。
- 两侧分别展示、不相加。采样可能漏掉极短连接和关闭前最后一段字节，物理字节也可能受网卡硬件卸载影响。

## 测试与自检

```powershell
python -m unittest discover -s tests    # 离线回归，不碰网络
python tools/selfcheck.py               # 本机条件：内核、配置、Npcap、窗口组件
```

测试覆盖：配置生成与规则顺序、回滚与校验和、节点选择与失败回滚、抓包解码、SOCKS 分段握手、流量增量与会话持久化。

## 已知限制

- 仅 Windows：依赖 WFP/TUN、Npcap 与 Win32 网络状态接口。
- 需要自备 mihomo 内核与完整配置；本仓库不分发内核、订阅或 WARP 账户。
- 物理流量监控需要 Npcap 与管理员；没有它们时其余功能可用，只是少一侧数据。
- 不提供 IPv4 自动回退。目标只有 A 记录且没加例外时会明确失败。
- 计费结果以网络提供方的账户为准；本工具只负责让流量走 IPv6，不承诺任何计费结果。

## 目录

```
app.py                 本机 HTTP 接口与启动入口
campuslink/config.py   设置、节点断言、配置生成、内核校验
campuslink/manager.py  开启/回滚、采样、健康检查
campuslink/capture.py  Npcap 被动抓包与地址分类
campuslink/traffic.py  增量计算与 SQLite 存储
campuslink/warp.py     warpscout 守护与端点重扫
web/                   界面（无外部资源）
desktop/               Windows 原生窗口（C# + WebView2）
tools/                 自检与窗口编译
tests/                 离线回归测试
```

## 参考

[mihomo API](https://wiki.metacubex.one/api/) · [mihomo v1.18.6 direct 实现](https://github.com/MetaCubeX/mihomo/blob/v1.18.6/adapter/outbound/direct.go) · [warpscout](https://github.com/vernette/warpscout) · [MetaCubeXD](https://github.com/MetaCubeX/metacubexd) · [Npcap](https://npcap.com/guide/npcap-devguide.html)

本项目直接调用上述组件，未复制它们的界面代码。

## License

MIT，见 [LICENSE](LICENSE)。
