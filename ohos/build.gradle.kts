import groovy.json.JsonOutput
import groovy.json.JsonSlurper
import java.security.MessageDigest
import org.gradle.api.artifacts.Configuration
import org.gradle.api.artifacts.component.ModuleComponentIdentifier
import org.gradle.api.artifacts.result.ResolvedDependencyResult
import org.gradle.api.artifacts.result.UnresolvedDependencyResult
import org.gradle.api.publish.maven.MavenPublication
import org.gradle.api.publish.maven.tasks.PublishToMavenRepository
import org.gradle.jvm.tasks.Jar
import org.jetbrains.kotlin.gradle.dsl.KotlinMultiplatformExtension
import org.jetbrains.kotlin.gradle.tasks.KotlinNativeCompile

plugins {
    kotlin("multiplatform")
    kotlin("plugin.compose")
    id("org.jetbrains.compose")
    `maven-publish`
}
fun readMap(path: File) = (JsonSlurper().parse(path) as Map<*, *>).entries.associate { it.key.toString() to it.value }
fun asMap(value: Any?) = (value as Map<*, *>).entries.associate { it.key.toString() to requireNotNull(it.value) }
fun sha(path: File) = MessageDigest.getInstance("SHA-256").digest(path.readBytes()).joinToString("") { "%02x".format(it) }
fun leaves(path: File): List<File> = if (path.isDirectory) path.walkTopDown().filter(File::isFile).toList() else listOf(path)
fun records(files: Iterable<File>) = files.filter(File::isFile).distinct().sortedBy { it.path }.map {
    mapOf("path" to it.absolutePath, "size" to it.length(), "sha256" to sha(it))
}
val config = readMap(file("port-config.json"))
val model = asMap(asMap(config.getValue("modules")).getValue("datetime-wheel-picker"))
val sourceInputs = readMap(file("source-inputs.json"))
val lyricistInput = readMap(file("lyricist-input.json"))
val lyricistGroup = lyricistInput.getValue("group").toString()
val lyricistVersion = lyricistInput.getValue("version").toString()
val lyricistModules = asMap(lyricistInput.getValue("modules"))
check(lyricistGroup == "io.github.qsxs.cpf.lyricist" && lyricistVersion == "1.8.0-cpf11-preview-fad3d9091c75-40827525373-1")
check(gradle.gradleVersion == config.getValue("gradle") && System.getProperty("java.specification.version") == "17")
val pins = readMap(file("dependency-pins.json"))
val metadataPins = if (file("metadata-pins.json").isFile) readMap(file("metadata-pins.json")) else emptyMap()
val compilerPluginTools = readMap(file("compiler-plugin-toolchain.json"))
val compilerPluginPins = asMap(compilerPluginTools.getValue("pins"))
val compilerPluginArtifacts = asMap(compilerPluginTools.getValue("artifacts"))
val toolRoot = file(providers.gradleProperty("portTooling").get()).resolve("toolchains/kotlin-native/2.4.20-0.1.0-08")
val metadataTools = readMap(toolRoot.resolve("metadata-toolchain.json"))
val commonizerTools = readMap(toolRoot.resolve("commonizer-toolchain.json"))
val nativeTools = readMap(toolRoot.resolve("native-compiler-toolchain.json"))
val commonizerPins = asMap(metadataTools.getValue("launcherPins")) + asMap(commonizerTools.getValue("pins"))
val commonizerArtifacts = asMap(metadataTools.getValue("launcherArtifacts")) + asMap(commonizerTools.getValue("artifacts"))
val nativePins = asMap(nativeTools.getValue("pins"))
val nativeArtifacts = asMap(nativeTools.getValue("artifacts"))
val fingerprint = providers.gradleProperty("portFingerprint").get()
val work = file(providers.gradleProperty("portWork").get()).canonicalFile
check(fingerprint.matches(Regex("[0-9a-f]{64}")))
val manifestFile = work.resolve("source-manifest.json")
val manifest = readMap(manifestFile)
check(manifest["fingerprint"] == fingerprint)
group = model.getValue("group").toString()
version = providers.gradleProperty("portVersion").get()
layout.buildDirectory.set(work.resolve("build"))
kotlin {
    ohosX64()
    ohosArm64()
    sourceSets {
        all { languageSettings.optIn("kotlin.time.ExperimentalTime") }
        val commonMain by getting {
            kotlin.setSrcDirs(listOf(work.resolve("sources/commonMain")))
            dependencies {
                implementation("org.jetbrains.compose.runtime:runtime:1.12.0-0.1.0-11")
                implementation("org.jetbrains.compose.material3:material3:1.12.0-0.1.0-11")
                implementation("org.jetbrains.kotlinx:kotlinx-datetime:0.8.0-0.1.0-07")
                implementation("$lyricistGroup:lyricist:$lyricistVersion")
            }
        }
        getByName("ohosX64Main").dependsOn(commonMain)
        getByName("ohosArm64Main").dependsOn(commonMain)
        matching { it.name.endsWith("Test") }.configureEach { kotlin.setSrcDirs(emptyList<File>()) }
    }
}
configurations.configureEach {
    if (name == nativeTools["configuration"] || name == commonizerTools["configuration"]) {
        val fixedPins = if (name == nativeTools["configuration"]) nativePins else commonizerPins
        resolutionStrategy.eachDependency {
            val ga = "${requested.group}:${requested.name}"
            val fixed = fixedPins[ga]?.toString() ?: error("Unpinned compiler host dependency: $ga")
            if (requested.version != fixed) useVersion(fixed)
        }
    }
    val metadata = name.endsWith("ResolvableDependenciesMetadata") || name == "allSourceSetsCompileDependenciesMetadata"
    val runtime = metadata || name.endsWith("CompileKlibraries") || name.endsWith("CompilationDependenciesMetadata")
    if (runtime && !name.contains("Test", ignoreCase = true)) resolutionStrategy.eachDependency {
        check(requested.group != "cafe.adriel.lyricist") { "Old Lyricist group is not allowed" }
        val ga = "${requested.group}:${requested.name}"
        val fixed = (if (metadata) metadataPins[ga] ?: pins[ga] else pins[ga])?.toString()
            ?: error("Unpinned Wheel dependency: $requested")
        if (requested.version != fixed) useVersion(fixed)
    }
}
fun evidence(name: String, value: Any) {
    val target = work.resolve("evidence/$name.json")
    target.parentFile.mkdirs()
    target.writeText(JsonOutput.prettyPrint(JsonOutput.toJson(value)) + "\n")
}
fun edges(configuration: Configuration) = configuration.incoming.resolutionResult.allDependencies.map { edge ->
    if (edge !is ResolvedDependencyResult) throw GradleException("Unresolved Wheel dependency: $edge", (edge as? UnresolvedDependencyResult)?.failure)
    mapOf("from" to edge.from.id.displayName, "requested" to edge.requested.displayName, "selected" to edge.selected.id.displayName)
}
fun checkedTools(name: String, fixed: Map<String, Any>, expected: Map<String, Any>): Map<String, Any> {
    val configuration = configurations.getByName(name)
    val artifacts = configuration.resolvedConfiguration.resolvedArtifacts.map { artifact ->
        val id = artifact.moduleVersion.id
        val ga = "${id.group}:${id.name}"
        check(fixed[ga] == id.version) { "Host tool version differs: $id" }
        val item = asMap(expected.getValue(ga))
        check(sha(artifact.file) == item["sha256"] && artifact.file.length() == (item.getValue("size") as Number).toLong()) { "Host tool bytes differ: $id" }
        mapOf("gav" to id.toString(), "files" to records(listOf(artifact.file)))
    }
    check(artifacts.map { it.getValue("gav") }.toSet() == fixed.map { "${it.key}:${it.value}" }.toSet()) { "Host tool set differs: $name" }
    return mapOf("configuration" to name, "edges" to edges(configuration), "artifacts" to artifacts)
}
fun toolRecords() = mapOf("commonizer" to checkedTools(commonizerTools.getValue("configuration").toString(), commonizerPins, commonizerArtifacts),
    "nativeCompiler" to checkedTools(nativeTools.getValue("configuration").toString(), nativePins, nativeArtifacts))

