# 板桥 · BoardBridge

“板桥”连接开发板、开发者与 Agent。产品定位是面向 OS/BSP 开发的共享开发板工作台，覆盖串口、SSH、启动日志、文件传输和协作访问。

图标上半部为桥梁，下半部为终端提示符 `>_`。石墨色 `#17292c` 提供稳定底色，薄荷绿 `#85e0c2` 表示连接，暖白 `#f3faf7` 表示终端交互。图形不包含文字，适合浏览器标签页与桌面小图标。

- `static/boardbridge.svg`：原创、可编辑的矢量源文件，用于网页和 favicon。
- `static/boardbridge.png`：512 × 512 透明背景导出，用于分享与触屏图标。
- `packaging/boardbridge.ico`：16、24、32、48、64、128、256 像素的 Windows 图标。

应用界面和安装器使用 BoardBridge 品牌。`seriald` 项目名、命令、服务、安装 AppId 和数据目录继续使用既有名称，确保升级仍识别已有安装。
