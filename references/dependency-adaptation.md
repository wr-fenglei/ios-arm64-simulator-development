# 依赖发现与适配

## 从本地证据识别构建方式

先找工程构建入口和依赖生成脚本, 跟踪插件、导入模块及构建阶段实际调用的命令, 对照源码编译项、链接输入和复制产物, 判断是否存在预编译缓存替代源码编译的能力, 查明使用什么实现以及在哪一步替换

沿这些引用定位当前工程实际使用的二进制, 核对版本、架构和目标平台, 不扫描全局缓存后假定全部使用, 若源码仍在但编译项为空, 回查生成逻辑是否移除了编译项, 若构建阶段复制库, 查明来源和最终链接位置

列出材料位置、实现入口、替换时机及用于复用/转换/源码重编的阶段, 证据明确就继续, 缺材料或版本有歧义时只问缺失项, 不把某个工程的目录、阶段名称或组件名单写入通用技能

恢复源码时根据发现的机制解除缓存替换, 同时核对编译项、模块声明、资源和生成文件, 防止新源码仍被缓存覆盖

## CocoaPods 配置入口

内置脚本处理 CocoaPods 生成工程, 其他依赖系统需要按实际机制适配, `discover` 列出标准材料和项目脚本, 指定项目 Ruby 可读取真实构建阶段, `--cache-root` 只接受 agent 已从构建引用确认的目录, 输出留在本地

```bash
python3 scripts/prepare.py discover --root /path/to/project --ruby /path/to/project-ruby
python3 scripts/prepare.py discover --root /path/to/project --cache-root /path/to/confirmed-cache
python3 scripts/prepare.py prepare --config /path/to/arm64-simulator.json
python3 scripts/prepare.py build --config /path/to/arm64-simulator.json --device SIMULATOR_ID
python3 scripts/package.py --source /path/to/simulator-build.app --output /path/to/new-signed.app
xcrun simctl install SIMULATOR_ID /path/to/new-signed.app
xcrun simctl launch SIMULATOR_ID APP_BUNDLE_ID
```

配置留在目标工程, 以下路径和阶段名称均为占位示例, 必须来自本地检查, 没有缓存时显式使用空列表, 锁摘要变化时重新核对

```json
{
  "root": ".",
  "workspace": "App.xcworkspace",
  "scheme": "App",
  "conversion_command": ["/path/to/local-tool", "--source", "{source}", "--output", "{output}", "--deployment", "{deployment_target}"],
  "tool_inputs": ["scripts/local-tool", "scripts/local-tool-implementation"],
  "ruby": "/path/to/project-ruby",
  "lock_sha256": "当前 Podfile.lock 的 SHA256",
  "deployment_target": "工程最低系统版本",
  "cache_roots": [{"path": "Pods/BinaryCache/frameworks", "overlay_path": "BinaryCache/frameworks"}],
  "cache_phase_names": ["本地确认的缓存复制阶段"],
  "prepared_phase_names": ["已由本次准备替代的二进制生成阶段"],
  "generated_pods_paths": ["BinaryDependencies"],
  "vendor_output": "BinaryDependencies/arm64",
  "source_fallbacks": []
}
```

`cache_roots` 对应被实际引用的 XCFramework 目录及配置副本中的相对位置, `cache_phase_names` 用于源码恢复目标, `prepared_phase_names` 只列已确认可由本次准备替代的阶段, 不按名称猜测并删除其他构建逻辑, `generated_pods_paths` 指向需引用副本的生成目录, 可选 `vendor_config` 使用工程提供的库选择清单, 格式不匹配时调整适配器

准备只复制生成配置和缓存, 业务源码引用原位置, 保留 Development Pods 相对路径基准, 日常保留独立 DerivedData 支持增量编译, 新增缓存源码开发目标前先解除对应替换

## 工具与验证边界

脚本需要 macOS/Xcode、Python 3.9+、含 xcodeproj gem 的 Ruby, 工程检查与配置处理只使用通用 API, 项目转换工具通过 `conversion_command` 参数列表调用, 不通过 shell 拼接命令, 支持 `{root}`、`{source}`、`{output}`、`{deployment_target}` 占位符, 工具应写入新输出且不得修改输入

工具接口不一致时只在目标工程建立本地桥接, 项目实现和桥接不得进入公开仓库, `tool_inputs` 列出桥接及其加载的实现文件以检测变化, 未找到可用工具时继续定位或单独适配, 不假定已有转换能力, 独立校验输出架构、目标平台、定义符号、搜索路径和动态二进制签名

当前构建通过命令行传入独立 PODS_ROOT 和 arm64, 生成 workspace 可打开不代表 Xcode 界面 Run 已验收, 项目重新生成配置后再次准备, 安装启动和目标页面须分别验证

控制台日志可通过 `simctl launch --stdout=/path/out.log --stderr=/path/err.log` 写普通文件, 避免无人读取的管道阻塞 App

回退使用原 workspace, 只删除本次新增入口、配置及生成目录, 不使用 reset/clean 清理既有改动, 修改平台声明仍有运行时兼容风险, 已验证实例曾有 libpag 解码崩溃及 SwiftLint 未执行
