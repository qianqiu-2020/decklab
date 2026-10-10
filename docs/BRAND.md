# DeckLab

DeckLab 是面向 OS/BSP 开发者的共享开发板工作台。“Deck”表示开发板与工作台，“Lab”表示开发、调试与实验。

图标使用钴蓝色 `#3158df` 圆角底板、白色几何 D 字标和浅蓝色 `#b4c8ff` 信号连接点。减少细小装饰，便于在桌面和浏览器标签页识别。

- `static/decklab.svg`：原创、可编辑的矢量源文件，用于网页和 favicon。
- `static/decklab.png`：512 × 512 透明背景导出。
- `packaging/decklab.ico`：16、24、32、48、64、128、256 像素的 Windows 图标。

Windows 构建把 ICO 复制到程序包，以内容摘要命名，并记录在 `app-icon-name.txt`。安装器及快捷方式使用该文件；EXE 也嵌入相同图标。

应用界面和安装器使用 DeckLab 品牌。`seriald` 项目名、命令、服务、安装 AppId 和数据目录继续使用既有名称，升级仍识别已有安装。