fun toolchain() = mapOf(
    "hostTools" to toolRecords(),
    "gradle" to gradle.gradleVersion,
    "javaVersion" to System.getProperty("java.version"),
    "javaRuntimeVersion" to System.getProperty("java.runtime.version"),
    "javaHome" to System.getProperty("java.home"),
    "nativeHome" to providers.gradleProperty("kotlin.native.home").get(),
    "konanDataDir" to System.getenv("KONAN_DATA_DIR"),
    "gradleUserHome" to gradle.gradleUserHomeDir.absolutePath,
    "temp" to System.getenv("TEMP"),
    "plugins" to buildscript.configurations.getByName("classpath").resolvedConfiguration.resolvedArtifacts.map {
        mapOf("component" to it.moduleVersion.id.toString(), "files" to records(listOf(it.file)))
    },
)
fun resolution() = listOf("commonMainResolvableDependenciesMetadata", "ohosX64CompileKlibraries", "ohosArm64CompileKlibraries").map { name ->
    val configuration = configurations.getByName(name)
    val target = when (name) { "ohosX64CompileKlibraries" -> "ohos_x64"; "ohosArm64CompileKlibraries" -> "ohos_arm64"; else -> null }
    val found = mutableMapOf<String, Int>()
    val artifacts = configuration.incoming.artifacts.artifacts.map { artifact ->
        val id = artifact.id.componentIdentifier
        check(id is ModuleComponentIdentifier)
        val attrs = artifact.variant.attributes.keySet().associate { it.name to artifact.variant.attributes.getAttribute(it).toString() }
        if (id.group == lyricistGroup) {
            val module = lyricistModules.keys.singleOrNull { id.module in listOf(it, "$it-ohosx64", "$it-ohosarm64") }
                ?: error("Unexpected public Lyricist component: $id")
            val lock = asMap(lyricistModules.getValue(module))
            val expected = if (target == null) asMap(lock.getValue("metadataArtifact")) else asMap(asMap(lock.getValue("nativeArtifacts")).getValue(target))
            check(id.version == lyricistVersion && sha(artifact.file) == expected["sha256"] && artifact.file.length() == (expected.getValue("size") as Number).toLong())
            if (target == null) check(id.module == module && attrs["org.jetbrains.kotlin.platform.type"] == "common" && attrs["org.gradle.usage"] == "kotlin-metadata")
            else check(id.module in listOf(module, module + if (target == "ohos_x64") "-ohosx64" else "-ohosarm64") &&
                attrs["org.jetbrains.kotlin.native.target"] == target && attrs["org.gradle.usage"] == "kotlin-api")
            found[module] = (found[module] ?: 0) + 1
        } else {
            val ga = "${id.group}:${id.module}"
            check((if (target == null) metadataPins[ga] ?: pins[ga] else pins[ga]) == id.version) { "Unpinned Wheel dependency: $id" }
            if (metadataPins.containsKey(ga)) {
                val common = attrs["org.gradle.usage"] == "kotlin-metadata" && attrs["org.jetbrains.kotlin.platform.type"] == "common"
                val javaAlias = ga == "androidx.arch.core:core-common" && id.version == "2.2.0" &&
                    attrs["org.gradle.usage"] in listOf("java-api", "java-runtime") && attrs["org.jetbrains.kotlin.platform.type"] in listOf(null, "jvm") && attrs["org.gradle.category"] == "library"
                check(target == null && (common || javaAlias) && attrs["org.jetbrains.kotlin.native.target"] == null)
            }
        }
        mapOf("component" to id.displayName, "attributes" to attrs, "files" to records(leaves(artifact.file)))
    }
    check(lyricistModules.keys.all { found[it] == 1 }) { "Incomplete Lyricist public graph: $found" }
    val graph = edges(configuration)
    check(graph.any { it.getValue("requested") == "$lyricistGroup:lyricist:$lyricistVersion" })
    check(graph.any { edge ->
        val from = edge.getValue("from").toString()
        from in listOf("$lyricistGroup:lyricist:$lyricistVersion", "$lyricistGroup:lyricist-ohosx64:$lyricistVersion", "$lyricistGroup:lyricist-ohosarm64:$lyricistVersion") &&
            edge.getValue("requested") == "$lyricistGroup:lyricist-core:$lyricistVersion"
    }) { "Lyricist api edge to same-version core missing" }
    mapOf("configuration" to name, "target" to target, "edges" to graph, "artifacts" to artifacts)
}
fun payload(file: File): Map<String, Map<String, Any>> {
    val result = mutableMapOf<String, Map<String, Any>>()
    fun add(name: String, bytes: ByteArray) {
        val normalized = name.removePrefix("commonMain/")
        if (normalized.startsWith("default/")) result[normalized] = mapOf("size" to bytes.size.toLong(), "sha256" to MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) })
    }
    if (file.isDirectory) leaves(file).forEach { add(it.relativeTo(file).invariantSeparatorsPath, it.readBytes()) }
    else if (file.isFile && file.extension in listOf("jar", "klib", "bin")) java.util.zip.ZipFile(file).use { zip ->
        zip.entries().asSequence().filter { !it.isDirectory }.forEach { add(it.name, zip.getInputStream(it).use { stream -> stream.readBytes() }) }
    }
    return result
}
fun lyricistCompilerInputs(compiler: KotlinNativeCompile, target: String?): Map<String, Any> {
    val candidates = compiler.libraries.files
    return lyricistModules.keys.associateWith { module ->
        val lock = asMap(lyricistModules.getValue(module))
        val matches = if (target == null) {
            val expected = asMap(lock.getValue("metadataPayload")).mapValues { (_, value) ->
                val row = asMap(value)
                mapOf("size" to (row.getValue("size") as Number).toLong(), "sha256" to row.getValue("sha256"))
            }
            candidates.filter { payload(it) == expected }
        } else {
            val expected = asMap(asMap(lock.getValue("nativeArtifacts")).getValue(target))
            candidates.filter { it.isFile && it.length() == (expected.getValue("size") as Number).toLong() && sha(it) == expected["sha256"] }
        }
        check(matches.size == 1) { "Compiler input cannot be traced to published Lyricist: $module / $target" }
        mapOf("compilerLibrary" to matches.single().absolutePath, "files" to records(leaves(matches.single())))
    }
}

