# DateTime Wheel Picker 1.4.0 OHOS 独立 producer

上游 [darkokoa/compose-datetime-wheel-picker 1.4.0](https://github.com/darkokoa/compose-datetime-wheel-picker/commit/56a15ca7c47fd6b23d155ba73027d29d9693664d) 保留 Apache-2.0 许可与作者 darkokoa。完整 61 份 common 源全部参与编译：其中 60 份从固定 Fork Git blob 读取并与官方 sources JAR 核对，另 1 份 `commonMain/dev/darkokoa/datetimewheelpicker/Strings.kt` 是官方 sources JAR 内的 3921 字节 KSP 生成原件，不属于 Git 原源，也不手工重写或重新运行 Lyricist processor。33 个语言注解和所有原 API 保留。

Wheel 的 Compose runtime、Material3、kotlinx-datetime 和 Lyricist 均保持 `implementation`；所有 Wheel sourceSet 应用 `kotlin.time.ExperimentalTime` opt-in。Lyricist 唯一直接根依赖为公开 Pages 的 `io.github.qsxs.cpf.lyricist:lyricist:1.8.0-cpf11-preview-fad3d9091c75-40827525373-1`，其 `api` 边传递同版 `lyricist-core`；配方要求在三个解析配置及实际编译器输入中核对锁定公开工件身份。不引用旧组或本机 Maven 缓存。

独立根坐标为 `io.github.qsxs.cpf.wheel:datetime-wheel-picker`，预览版本规则为 `1.4.0-cpf11-preview-<sourceSHA12>-<runID>-<attempt>`。固定 KGP、Kotlin/Native、Compose compiler `2.4.20-0.1.0-08`、Compose/Material3 `1.12.0-0.1.0-11`、datetime `0.8.0-0.1.0-07`、coroutines `1.11.0-0.1.0-07`、Gradle 9.5.0 和 JDK 17；先执行标准 KGP metadata，不预设替代入口。

只读 Plan 示例（work 与 staging 须相互分离且位于 Fork checkout 外）：

```sh
python -B ohos/WheelPort.py Plan --source-sha "$SOURCE_SHA" --run-id "$RUN_ID" --run-attempt "$RUN_ATTEMPT" --work "$WORK_DIR" --repository "$STAGING_DIR"
```

Plan 不创建目录、不下载或构建，并报告 `recipeCommitCheck: NOT_RUN`；Prepare 要求完整 `SOURCE_SHA` 包含本配方。首次 Prepare 的 work 和 staging 必须不存在或为空，失败现场不自动清理。`--tooling` 可指定逐文件核对的共享工具 A `d0cff88fd1ea2e4f08bb4409f1c802cca0c7f285`；后续阶段通过 `--native-home` 显式指定官方 `.08` K/N，顺序执行 `Tasks`、`Resolve`、`Metadata`、`NativeX64`、`NativeArm64`、`Publish`、`Verify`。Gradle、K/N 数据及临时目录隔离在 work，发布仅写 staging。

Verify 核对完整来源、原始 POM/GMM/JAR/许可、公开 Lyricist 依赖、GMM/POM scope、metadata/双 ABI、源码和 API 签名；[producer 40839510773/1](https://github.com/qsxs/cpf-port-tooling/actions/runs/40839510773/attempts/1) 已完成 metadata、OHOS x64/arm64、Publish 和 Verify；[Pages 制品提交 `b826affc3704`](https://github.com/qsxs/cpf-maven/commit/b826affc370404ecedcd1b5fca418ad5eeae3940) 已推送，140/140 文件经匿名 HTTP 核对与 staging 的大小、SHA-256 一致，零重试；[consumer 40841841344/1](https://github.com/qsxs/cpf-port-tooling/actions/runs/40841841344/attempts/1) 使用 Gradle 9.5.0 完成 metadata、OHOS x64/arm64 的 Wheel 日期与日期时间探针编译；仅直接依赖 Wheel 根，传递解析 Lyricist 与 core。66 条匿名 HTTP 记录与 staging 对齐，57 条响应 body 随证据上传可本机复哈，9 条仅保留 runner 记录。Producer 完整 compiler argv 和 taskDependencies 未记录或上传；consumer 转换 metadata Klib 及 runner 物理输入未上传，不能本机独立重哈。Gradle 8、link、runtime、设备及 Android/iOS 回归尚未验证。Windows 工作树可能受换行配置影响，Git blob 和官方 JAR 才是来源字节依据。
