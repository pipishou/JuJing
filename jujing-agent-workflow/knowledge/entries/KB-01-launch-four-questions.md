# KB-01 · 上线前逐问留证

> 标签：部署/发布
> 形态：通用（三形态均适用）
> 锚点（供探针抽样）：`上线四问`
> 状态：candidate（复现 1，门限见 assets/params.json#kb.promote_min_projects）
> 现落点：候选储备（曾落 stages⑤ 版本指纹区，09-27 随 P2 改号下沉）
> 溯源：whalemementor 2026-09-22（发版/部署五连 004→007）
> 隐私（privacy）：public

## 经验正文

宣布「上线完成」前，对可观测四项逐问留证（命令＋输出关键行＋日期，缺一即不得宣布）：

1. 服务进程真的起了吗（进程/容器状态，非部署返回值）；
2. 健康检查通了吗（对运行中实例探测，非本地构建绿）；
3. 新代码真在上（指纹/版本断言，防缓存与旧实例接管）；
4. 启动日志有无异常首行（异常先于用户发现）。

**为什么**：部署接口返回成功 ≠ 实例接管 ≠ 新版本生效，三层各会静默失败；逐问留证把「上线完成」从声明变成可核事实（公理 1）。

## 晋升与退役

- 晋升：被 `assets/params.json#kb.promote_min_projects` 个独立项目复现 → 升 stages/05 正文（本条状态改 `promoted`，行保留不删）。
- 退役：连续 `assets/params.json#probe.retire_window` 轮探针抽中零消费 → `retire-candidate`（人工确认＋棘轮后才 `retired`；消费留痕协议见 `knowledge/INDEX.md` 头部）。
