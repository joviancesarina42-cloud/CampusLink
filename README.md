# CampusLink

**Windows 上的 IPv6 出口管理器。** 让能走 IPv6 的流量都走 IPv6，需要隧道时优先 WARP，VPN 可选；每次开启前先落一份完整回滚点，失败自动还原。

> **English:** CampusLink is a Windows desktop app that makes IPv6 the default egress for a mihomo kernel. It auto-registers a Cloudflare WARP account, falls back to native IPv6 where possible, optionally chains an airport over a pinned IPv6 first hop, and keeps verified rollback for every change. Requires Windows 10/11, a `mihomo` binary and `warpscout`; an airport subscription is optional. UI is Chinese. MIT licensed.

![链路概览](docs/overview.png)

## 使用场景

**看视频，跑 Agent。**

- **视频**：工具把这些域名、进程和 CDN 网段交给 WARP，全程是 IPv6。
- **Agent / AI 工具**：ChatGPT、Claude、Codex 单独走「AI 服务组」，可以指定出口。
- **日常使用**：微信可收发消息文件、浏览器不需要特别处理。可能部分传统网站不通。国内域名优先走原生 IPv6，其余默认出口——没有 VPN 时是 WARP，有 VPN 时是所选节点。

**推荐配置是 WARP + VPN 订阅**：WARP 承接视频和那些不想占 VPN 流量的连接，VPN 负责换地区和其余代理流量。公网流量里 IPv6 可达 **99% 以上**。

## 功能

开启前保存内核配置、TUN 状态、系统代理与策略组选择；一键回滚并逐项校验。

物理网卡抓包与内核连接列表分别记账，IPv4/IPv6/隧道/无法确认四类分开显示。

未确认地址族的 UDP 交给支持 UDP 的 IPv6 出口；没有出口时明确阻断，不回落 IPv4。

## 网络流量路径

| 路径      | 走法                                  | 条件                   |
| --------- | ------------------------------------- | ---------------------- |
| 原生 IPv6 | 直连 IPv6                             | 目标有 AAAA 且线路可达 |
| IPv6 WARP | `warpscout` SOCKS → Cloudflare MASQUE | 无原生；视频、普通代理 |
| IPv6 VPN  | VPN 服务器 → 所选出口                 | 国际连接；网络兜底     |

首次初始化会自动 `warpscout register` 注册一个免费账户。VPN 是可选的，没有它就只跑「原生 IPv6 + WARP」，一些流量会无法连接。

![流量与连接](docs/traffic.png)

## 安装

**方式一：发布版（推荐）**

下载 Releases 里的 `CampusLink-windows-x64.zip`，**先右键 zip → 属性 → 勾选「解除锁定」→ 应用**，再完整解压，双击 `启动 CampusLink.cmd`。启动会请求管理员权限，因为它要接管 TUN 和系统代理。看不到解压后的 exe 文件时，先检查 Windows 安全中心的保护历史记录。

**已解压，双击 CMD 仍显示「Windows 已保护你的电脑」？** CMD 本身也可能被 SmartScreen 拦截，里面的解除锁定代码此时没有机会执行。在解压后的 `CampusLink` 文件夹打开 PowerShell（当前目录内应有 `CampusLink.exe`），粘贴以下整行：

```powershell
Get-ChildItem -LiteralPath . -File | Unblock-File; powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\repair-start.ps1
```

修复入口会解除程序及依赖文件的下载标记并检查结果，然后请求管理员权限启动；失败会显示错误，取消权限请求也会明确提示。`data` 中的账户、订阅和回滚点不会被修改。仅修复、不启动时，给上述命令末尾加上 `-RepairOnly`。

