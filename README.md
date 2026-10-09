# iOS ARM64 模拟器开发技能

将已有 iOS 工程接入 ARM64 模拟器开发, 主工程重新构建, 依赖优先用原生模拟器库, 真机库仅在独立副本中尝试转换

安装到 Codex 技能目录, 然后使用 `$ios-arm64-simulator-development`, 入口见 [SKILL.md](SKILL.md)

```bash
git clone https://github.com/wr-fenglei/ios-arm64-simulator-development.git ~/.codex/skills/ios-arm64-simulator-development
```

技能先沿本地生成脚本、构建阶段和链接输入识别依赖机制, 定位源码、缓存及实现入口, 找不到关键材料时再询问

内置脚本支持通过项目配置适配 CocoaPods 生成工程, 缓存位置和阶段名称均从本地发现, 依赖工程已有转换工具, 入口与配置见 [依赖发现与适配](references/dependency-adaptation.md), 本仓库不附带项目转换工具、业务源码、二进制缓存或实例组件名单

公开脚本使用 Python 标准库、系统命令和 CocoaPods 的 xcodeproj API, 项目转换工具通过配置的参数列表调用, 专用实现与调用桥接仅保留在目标工程

已在一个实际工程完成转换、构建、安装、未登录页面浏览和源码增量执行, 测得重复准备约5秒, 单次源码增量构建约99秒, 数字取决于工程与机器

修改平台声明不能保证全部运行时功能兼容, 实例曾有 libpag 解码崩溃, SwiftLint 未执行, Xcode 界面 Run 尚未验收, 需要按目标工程检查所用功能
