# 单机安装后接入 TitanX

Linux 内网工位安装包自带 TitanX Edge 和配套 case-cli。安装信息文件 `/usr/lib/decklab/installation.json` 中的 `titanx_worker` 为 `true` 时可使用此入口。先在 TitanX 分配唯一节点 ID 和服务器 100 下的 COM 号段。已有同名 Edge 的工位在空闲时停止原实例，避免两个客户端抢占同一节点。

在工位创建 `/etc/decklab/titanx.json`，文件属主为 `root:decklab`，权限为 `0640`。可先写到同目录临时文件，设置属主和权限后再改名。单板示例为

```json
{
  "node_id": "bench-01",
  "broker": "ssl://mqtt.example.test:8883",
  "first_com": 1
}
```

需要 MQTT 认证时加入 `username` 和 `password`。需要在 DeckLab 查看用例、重新派发时，可以在同一文件填写 `api_url`（TitanX 的 HTTPS 根地址）与 `api_token`。已有网页团队用例设置或平台环境变量仍优先使用。平台令牌只供 DeckLab 查询和派发使用。

需要自动建档时，在同一文件加入以下字段。平台账号须有实验室的节点和板卡读取、新增权限。`web_origin` 填写开发者浏览器可访问的 DeckLab HTTPS 地址，`lab_id` 为 TitanX 实验室编号，`project` 为项目类型。

```json
{
  "node_id": "bench-01",
  "broker": "ssl://mqtt.example.test:8883",
  "api_url": "https://titanx.example.test",
  "api_token": "<平台服务令牌>",
  "lab_id": 1,
  "project": "K1",
  "web_origin": "https://bench-01.example.test",
  "accept_dispatch": true
}
```

登记服务会核对 Edge 在 TitanX 上报的板号、槽位和 COM，再创建档案。新档案默认关闭自动恢复；`accept_dispatch` 缺省时关闭自动调度。已匹配档案沿用管理员的调度设置。已有同槽位或同 COM 的原平台档案需要先由管理员核对并迁移。通过 `journalctl -u decklab-titanx-enroll.service` 查看结果。登记响应中断时，服务保留本次提交记录；登记服务自动回查原档，重启后也沿用同一记录。若平台确认未建立该记录，可移走数据目录内的 `titanx-enroll-pending.json` 再重试。

首次保存配置后，systemd 自动启动工位。单板工位自动绑定首次确认的板号到槽位 1，之后重启沿用原板号。多板或换板时显式填写 `"slots": {"1": "SAMPLE-BOARD-A", "2": "SAMPLE-BOARD-B"}`。只列要接管的板，其他板继续由原平台管理。槽位 N 的 COM 为 `first_com + N - 1`。DeckLab 逐板签发设备授权，生成 Edge 配置。配套的 TitanX 设备服务会为该工位的 PC 用例选用随包的 case-cli。

运行 `systemctl status decklab-titanx.service` 查看连接状态。已有配置改动在作业释放后用 `systemctl restart decklab-titanx.service` 应用。停用时在空闲窗口移走配置并停止该服务，撤销对应设备授权。TitanX 档案登记、接受调度与失败轮次的回链依赖平台侧的接口和权限。