本发布包尚未进行可信代码签名，不能保证所有 Windows 策略下免拦截。解除下载标记不等于获得签名或信誉；如果 Smart App Control 或单位策略仍阻止运行，可使用源码方式或联系管理员。管理员权限窗口「是否允许此应用对你的设备进行更改」是正常 UAC 请求。详见 [Microsoft SmartScreen 说明](https://learn.microsoft.com/windows/apps/package-and-deploy/smartscreen-reputation)。

**方式二：源码运行**

```powershell
git clone https://github.com/joviancesarina42-cloud/CampusLink.git
cd CampusLink
python -m pip install -r requirements.txt
powershell -ExecutionPolicy Bypass -File tools\get-deps.ps1       # 下载 mihomo 与 warpscout 到 vendor\
powershell -ExecutionPolicy Bypass -File tools\build_desktop.ps1  # 编译原生窗口与托盘
python tools/selfcheck.py                                         # 看缺什么：内核、warpscout、Npcap
python app.py
```

没有编译 `_desktop` 时界面会退回默认浏览器——功能一样，只是没有托盘。

### 第一次使用

1. 点 **下载内核与 warpscout**（必须，约 75MB）。工具会帮助下载内核与 WARP 客户端，识别 VPN 配置、注册 WARP 账户。
2. 点 **开启 IPv6 链路**。TUN 模式需要管理员权限。

下载连接失败会自动重试，两个组件下载及随后的初始化共用 **60 秒总时限**，到时停止并恢复按钮，可再次点击下载；已完整下载的组件会保留。下载期间可点下载按钮旁或概览页的 **停止下载**，或通过界面、托盘 **退出并恢复网络**，无需等待网络请求结束。停止下载不会暂停流量监控；取消或超时会终止下载及其注册子进程，不会在后台继续下载。

## 常见问题

- 仅 Windows：依赖 WFP/TUN、Npcap 与 Win32 网络状态接口。
- **无法启动？** 内核与 warpscout 由使用者的机器从官方 Release 下载。**「策略与配置」页，点击「下载内核与 warpscout」按钮。**
- 首次运行需要联网：约 75MB 的两个可执行文件，外加 mihomo 自己下载的约 20MB 地理数据（`geosite.dat`、`geoip.dat`、`Country.mmdb`）。
- **流量监控有问题？** 物理流量监控需要 Npcap 与管理员；没有它们时其余功能可用，只是少一侧数据。**手动安装 Npcap。**
- **部分网站无法打开？** 不提供 IPv4 自动回退。目标只有 A 记录且没加例外时会明确失败。**手动加规则。**
- 计费结果以网络提供方的账户为准；本工具只负责让流量走 IPv6，不承诺任何计费结果。

## 默认策略

| 流量                              | 处理                                |
| --------------------------------- | ----------------------------------- |
| WARP 端点、VPN IPv6 入口          | 精确地址放行，包含 UDP/443          |
| 回环、私有网段、内网直连域名/网段 | 直连                                |
| B 站域名、CDN 网段、播放器进程    | TCP 走 WARP；UDP 阻断，促使回退 TCP |
| AI 域名与进程                     | AI 服务组所选节点                   |
| 其余需要代理的连接                | 默认出口组；没有 VPN 时就是 WARP    |
| 已知公网 IPv6 字面量              | 原生 IPv6 直连                      |
| 其余                              | 默认出口组，**不自动回退公网 IPv4** |

![策略与配置](docs/settings.png)

### 常见调整

**校园门户与 IP 网关直连**：打开「策略与配置 → 校园网站直连」，手动填写域名（一行一个，不含协议或路径），保存后再校验、开启链路。已经开启时先停止并回滚。默认列表为空，不内置学校预设。填写学校根域名会覆盖其全部子域名，只放行特定网站时填写完整域名；认证跳转涉及其他域名时也需补入。这里使用真实 DNS 地址，直连时优先 IPv6，允许 IPv4，不经过 WARP 或 VPN。删除域名或清空列表后保存并重新开启，即可取消对应规则。显式例外优先于校园直连。

**视频站不占 VPN 流量**：默认配置就是这样。TCP 走 WARP，UDP 被拒绝后播放器回退 TCP。想换成 VPN 传 UDP，把 `warp_udp` 改成 `airport`。

**只有 IPv4 的服务**：在例外里加 `{"kind":"domain","value":"example.com","route":"ipv4"}`。让该目标走公网 IPv4，删除即撤销。

**AI 走另一个地区**：在「节点分流」里给 AI 服务组单独选节点。

**VPN 入口是域名**：导入时工具会解析一次 AAAA，把能解析到公网 IPv6 的那个节点作为首跳并锁定地址；解析不到 IPv6 的节点不会被选为首跳。

## 两种运行方式

- **独立内核（默认）**：内核与 WARP 均由工具管理，用 `17897` 代理、`17979` 控制、`11081` WARP SOCKS。会临时暂停本机已有的 TUN 并接管系统代理，停止时恢复。
- **接管现有内核**：通过 `external-controller` 热加载覆盖策略，复用现有客户端的管理员能力。开启前校对运行规则与文件、保存原配置与策略组选择。

工具不按过期域名模糊匹配节点：首跳必须唯一命中导入时的节点名与入口，所选出口也必须仍然存在。**订阅改名或换入口后，先停止回滚、更新订阅、重新导入。**

## 监控口径

- **物理网卡**：从已连接的 WLAN/以太网读 Ethernet/IP 头，按远端地址区分公网 IPv4、公网 IPv6、局域网，累计 IP 层长度。包含未经过代理的流量。
- **业务负载**：每秒读内核 `/connections`，用连接 UUID 与开始时间算真实增量。排除隧道进程自身。
- 两侧分别展示、不相加。采样可能漏掉极短连接和关闭前最后一段字节，物理字节也可能受网卡硬件卸载影响。

## 配置项

设置页可以直接改，「高级」里的路径类配置也可以写进 `data/settings.json`。

| 键                                               | 含义                                                                                                                             |
| ------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------- |
| `mode`                                           | `standalone` 自带内核；`attach` 接管已有内核（热加载配置，不改订阅文件）                                                         |
| `source_config`                                  | 完整 mihomo 配置路径；留空则自动查找 Clash Verge 运行时配置                                                                      |
| `kernel_path` / `warp_path` / `warp_account`     | 内核、`warpscout`、WARP 账户路径；留空自动查找                                                                                   |
| `airport_ipv6`                                   | 固定 IPv6 首跳入口。**留空即「仅 WARP」模式**，不加载 VPN 节点。订阅里入口写的是域名也没关系：导入时解析一次 AAAA 并锁定为字面量 |
| `campus_domains`                                 | 内网直连域名（内部门户、统一认证等），走本机直连并优先 IPv6                                                                      |
| `campus_cidrs`                                   | 额外直连的 IPv6 网段                                                                                                             |
| `native_domains`                                 | 强制原生 IPv6 的额外域名                                                                                                         |
| `native_cn`                                      | 国内域名尽量走原生 IPv6（依赖 `geosite.dat`）                                                                                    |
| `warp_domains` / `warp_processes` / `warp_cidrs` | 走 WARP 的域名、进程与 IPv4 网段；默认已含 B 站                                                                                  |
| `warp_udp`                                       | 这些目标的 UDP：`block` 阻断促使回退 TCP，或 `airport` 交给 IPv6 VPN                                                             |
| `warp_enable`                                    | 关闭后不加载 `CL-WARP`，也不生成相关规则                                                                                         |
| `exceptions`                                     | 逐项放宽：`domain`/`process` × `airport`/`ai`/`warp`/`native`/`ipv4`/`block`                                                     |
| `default_node` / `ai_node`                       | 默认出口与 AI 服务组各自选择的节点（需要 VPN）                                                                                   |
| `dns` / `endpoints`                              | IPv6 DNS 上游与 WARP 端点，均为 `[IPv6]:端口`                                                                                    |
| `socks_port` / `mixed_port` / `core_api_port`    | WARP SOCKS、独立代理、独立控制端口                                                                                               |

## 目录

```
app.py                 本机 HTTP 接口与启动入口
campuslink/config.py   设置、节点断言、配置生成、内核校验、WARP 注册
campuslink/deps.py     从官方 Release 下载内核与 warpscout
campuslink/manager.py  开启/回滚、采样、健康检查
campuslink/capture.py  Npcap 被动抓包与地址分类
campuslink/traffic.py  增量计算与 SQLite 存储
campuslink/warp.py     warpscout 守护与端点重扫
web/                   界面（无外部资源）
desktop/               Windows 原生窗口（C# + WebView2）
tools/                 自检、依赖下载与窗口编译
tests/                 离线回归测试
```

## 参考

[mihomo API](https://wiki.metacubex.one/api/) · [mihomo v1.18.6 direct 实现](https://github.com/MetaCubeX/mihomo/blob/v1.18.6/adapter/outbound/direct.go) · [warpscout](https://github.com/vernette/warpscout) · [MetaCubeXD](https://github.com/MetaCubeX/metacubexd) · [Npcap](https://npcap.com/guide/npcap-devguide.html)

本项目直接调用上述组件，未复制它们的界面代码。与 Cloudflare、MetaCubeX、Verge 无隶属关系。

## License

MIT，见 [LICENSE](LICENSE)。