tasks.register("resolveWheel") {
    doLast {
        check(gradle.gradleVersion == config.getValue("gradle") && System.getProperty("java.specification.version") == "17")
        listOf("compileCommonMainKotlinMetadata", "allMetadataJar", "compileKotlinOhosX64", "compileKotlinOhosArm64").forEach { tasks.getByName(it) }
        evidence("resolve", mapOf("fingerprint" to fingerprint, "toolchain" to toolchain(),
            "configurations" to resolution()))
    }
}
fun recordStage(name: String, target: String?) {
    tasks.register(name) {
        val compilerName = if (target == null) "compileCommonMainKotlinMetadata" else "compileKotlin$target"
        val producerName = compilerName
        dependsOn(producerName)
        if (target == null) dependsOn("allMetadataJar")
        outputs.upToDateWhen { false }
        doLast {
            val compiler = tasks.getByName(compilerName) as KotlinNativeCompile
            val sources = compiler.sources.files.filter { it.isFile && it.extension == "kt" }.map { it.canonicalFile }.toSet()
            val pluginFiles = compiler.compilerPluginClasspath?.files.orEmpty()
            val pluginIds = listOf("org.jetbrains.kotlin:kotlin-compose-compiler-plugin-embeddable")
            val expectedPlugins = pluginIds.map { id ->
                check(compilerPluginPins[id] == config["kotlin"])
                val item = asMap(compilerPluginArtifacts.getValue(id))
                (item.getValue("size") as Number).toLong() to item.getValue("sha256").toString()
            }.toSet()
            check(expectedPlugins.size == pluginIds.size && pluginFiles.size == pluginIds.size &&
                pluginFiles.map { it.length() to sha(it) }.toSet() == expectedPlugins) {
                "Wheel compiler plugin set differs"
            }
            // 从配方中的官方清单推导路径与hash，不把可变work manifest当作原源真值。
            val originalMembers = (sourceInputs.getValue("sourceMembers") as List<*>).map(::asMap)
            val stageMembers = originalMembers
            val expected = stageMembers.associate { work.resolve("sources/" + it.getValue("member")).canonicalFile to it.getValue("sha256").toString() }
            check(expected.size == 61 && sources == expected.keys && expected.all { (path, hash) -> sha(path) == hash }) {
                "Complete Wheel compiler source set or original bytes differ"
            }
            val producers = listOf(tasks.getByName(producerName)) + if (target == null) listOf(tasks.getByName("allMetadataJar")) else emptyList()
            producers.forEach { producer ->
                val state = producer.state
                check(state.executed && producer.enabled && !state.noSource && state.failure == null &&
                    state.skipMessage in listOf(null, "UP-TO-DATE", "FROM-CACHE")) { "Wheel producer did not execute: ${producer.path}" }
                check(records(producer.outputs.files.files.flatMap(::leaves)).any { (it.getValue("size") as Long) > 0 })
            }
            val stage = if (target == null) "metadata" else if (target == "OhosX64") "native-x64" else "native-arm64"
            evidence(stage, mapOf("fingerprint" to fingerprint, "stage" to stage, "sourceCount" to expected.size,
                "sourceSetSha256" to sourceInputs.getValue("sourceSetSha256"), "sourceManifest" to records(listOf(manifestFile)).single(),
                "sources" to records(sources), "publicApi" to sourceInputs.getValue("publicApi"), "toolchain" to toolchain(),
                "explicitApi" to project.extensions.getByType<KotlinMultiplatformExtension>().explicitApi?.name,
                "sourceDependencies" to listOf("implementation" to "commonMainImplementation", "api" to "commonMainApi").associate { (scope, configuration) ->
                    scope to configurations.getByName(configuration).dependencies.map { "${it.group}:${it.name}" }.filter { it != "org.jetbrains.kotlin:kotlin-stdlib" }
                },
                "compilerPlugins" to records(compiler.compilerPluginClasspath?.files.orEmpty()), "pluginOptions" to compiler.compilerPluginCommandLine,
                "metadataLanguageOptions" to if (target == null) mapOf("languageVersion" to compiler.compilerOptions.languageVersion.orNull?.version,
                    "apiVersion" to compiler.compilerOptions.apiVersion.orNull?.version, "optIns" to compiler.compilerOptions.optIn.get().sorted(),
                    "freeCompilerArgs" to compiler.compilerOptions.freeCompilerArgs.get(),
                    "explicitApi" to project.extensions.getByType<KotlinMultiplatformExtension>().explicitApi?.name) else null,
                "compilerLibraries" to compiler.libraries.files.sortedBy { it.path }.map {
                    mapOf("path" to it.absolutePath, "files" to records(leaves(it)))
                },
                "lyricistCompilerInputs" to lyricistCompilerInputs(compiler, if (target == null) null else if (target == "OhosX64") "ohos_x64" else "ohos_arm64"),
                "producers" to producers.map { mapOf("task" to it.path, "outputs" to records(it.outputs.files.files.flatMap(::leaves))) },
                "configurations" to resolution(), "runtime" to "NOT_RUN"))
        }
    }
}
recordStage("recordWheelMetadata", null)
recordStage("recordWheelX64", "OhosX64")
recordStage("recordWheelArm64", "OhosArm64")
publishing {
    repositories { maven { name = "Port"; url = uri(providers.gradleProperty("portRepository").get()) } }
    publications.withType<MavenPublication>().configureEach {
        artifact(file("legal/LICENSE")) { classifier = "license"; extension = "txt" }
        artifact(work.resolve("provenance.json")) { classifier = "provenance"; extension = "json" }
        if (name == "kotlinMultiplatform") {
            (sourceInputs.getValue("originalClassifiers") as List<*>).map(::asMap).forEach { original ->
                val key = original.getValue("key").toString()
                artifact(file(asMap(sourceInputs.getValue(key)).getValue("path"))) {
                    classifier = original.getValue("classifier").toString()
                    extension = original.getValue("extension").toString()
                }
            }
        }
        pom {
            name.set("datetime-wheel-picker")
            description.set("Complete Wheel 1.4.0 original and official generated sources for CPF OHOS dual ABI")
            licenses { license { name.set("The Apache Software License, Version 2.0"); url.set("http://www.apache.org/licenses/LICENSE-2.0.txt") } }
            scm { url.set(model.getValue("repository").toString()) }
            developers { developer { id.set("darkokoa"); name.set("darkokoa"); url.set("https://github.com/darkokoa/") } }
        }
    }
}
tasks.withType<PublishToMavenRepository>().configureEach {
    doFirst {
        check(readMap(work.resolve("provenance.json"))["fingerprint"] == fingerprint)
        (sourceInputs.getValue("rawOriginals") as List<*>).map(::asMap).forEach { original ->
            check(sha(file(original.getValue("path"))) == original.getValue("sha256"))
        }
        listOf("metadata", "native-x64", "native-arm64").forEach { check(readMap(work.resolve("evidence/$it.json"))["fingerprint"] == fingerprint) }
    }
}
tasks.withType<Jar>().configureEach {
    isPreserveFileTimestamps = false
    isReproducibleFileOrder = true
    from(file("legal")) { into("META-INF/licenses/datetime-wheel-picker") }
    from(manifestFile) { into("META-INF/upstream") }
}
tasks.register("publishWheel") { dependsOn("publishAllPublicationsToPortRepository") }

// 每个 sources artifact 保留 60 份原源及官方 JAR 的生成源。
tasks.withType<Jar>().matching { it.name.endsWith("SourcesJar", ignoreCase = true) }.configureEach {
    duplicatesStrategy = DuplicatesStrategy.EXCLUDE
    from(work.resolve("sources")) { include("**/*.kt") }
}
