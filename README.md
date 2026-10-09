# iOS ARM64 模拟器开发技能

将已有 iOS 工程接入 ARM64 模拟器开发, 自动查找源码和二进制依赖, 复用缓存并支持源码增量构建

安装到 Codex 技能目录:

```bash
git clone https://github.com/wr-fenglei/ios-arm64-simulator-development.git ~/.codex/skills/ios-arm64-simulator-development
```

在项目会话中使用 `$ios-arm64-simulator-development`, 执行流程见 [SKILL.md](SKILL.md), 技能先检查本地构建方式和依赖, 缺少关键材料时再询问

内置脚本适配 CocoaPods 工程, 需要 macOS/Xcode、Python 3.9+ 和含 xcodeproj gem 的 Ruby, 真机二进制转换需接入项目本地工具, 本仓库不附带转换器, 配置见 [依赖发现与适配](references/dependency-adaptation.md)

依赖优先使用原生 ARM64 模拟器库, 真机库只在副本中尝试转换, 修改平台声明不保证运行时兼容, 构建后仍需验证目标功能
