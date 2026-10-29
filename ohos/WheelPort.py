"""Wheel 1.4.0 的独立 OHOS producer；不部署、不读取凭据。"""
from __future__ import annotations

import argparse
import time
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
from urllib.request import ProxyHandler, build_opener
import xml.etree.ElementTree as ET
import zipfile

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
TOOL_REVISION = "d0cff88fd1ea2e4f08bb4409f1c802cca0c7f285"
TOOL_DIR = "toolchains/kotlin-native/2.4.20-0.1.0-08"
GROUP = "io.github.qsxs.cpf.wheel"
MODULE = "datetime-wheel-picker"
ROOT = HERE.parent


def physical(path):
    """Windows 文件系统操作使用扩展路径；其他平台保持原路径。"""
    if os.name != "nt":
        return Path(path)
    absolute = os.path.abspath(path)
    if absolute.startswith("\\\\?\\"):
        return Path(absolute)
    if absolute.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + absolute[2:])
    return Path("\\\\?\\" + absolute)


def logical_path(path):
    text = os.fspath(path)
    if os.name == "nt" and text[:8].upper() == "\\\\?\\UNC\\":
        return Path("\\\\" + text[8:])
    if os.name == "nt" and text.startswith("\\\\?\\"):
        return Path(text[4:])
    return Path(path)


def logical_resolve(path):
    return logical_path(physical(path).resolve())


TASKS = {"Tasks": ["tasks", "--all"], "Resolve": ["resolveWheel"], "Metadata": ["recordWheelMetadata"],
         "NativeX64": ["recordWheelX64"], "NativeArm64": ["recordWheelArm64"], "Publish": ["publishWheel"]}
COMPILES = (("Metadata", "metadata", None), ("NativeX64", "native-x64", "ohos_x64"),
            ("NativeArm64", "native-arm64", "ohos_arm64"))
REQUIRED = {"resolveWheel", "compileCommonMainKotlinMetadata", "compileKotlinOhosX64",
            "compileKotlinOhosArm64", "allMetadataJar", "recordWheelMetadata", "recordWheelX64", "recordWheelArm64", "publishWheel"}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def record(path):
    path = logical_resolve(path)
    data = physical(path).read_bytes()
    return {"path": str(path), "size": len(data), "sha256": digest(data)}


def load(path):
    return json.loads(physical(path).read_text(encoding="utf-8"))


def write(path, value, *, exclusive=False):
    path = logical_path(path)
    physical(path.parent).mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if exclusive:
        with physical(path).open("x", encoding="utf-8") as output:
            output.write(text)
    else:
        physical(path).write_text(text, encoding="utf-8")


def safe_environment(work=None):
    env = {key: os.environ[key] for key in ("PATH", "HOME", "USERPROFILE", "SystemRoot", "ComSpec", "JAVA_HOME", "LANG", "LC_ALL", "SSL_CERT_FILE",
                                              "SSL_CERT_DIR", "LD_LIBRARY_PATH") if key in os.environ}
    if work is not None:
        for key, folder in (("GRADLE_USER_HOME", "gradle-home"), ("KONAN_DATA_DIR", "konan"),
                            ("TMPDIR", "temp"), ("TEMP", "temp"), ("TMP", "temp")):
            path = Path(work) / folder
            physical(path).mkdir(parents=True, exist_ok=True)
            env[key] = str(path)
    return env


def checked_tools(work, explicit):
    lock = load(HERE / "tooling-lock.json")
    if lock["revision"] != TOOL_REVISION or lock["repository"] != "https://github.com/qsxs/cpf-port-tooling":
        raise RuntimeError("Immutable shared tooling revision differs")
    if lock["archiveUrl"] != lock["repository"] + "/archive/" + TOOL_REVISION + ".tar.gz":
        raise RuntimeError("Shared tooling archive URL differs")
    if len(lock["files"]) != 20 or len({row["path"] for row in lock["files"]}) != 20:
        raise RuntimeError("Shared tooling file lock differs")
    directory = logical_resolve(explicit) if explicit else logical_resolve(work / "tooling" / TOOL_REVISION)
    for row in lock["files"]:
        relative = PurePosixPath(row["path"])
        if relative.is_absolute() or ".." in relative.parts or "\\" in row["path"] or not row["path"].startswith(TOOL_DIR + "/"):
            raise RuntimeError("Unsafe shared tooling file path")
    if not explicit and not all(physical(directory / row["path"]).is_file() for row in lock["files"]):
        # 仅读取锁定文件；归档压缩容器本身不作为内容身份。
        with build_opener(ProxyHandler({})).open(lock["archiveUrl"], timeout=60) as response:
            data = response.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024:
            raise RuntimeError("Shared tooling archive is too large")
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            members = archive.getmembers()
            for row in lock["files"]:
                matches = [member for member in members if "/" in member.name and member.name.split("/", 1)[1] == row["path"]]
                if len(matches) != 1 or not matches[0].isfile():
                    raise RuntimeError("Shared tooling archive member differs: " + row["path"])
                stream = archive.extractfile(matches[0])
                if stream is None:
                    raise RuntimeError("Shared tooling archive member is unreadable")
                content = stream.read(row["size"] + 1)
                if len(content) != row["size"] or digest(content) != row["sha256"]:
                    raise RuntimeError("Shared tooling archive bytes differ: " + row["path"])
                target = directory / row["path"]
                physical(target.parent).mkdir(parents=True, exist_ok=True)
                if physical(target).exists():
                    if physical(target).read_bytes() != content:
                        raise RuntimeError("Existing shared tooling bytes differ")
                else:
                    with physical(target).open("xb") as out:
                        out.write(content)
    for row in lock["files"]:
        actual = record(directory / row["path"])
        if (actual["size"], actual["sha256"]) != (row["size"], row["sha256"]):
            raise RuntimeError("Shared tooling identity differs: " + row["path"])
    return directory


def git_bytes(source_sha, path):
    env = safe_environment()
    env["GIT_NO_LAZY_FETCH"] = "1"
    return subprocess.check_output(["git", "-C", str(ROOT), "show", source_sha + ":" + path], env=env)


def source_digest(rows):
    compact = [{key: row[key] for key in ("member", "size", "sha256")} for row in sorted(rows, key=lambda item: item["member"])]
    return digest(json.dumps(compact, sort_keys=True, separators=(",", ":")).encode())


def source_facts(source_sha):
    model = load(HERE / "source-inputs.json")
    expected = {row["member"]: row for row in model["sourceMembers"]}
    generated = model["generatedSource"]
    if (len(expected) != 61 or len(model["gitSources"]) != 60 or model["upstream"]["commit"] != "56a15ca7c47fd6b23d155ba73027d29d9693664d" or
            set(expected) != set(model["gitSources"]) | {generated["member"]}):
        raise RuntimeError("Wheel source origin inventory differs")
    for item in model["rawOriginals"]:
        actual = record(HERE / item["path"])
        if (actual["size"], actual["sha256"]) != (item["size"], item["sha256"]):
            raise RuntimeError("Official upstream input differs: " + item["path"])
    wrapper = {"gradle/wrapper/gradle-wrapper.jar": "7d3a4ac4de1c32b59bc6a4eb8ecb8e612ccd0cf1ae1e99f66902da64df296172",
               "gradle/wrapper/gradle-wrapper.properties": "93b4f2c5ddcfc9e6f630fc7e004b31b389695cd0228be6da42e196e5d4af7acb"}
    for path, expected_hash in wrapper.items():
        if record(HERE / path)["sha256"] != expected_hash:
            raise RuntimeError("Approved Gradle wrapper differs: " + path)
    plugin = load(HERE / "compiler-plugin-toolchain.json")["sourcePom"]
    actual = record(HERE / plugin["path"])
    if (actual["size"], actual["sha256"]) != (plugin["size"], plugin["sha256"]):
        raise RuntimeError("Compose compiler source POM differs")
    if digest(git_bytes(source_sha, "LICENSE")) != model["license"]["sha256"]:
        raise RuntimeError("Fork license blob differs")
    lyricist = load(HERE / "lyricist-input.json")
    if (lyricist["repository"] != "https://qsxs.github.io/cpf-maven/" or lyricist["group"] != "io.github.qsxs.cpf.lyricist" or
            lyricist["version"] != "1.8.0-cpf11-preview-fad3d9091c75-40827525373-1" or set(lyricist["modules"]) != {"lyricist", "lyricist-core"}):
        raise RuntimeError("Published Lyricist input identity differs")
    env = safe_environment()
    env["GIT_NO_LAZY_FETCH"] = "1"
    prefix = "datetime-wheel-picker/src/commonMain/kotlin/"
    tree = subprocess.check_output(["git", "-C", str(ROOT), "ls-tree", "-r", "-z", source_sha, "--", prefix], env=env)
    blobs = {}
    for entry in tree.split(b"\0"):
        if not entry:
            continue
        info, path = entry.decode("utf-8").split("\t", 1)
        mode, kind, oid = info.split(" ")
        if path.endswith(".kt"):
            member = "commonMain/" + path.removeprefix(prefix)
            if mode != "100644" or kind != "blob" or member in blobs:
                raise RuntimeError("Unexpected Wheel source tree entry: " + path)
            blobs[member] = (path, oid)
    if set(blobs) != set(model["gitSources"]) or any(blobs[m][0] != p for m, p in model["gitSources"].items()):
        raise RuntimeError("Wheel Git source inventory differs")
    rows, locale_count = [], 0
    with zipfile.ZipFile(physical(HERE / model["sources"]["path"])) as archive:
        names = archive.namelist()
        if len(names) != model["archiveEntryCount"] or len(set(names)) != len(names) or {name for name in names if name.endswith(".kt")} != set(expected):
            raise RuntimeError("Official Wheel sources JAR inventory differs")
        for member, item in sorted(expected.items()):
            original = archive.read(member)
            if (len(original), digest(original)) != (item["size"], item["sha256"]):
                raise RuntimeError("Official Wheel source bytes differ: " + member)
            locale_count += b"@LyricistStrings(" in original
            if member in blobs:
                path, oid = blobs[member]
                if git_bytes(source_sha, path) != original:
                    raise RuntimeError("Wheel Git blob differs from official JAR: " + member)
                rows.append({"member": member, "size": len(original), "sha256": digest(original), "origin": "UPSTREAM_GIT_BLOB", "gitPath": path, "gitBlob": oid})
            else:
                if member != generated["member"] or (len(original), digest(original)) != (generated["size"], generated["sha256"]):
                    raise RuntimeError("Official generated Strings.kt differs")
                rows.append({"member": member, "size": len(original), "sha256": digest(original), "origin": generated["origin"]})
    if locale_count != 33 or source_digest(rows) != model["sourceSetSha256"]:
        raise RuntimeError("Wheel complete source digest or locale inventory differs")
    return model, rows


def output_roots(args):
    root = logical_resolve(HERE)
    work, repository = logical_resolve(args.work), logical_resolve(args.repository)
    outputs = (work, repository)
    if work.is_relative_to(repository) or repository.is_relative_to(work):
        raise RuntimeError("Work and staging must be separate directories")
    if any(output.is_relative_to(logical_resolve(ROOT)) or logical_resolve(ROOT).is_relative_to(output) for output in outputs):
        raise RuntimeError("Work and staging must be outside the Fork checkout")
    if any(root.is_relative_to(output) for output in outputs):
        raise RuntimeError("Work and staging must not be or contain the recipe root")
    prepared = work / "prepared.json"
    if physical(prepared).is_file():
        previous = load(prepared)
        if (previous["work"], previous["repository"]) != (str(work), str(repository)):
            raise RuntimeError("Prepared output paths differ")
        for item in previous["recipe"]:
            path = logical_resolve(root / item["path"])
            if any(path.is_relative_to(output) for output in outputs):
                raise RuntimeError("Output path contains a recorded recipe file")
    return outputs


def recipe_inputs(outputs):
    root = logical_resolve(HERE)
    recipe = []
    for current, directories, files in os.walk(physical(root)):
        parent = logical_path(current)
        directories[:] = [name for name in directories
                          if name not in (".gradle", ".kotlin", "build", "__pycache__")
                          and not any(logical_resolve(parent / name).is_relative_to(output) for output in outputs)]
        for name in files:
            path = parent / name
            if physical(path).is_file() and not any(logical_resolve(path).is_relative_to(output) for output in outputs):
                data = physical(path).read_bytes()
                recipe.append({"path": path.relative_to(root).as_posix(), "size": len(data), "sha256": digest(data)})
    return sorted(recipe, key=lambda item: item["path"])


def identity(args, tools, outputs):
    config = load(HERE / "port-config.json")
    if not re.fullmatch(r"[0-9a-f]{40}", args.source_sha) or not re.fullmatch(r"[1-9][0-9]*", args.run_id) or not re.fullmatch(r"[1-9][0-9]*", args.run_attempt):
        raise RuntimeError("Immutable source SHA and positive run identifiers are required")
    actual = subprocess.run(["git", "-C", str(HERE.parent), "rev-parse", "HEAD"], env=safe_environment(), check=True, capture_output=True, text=True).stdout.strip()
    if actual != args.source_sha:
        raise RuntimeError("Wheel checkout differs from requested source SHA")
    if config["modules"][MODULE]["group"] != GROUP or config["kotlin"] != "2.4.20-0.1.0-08" or config["metadataPolicy"] != "STANDARD_LOCKED_KGP_METADATA_NO_FALLBACK":
        raise RuntimeError("Wheel port identity differs")
    model, rows = source_facts(args.source_sha)
    recipe = recipe_inputs(outputs)
    value = {"sourceCommit": args.source_sha, "sourceTagCommit": model["upstream"]["commit"], "sources": rows,
             "recipe": recipe, "toolingCommit": TOOL_REVISION, "toolingFiles": load(HERE / "tooling-lock.json")["files"],
             "group": GROUP, "version": "1.4.0-cpf11-preview-" + args.source_sha[:12] + "-" + args.run_id + "-" + args.run_attempt}
    # 指纹不含机器路径；源码使用稳定的成员名、大小和哈希。
    fingerprint_value = {**value, "sources": rows}
    value["fingerprint"] = digest(json.dumps(fingerprint_value, sort_keys=True, separators=(",", ":")).encode())
    value["toolingHome"] = str(tools)
    value["work"] = str(outputs[0])
    value["repository"] = str(outputs[1])
    return value


def verify_recipe_commit(source_sha, recipe):
    # 发布版本包含 source SHA，实际配方必须属于同一提交。
    env = safe_environment()
    env["GIT_NO_LAZY_FETCH"] = "1"
    tree = subprocess.check_output(["git", "-C", str(ROOT), "ls-tree", "-r", "-z", source_sha, "--", "ohos"], env=env)
    committed = {}
    for entry in tree.split(b"\0"):
        if not entry:
            continue
        info, path = entry.decode("utf-8").split("\t", 1)
        mode, kind, oid = info.split(" ")
        relative = path.removeprefix("ohos/")
        if any(part in (".gradle", ".kotlin", "build", "__pycache__") for part in PurePosixPath(relative).parts[:-1]):
            continue
        if kind != "blob" or mode not in ("100644", "100755"):
            raise RuntimeError("Unsupported committed recipe entry: " + path)
        committed[relative] = oid
    if set(committed) != {item["path"] for item in recipe}:
        raise RuntimeError("Recipe files differ from the requested committed source SHA")
    for item in recipe:
        data = physical(HERE / item["path"]).read_bytes()
        header = b"blob " + str(len(data)).encode("ascii") + b"\0"
        if hashlib.sha1(header + data).hexdigest() != committed[item["path"]]:
            raise RuntimeError("Recipe bytes differ from the requested committed source SHA: " + item["path"])


def check_native(home):
    if not home:
        raise RuntimeError("Explicit Kotlin/Native home is required")
    props = dict(line.split("=", 1) for line in physical(Path(home) / "konan/konan.properties").read_text(encoding="utf-8").splitlines() if "=" in line)
    if props.get("compilerVersion", "").strip() != "2.4.20-0.1.0-08":
        raise RuntimeError("Kotlin/Native compilerVersion differs")


def verify_stage(value, evidence):
    work = Path(value["work"])
    model = load(HERE / "source-inputs.json")
    row = load(work / "evidence" / (evidence + ".json"))
    expected_members = [item for item in model["sourceMembers"] if evidence != "metadata" or item["member"].startswith("commonMain/")]
    expected_digest = source_digest(expected_members)
    if (row["fingerprint"] != value["fingerprint"] or row["stage"] != evidence or row["sourceCount"] != len(expected_members) or
            row["sourceSetSha256"] != expected_digest or row["sourceManifest"] != record(work / "source-manifest.json")):
        raise RuntimeError("Wheel compile source identity differs: " + evidence)
    expected_sources = {}
    for source in expected_members:
        path = work / "sources" / Path(*PurePosixPath(source["member"]).parts)
        actual = record(path)
        if actual["size"] != source["size"] or actual["sha256"] != source["sha256"]:
            raise RuntimeError("Wheel original compile source bytes differ: " + source["member"])
        expected_sources[logical_resolve(path)] = (source["size"], source["sha256"])
    if {logical_resolve(source["path"]): (source["size"], source["sha256"]) for source in row["sources"]} != expected_sources:
        raise RuntimeError("Wheel recorded compiler sources differ from the complete official source set")
    compiler = {"metadata": ":compileCommonMainKotlinMetadata", "native-x64": ":compileKotlinOhosX64",
                "native-arm64": ":compileKotlinOhosArm64"}[evidence]
    expected_tasks = {compiler, ":allMetadataJar"} if evidence == "metadata" else {compiler}
    if {item["task"] for item in row["producers"]} != expected_tasks or row["explicitApi"] not in (None, "Disabled"):
        raise RuntimeError("Standard KGP producer or upstream disabled explicit API mode differs")
    if row["sourceDependencies"] != {"implementation": model["sourceDependencies"]["implementation"], "api": []}:
        raise RuntimeError("Upstream implementation dependency model differs")
    inputs = row.get("lyricistCompilerInputs", {})
    if set(inputs) != {"lyricist", "lyricist-core"}:
        raise RuntimeError("Published Lyricist compiler input evidence differs")
    for item in inputs.values():
        if not item.get("files") or any(record(file["path"]) != file for file in item["files"]):
            raise RuntimeError("Lyricist compiler input bytes changed")
    plugins = row["compilerPlugins"]
    plugin_tools = load(HERE / "compiler-plugin-toolchain.json")
    plugin_ids = ("org.jetbrains.kotlin:kotlin-compose-compiler-plugin-embeddable",)
    expected_plugins = {(plugin_tools["artifacts"][key]["size"], plugin_tools["artifacts"][key]["sha256"])
                        for key in plugin_ids}
    if (any(plugin_tools["pins"][key] != "2.4.20-0.1.0-08" for key in plugin_ids) or
            len(expected_plugins) != 1 or len(plugins) != 1 or
            {(item["size"], item["sha256"]) for item in plugins} != expected_plugins or
            any(record(item["path"]) != item for item in plugins)):
        raise RuntimeError("Wheel compiler plugin set or bytes differ")
    native_targets, has_metadata, has_ir = [], False, False
    for producer in row["producers"]:
        if not producer["outputs"] or not any(item["size"] > 0 for item in producer["outputs"]):
            raise RuntimeError("Wheel producer outputs are empty")
        for item in producer["outputs"]:
            if record(item["path"]) != item:
                raise RuntimeError("Wheel stage output bytes changed")
            path = physical(item["path"])
            props = None
            if zipfile.is_zipfile(path):
                with zipfile.ZipFile(path) as archive:
                    names = archive.namelist()
                    has_metadata |= any(("/linkdata/" in name or name.endswith(".kotlin_metadata")) and archive.getinfo(name).file_size > 0 for name in names)
                    has_ir |= any("/ir/" in name and archive.getinfo(name).file_size > 0 for name in names)
                    if "default/manifest" in names:
                        props = archive.read("default/manifest").decode()
            elif path.as_posix().endswith("/default/manifest"):
                props = path.read_text(encoding="utf-8")
            if "/linkdata/" in path.as_posix() and item["size"] > 0:
                has_metadata = True
            if "/ir/" in path.as_posix() and item["size"] > 0:
                has_ir = True
            if props is not None:
                properties = dict(line.split("=", 1) for line in props.splitlines() if "=" in line)
                native_targets.extend(properties.get("native_targets", "").split())
    if not has_metadata:
        raise RuntimeError("Wheel metadata payload is absent")
    if evidence != "metadata":
        expected = "ohos_x64" if evidence == "native-x64" else "ohos_arm64"
        if not native_targets or set(native_targets) != {expected} or not has_ir:
            raise RuntimeError("Wheel Native target or IR payload differs: " + evidence)
    return row


def api_symbols(model):
    return model["publicApi"]


def native_klib(outputs):
    manifests = [item for item in outputs if item["path"].replace("\\", "/").endswith("/default/manifest")]
    packed = [item for item in outputs if item["path"].endswith(".klib")]
    if len(manifests) == 1:
        return Path(manifests[0]["path"]).parent.parent
    elif len(packed) == 1:
        return Path(packed[0]["path"])
    raise RuntimeError("Native Klib root is not uniquely identified")


def signature_stage(value, evidence, attempt, native_home):
    """同一完整 Native 产物输出 metadata 与 IR 签名；不推断消费调用或运行结果。"""
    row = verify_stage(value, evidence)
    work = Path(value["work"])
    config = load(HERE / "port-config.json")
    outputs = [item for producer in row["producers"] for item in producer["outputs"]]
    klib = native_klib(outputs)
    launcher = logical_resolve(native_home) / ("bin/klib.bat" if os.name == "nt" else "bin/klib")
    env = safe_environment(work)
    physical(work / "invocations").mkdir(parents=True, exist_ok=True)
    dumps = {}
    for channel, command, infix in (("metadata", "dump-metadata-signatures", "-signatures-"),
                                    ("ir", "dump-ir-signatures", "-ir-signatures-")):
        argv = [str(launcher), command, str(klib)]
        receipt = work / "invocations" / (evidence + infix + str(attempt) + ".json")
        if physical(receipt).exists():
            raise RuntimeError("Existing signature invocation; select a new Attempt")
        invocation = {"fingerprint": value["fingerprint"], "sourceSetSha256": row["sourceSetSha256"],
                      "argv": argv, "cwd": str(ROOT), "launcher": record(launcher),
                      "inputs": outputs, "started": time.time(), "exitCode": None}
        with physical(receipt).open("xb") as stream:
            stream.write((json.dumps(invocation, indent=2) + "\n").encode())
        with physical(receipt.with_suffix(".stdout.log")).open("xb") as out, physical(receipt.with_suffix(".stderr.log")).open("xb") as err:
            result = subprocess.run(argv, cwd=ROOT, env=env, stdout=out, stderr=err)
        output = record(receipt.with_suffix(".stdout.log"))
        invocation.update(exitCode=result.returncode, finished=time.time(), output=output)
        physical(receipt).write_bytes((json.dumps(invocation, indent=2) + "\n").encode())
        if result.returncode:
            raise RuntimeError("Native signature dump failed; see " + str(receipt))
        dumps[channel] = {"invocation": record(receipt), "output": output}
    write(work / "evidence" / (evidence + "-api.json"), {
        "fingerprint": value["fingerprint"], "stage": evidence, "sourceSetSha256": row["sourceSetSha256"],
        "inputs": outputs, **dumps["metadata"], "irInvocation": dumps["ir"]["invocation"], "irOutput": dumps["ir"]["output"],
        "symbols": api_symbols(load(HERE / "source-inputs.json")), "runtime": "NOT_RUN"}, exclusive=True)
    return verify_api(value, evidence, native_home)


def verify_signature_texts(model, metadata, ir, dump=""):
    declared = re.search(r"(?ms)^// Declared signatures: \d+\r?\n(.*?)^// Imported signatures: \d+\r?$", ir)
    if not metadata.strip() or not declared:
        raise RuntimeError("Metadata or Declared IR signatures are absent")
    declared_text = declared.group(1)
    matches, used = [], set()
    for item in model["publicApi"]["declarations"]:
        channel = item["evidenceChannel"]
        if channel == "metadata-dump":
            found = re.findall(item["metadataDumpPattern"], dump)
            if not found:
                raise RuntimeError("Public typealias missing from metadata dump: " + item["owner"])
            matches.append({"owner": item["owner"], "channel": channel, "matches": found})
            continue
        found = re.findall(item["metadataPattern"] + r"[^\r\n]*", metadata, re.M)
        # 同名重载按完整参数签名区分，同一签名不得抵扣另一条源码声明。
        found = [signature for signature in found if signature not in used]
        if len(found) != 1:
            raise RuntimeError("Public declaration metadata signature is not unique: " + item["owner"])
        used.add(found[0])
        matches.append({"owner": item["owner"], "sourceLine": item["sourceLine"], "channel": "metadata", "signature": found[0]})
    for item in model["publicApi"]["languageGeneratedMembers"]:
        found = re.findall(item["signaturePattern"] + r"[^\r\n]*", metadata, re.M)
        channel = "metadata"
        if not found:
            found = re.findall(item["signaturePattern"] + r"[^\r\n]*", declared_text, re.M)
            channel = "declaredIr"
        if len(found) != 1:
            raise RuntimeError("Language-generated member missing or ambiguous: " + item["owner"])
        matches.append({"owner": item["owner"], "channel": channel, "signature": found[0]})
    return matches


def verify_api(value, evidence, native_home):
    row = verify_stage(value, evidence)
    api = load(Path(value["work"]) / "evidence" / (evidence + "-api.json"))
    outputs = [item for producer in row["producers"] for item in producer["outputs"]]
    symbols = api_symbols(load(HERE / "source-inputs.json"))
    if (api["fingerprint"] != value["fingerprint"] or api["inputs"] != outputs or api["symbols"] != symbols or
            api["sourceSetSha256"] != row["sourceSetSha256"]):
        raise RuntimeError("Native API signatures do not belong to the exact complete compiler output")
    config = load(HERE / "port-config.json")
    launcher = logical_resolve(native_home) / ("bin/klib.bat" if os.name == "nt" else "bin/klib")
    for invocation_key, output_key, command in (("invocation", "output", "dump-metadata-signatures"),
                                               ("irInvocation", "irOutput", "dump-ir-signatures")):
        receipt, output = api[invocation_key], api[output_key]
        if record(receipt["path"]) != receipt or record(output["path"]) != output:
            raise RuntimeError("Native signature receipt or output bytes differ")
        invocation = load(receipt["path"])
        if (invocation["exitCode"] != 0 or invocation["inputs"] != outputs or invocation["fingerprint"] != value["fingerprint"] or
                invocation["sourceSetSha256"] != row["sourceSetSha256"] or invocation["output"] != output or
                invocation["argv"] != [str(launcher), command, str(native_klib(outputs))] or
                invocation["launcher"] != record(launcher) or
                logical_resolve(output["path"]) != logical_resolve(Path(receipt["path"]).with_suffix(".stdout.log"))):
            raise RuntimeError("Native signature command did not succeed for the same complete compiler output")
    verify_signature_texts(load(HERE / "source-inputs.json"), physical(api["output"]["path"]).read_text(encoding="utf-8-sig"),
                           physical(api["irOutput"]["path"]).read_text(encoding="utf-8-sig"))
    return api


def verify_publication(value, native_home):
    config = load(HERE / "port-config.json")
    model = load(HERE / "source-inputs.json")
    pins = load(HERE / "dependency-pins.json")
    stages = [verify_stage(value, evidence) for _, evidence, _ in COMPILES]
    group, version = config["modules"]["datetime-wheel-picker"]["group"], value["version"]
    repo = logical_path(value["repository"]) / group.replace(".", "/")
    root_file = repo / "datetime-wheel-picker" / version / ("datetime-wheel-picker-" + version + ".module")
    root, rows, scopes = load(root_file), [], []
    ns = {"m": "http://maven.apache.org/POM/4.0.0"}
    expected_roots = {"org.jetbrains.kotlin:kotlin-stdlib", *model["sourceDependencies"]["implementation"]}
    for suffix, target in (("", None), ("-ohosx64", "ohos_x64"), ("-ohosarm64", "ohos_arm64")):
        name = "datetime-wheel-picker" + suffix
        directory = repo / name / version
        gmm_file = directory / (name + "-" + version + ".module")
        gmm = load(gmm_file)
        if any(gmm["component"].get(k) != v for k, v in {"group": group, "module": "datetime-wheel-picker", "version": version}.items()):
            raise RuntimeError("Wheel publication owner differs")
        if target:
            if logical_resolve(directory / gmm["component"]["url"]) != logical_resolve(root_file):
                raise RuntimeError("Wheel Native leaf owner URL differs")
            links = [variant["available-at"] for variant in root["variants"] if
                     variant.get("attributes", {}).get("org.jetbrains.kotlin.native.target") == target and "available-at" in variant]
            if not links or any(link["group"] != group or link["module"] != name or link["version"] != version or
                                logical_resolve(root_file.parent / link["url"]) != logical_resolve(gmm_file) for link in links):
                raise RuntimeError("Wheel root Native available-at differs")
        api = []
        for variant in gmm["variants"]:
            attrs = variant.get("attributes", {})
            if ((not target and attrs.get("org.gradle.usage") == "kotlin-metadata" and attrs.get("org.jetbrains.kotlin.platform.type") == "common") or
                    (target and attrs.get("org.gradle.usage") == "kotlin-api" and attrs.get("org.jetbrains.kotlin.native.target") == target)):
                api.append(variant)
            for dep in variant.get("dependencies", []):
                if pins.get(dep["group"] + ":" + dep["module"]) != dep.get("version", {}).get("requires"):
                    raise RuntimeError("Wheel GMM dependency escaped frozen Native/root pins")
            for artifact in variant.get("files", []):
                path = logical_resolve(directory / artifact["url"])
                if not path.is_relative_to(logical_resolve(directory)):
                    raise RuntimeError("Wheel artifact URL escaped its publication")
                item = record(path)
                if item["sha256"] != artifact["sha256"] or item["size"] != artifact["size"] or item["size"] == 0:
                    raise RuntimeError("Wheel published artifact bytes differ")
        if not api or not all(variant.get("files") for variant in api):
            raise RuntimeError("Wheel common metadata or Native API artifact is absent")
        if any({dep["group"] + ":" + dep["module"] for dep in variant.get("dependencies", [])} != expected_roots for variant in api):
            raise RuntimeError("Wheel GMM API dependency set differs from the upstream model")
        pom = ET.fromstring(physical(directory / (name + "-" + version + ".pom")).read_bytes())
        if any(pom.findtext("m:" + key, namespaces=ns) != expected for key, expected in
               (("groupId", group), ("artifactId", name), ("version", version))):
            raise RuntimeError("Wheel POM coordinates differ")
        licenses = [(item.findtext("m:name", namespaces=ns), item.findtext("m:url", namespaces=ns))
                    for item in pom.findall("m:licenses/m:license", ns)]
        if licenses != [(model["privateLicense"]["name"], model["privateLicense"]["url"])]:
            raise RuntimeError("Private POM does not follow the approved Apache-2.0 source LICENSE")
        developers = [(item.findtext("m:id", namespaces=ns), item.findtext("m:name", namespaces=ns), item.findtext("m:url", namespaces=ns)) for item in pom.findall("m:developers/m:developer", ns)]
        if developers != [("darkokoa", "darkokoa", "https://github.com/darkokoa/")]:
            raise RuntimeError("Wheel POM author differs")
        dependencies = pom.findall("m:dependencies/m:dependency", ns)
        expected_pom = {ga + (suffix if target and not ga.startswith("org.jetbrains.kotlin:") else "") for ga in expected_roots}
        actual_pom = set()
        for dep in dependencies:
            ga = dep.findtext("m:groupId", namespaces=ns) + ":" + dep.findtext("m:artifactId", namespaces=ns)
            scope = dep.findtext("m:scope", namespaces=ns)
            actual_pom.add(ga)
            if pins.get(ga) != dep.findtext("m:version", namespaces=ns) or scope != ("compile" if target else "runtime"):
                raise RuntimeError("Wheel POM dependency version or publication-specific scope differs")
        if actual_pom != expected_pom:
            raise RuntimeError("Wheel POM dependency closure differs")
        scopes.append({"publication": name, "pomScope": "compile" if target else "runtime",
                       "gmmUsage": "kotlin-api" if target else "kotlin-metadata", "dependencies": sorted(actual_pom)})
        if record(directory / (name + "-" + version + "-license.txt"))["sha256"] != model["license"]["sha256"]:
            raise RuntimeError("Wheel publication license differs")
        # 标准 sources artifact 必须包含完整 60 份 Git 原源和 1 份官方生成源，不能只附一份未被编译的上游归档。
        with zipfile.ZipFile(physical(directory / (name + "-" + version + "-sources.jar"))) as archive:
            actual_sources = [{"member": member.filename, "size": member.file_size,
                               "sha256": hashlib.sha256(archive.read(member)).hexdigest()}
                              for member in archive.infolist() if member.filename.endswith(".kt")]
            if sorted(actual_sources, key=lambda item: item["member"]) != model["sourceMembers"]:
                raise RuntimeError("Published sources artifact is not the complete original library")
            license_member = "META-INF/licenses/datetime-wheel-picker/LICENSE"
            if hashlib.sha256(archive.read(license_member)).hexdigest() != model["license"]["sha256"]:
                raise RuntimeError("Published sources license differs")
        if target:
            stage = next(row for row in stages if row["stage"] == ("native-x64" if target == "ohos_x64" else "native-arm64"))
            with zipfile.ZipFile(physical(directory / (name + "-" + version + ".klib"))) as archive:
                for producer in stage["producers"]:
                    for item in producer["outputs"]:
                        path = Path(item["path"])
                        normalized = path.as_posix()
                        if "/default/" in normalized:
                            member = "default/" + normalized.split("/default/", 1)[1]
                            if hashlib.sha256(archive.read(member)).hexdigest() != item["sha256"]:
                                raise RuntimeError("Published Klib member differs from the real Native compiler output")
                        elif path.suffix == ".klib" and record(path)["sha256"] != record(directory / (name + "-" + version + ".klib"))["sha256"]:
                            raise RuntimeError("Published Klib differs from the real Native compiler output")
        else:
            metadata = next(row for row in stages if row["stage"] == "metadata")
            jar = next(item for producer in metadata["producers"] if producer["task"] == ":allMetadataJar" for item in producer["outputs"] if item["path"].endswith(".jar"))
            if record(directory / (name + "-" + version + ".jar"))["sha256"] != jar["sha256"]:
                raise RuntimeError("Published common metadata differs from the real standard KGP artifact")
        if not suffix:
            for original in model["originalClassifiers"]:
                key = original["key"]
                classifier = original["classifier"] + "." + original["extension"]
                if record(directory / (name + "-" + version + "-" + classifier))["sha256"] != model[key]["sha256"]:
                    raise RuntimeError("Wheel publication official original differs: " + key)
        if physical(directory / (name + "-" + version + "-provenance.json")).read_bytes() != physical(Path(value["work"]) / "provenance.json").read_bytes():
            raise RuntimeError("Published provenance differs")
        for entry in sorted(physical(directory).iterdir()):
            path = logical_path(entry)
            if not physical(path).is_file() or path.suffix in (".md5", ".sha1", ".sha256", ".sha512"):
                continue
            for algorithm in ("md5", "sha1", "sha256", "sha512"):
                checksum = path.with_name(path.name + "." + algorithm)
                if not physical(checksum).is_file() or physical(checksum).read_text().strip().lower() != hashlib.new(algorithm, physical(path).read_bytes()).hexdigest():
                    raise RuntimeError("Publication checksum differs: " + str(checksum))
            rows.append(record(path))
    api_receipts = [verify_api(value, evidence, native_home) for evidence in ("native-x64", "native-arm64")]
    result = {"status": "VERIFIED_STAGING_NOT_PUBLISHED", **value, "files": rows,
              "compilations": [{key: row[key] for key in ("stage", "sourceCount", "sourceSetSha256", "sourceManifest")} for row in stages],
              "dependencyScopes": scopes, "publicApi": model["publicApi"], "nativeSignatures": api_receipts,
              "runtime": "NOT_RUN", "armRuntime": "NOT_RUN"}
    destination = Path(value["work"]) / "publication.json"
    if physical(destination).exists():
        if load(destination) != result:
            raise RuntimeError("Existing publication evidence differs")
    else:
        write(destination, result, exclusive=True)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("Plan", "Prepare", *TASKS, "Verify"))
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--tooling", type=Path)
    parser.add_argument("--native-home", type=Path)
    args = parser.parse_args()
    outputs = output_roots(args)
    work, repository = outputs
    prepared = work / "prepared.json"
    tooling = logical_resolve(args.tooling) if args.tooling else logical_resolve(work / "tooling" / TOOL_REVISION)
    value = identity(args, tooling, outputs)
    model = load(HERE / "source-inputs.json")
    provenance = {"fingerprint": value["fingerprint"], "sourceCommit": value["sourceCommit"],
                  "upstream": model["upstream"], "group": GROUP, "module": MODULE, "version": value["version"],
                  "toolingCommit": TOOL_REVISION, "sourceMembers": value["sources"], "publicApi": model["publicApi"],
                  "generatedSource": model["generatedSource"], "lyricistInput": load(HERE / "lyricist-input.json"),
                  "upstreamFiles": model["rawOriginals"], "licenseDecision": model["privateLicense"]}
    if args.stage == "Plan":
        print(json.dumps({"stage": "Plan", **value, "recipeCommitCheck": "NOT_RUN", "build": "NOT_RUN", "runtime": "NOT_RUN"}))
        return
    verify_recipe_commit(args.source_sha, value["recipe"])
    fresh = args.stage == "Prepare" and not physical(prepared).exists()
    if fresh:
        for output in outputs:
            if physical(output).exists() and (not physical(output).is_dir() or any(physical(output).iterdir())):
                raise RuntimeError("Fresh work and staging must be empty: " + str(output))
        physical(work).mkdir(parents=True, exist_ok=True)
        checked_tools(work, args.tooling)
        members = []
        for source in value["sources"]:
            if source["origin"] == "UPSTREAM_GIT_BLOB":
                data = git_bytes(value["sourceCommit"], source["gitPath"])
            else:
                with zipfile.ZipFile(physical(HERE / model["sources"]["path"])) as archive:
                    data = archive.read(source["member"])
            if len(data) != source["size"] or digest(data) != source["sha256"]:
                raise RuntimeError("Prepared source origin bytes changed: " + source["member"])
            target = work / "sources" / Path(*PurePosixPath(source["member"]).parts)
            physical(target.parent).mkdir(parents=True, exist_ok=True)
            with physical(target).open("xb") as stream:
                stream.write(data)
            members.append({"member": source["member"], "origin": source["origin"], **record(target)})
        write(work / "source-manifest.json", {"fingerprint": value["fingerprint"], "members": members,
              "sourceCount": 61, "gitSourceCount": 60, "jarGeneratedSourceCount": 1, "localeCount": 33,
              "sourceSetSha256": model["sourceSetSha256"], "publicApi": model["publicApi"]}, exclusive=True)
        write(work / "provenance.json", provenance, exclusive=True)
        write(prepared, value, exclusive=True)
    if not physical(prepared).is_file() or load(prepared) != value or load(work / "provenance.json") != provenance:
        raise RuntimeError("Work identity or provenance differs; preserve this work and use a fresh directory")
    tools = checked_tools(work, args.tooling)
    manifest = load(work / "source-manifest.json")
    if manifest["fingerprint"] != value["fingerprint"] or manifest["sourceSetSha256"] != model["sourceSetSha256"]:
        raise RuntimeError("Prepared source manifest differs")
    for source in value["sources"]:
        path = work / "sources" / Path(*PurePosixPath(source["member"]).parts)
        actual = record(path)
        if (actual["size"], actual["sha256"]) != (source["size"], source["sha256"]):
            raise RuntimeError("Prepared source bytes differ: " + source["member"])
    if args.stage == "Prepare":
        print(json.dumps({"work": str(work), "toolingHome": str(tools), "fingerprint": value["fingerprint"], "version": value["version"]}))
        return
    check_native(args.native_home)
    if args.stage == "Verify":
        print(json.dumps(verify_publication(value, args.native_home)))
        return
    if args.stage != "Tasks":
        discovery = load(work / "task-discovery.json")
        if discovery["fingerprint"] != value["fingerprint"] or record(discovery["stdout"]["path"]) != discovery["stdout"]:
            raise RuntimeError("Task discovery differs")
    if args.stage not in ("Tasks", "Resolve"):
        if load(work / "evidence/resolve.json")["fingerprint"] != value["fingerprint"]:
            raise RuntimeError("Resolve belongs to another recipe")
        for label, stage, target in COMPILES:
            if label == args.stage:
                break
            verify_stage(value, stage)
    if args.stage == "Publish":
        for stage in ("native-x64", "native-arm64"):
            verify_api(value, stage, args.native_home)
        if physical(repository).exists() and (not physical(repository).is_dir() or any(physical(repository).iterdir())):
            raise RuntimeError("Staging is not empty; refusing to overwrite existing files")
    evidence_name = {"Tasks": "task-discovery.json", "Resolve": "evidence/resolve.json",
                     "Metadata": "evidence/metadata.json", "NativeX64": "evidence/native-x64.json",
                     "NativeArm64": "evidence/native-arm64.json"}.get(args.stage)
    if evidence_name and physical(work / evidence_name).exists():
        raise RuntimeError("Stage evidence already exists; preserve it and use a fresh work directory")
    api_evidence = {"NativeX64": "evidence/native-x64-api.json", "NativeArm64": "evidence/native-arm64-api.json"}.get(args.stage)
    if api_evidence and physical(work / api_evidence).exists():
        raise RuntimeError("Native API evidence already exists; preserve it and use a fresh work directory")
    wrapper = ["cmd.exe", "/d", "/c", str(HERE / "gradlew.bat")] if os.name == "nt" else ["sh", str(HERE / "gradlew")]
    argv = [*wrapper, "-p", str(HERE), "--project-cache-dir", str(work / "project-cache"),
            "-PportWork=" + str(work), "-PportTooling=" + str(tools), "-PportFingerprint=" + value["fingerprint"], "-PportVersion=" + value["version"],
            "-PportRepository=" + str(repository), "-Pkotlin.project.persistent.dir=" + str(work / "kotlin-cache"),
            "-Pkotlin.native.home=" + str(logical_resolve(args.native_home)), "--console=plain", "--stacktrace", *TASKS[args.stage]]
    logs = work / "logs"
    physical(logs).mkdir(parents=True, exist_ok=True)
    stdout, stderr = logs / (args.stage + ".stdout.log"), logs / (args.stage + ".stderr.log")
    with physical(stdout).open("xb") as out, physical(stderr).open("xb") as err:
        run = subprocess.run(argv, env=safe_environment(work), stdout=out, stderr=err)
    if run.returncode:
        print(physical(stderr).read_text(encoding="utf-8", errors="replace")[-16384:], file=sys.stderr)
        raise RuntimeError("Gradle " + args.stage + " failed with exit " + str(run.returncode))
    if args.stage == "Tasks":
        names = {name.lstrip(":") for name in re.findall(r"^\s*([\w:-]+)(?:\s+-|\s*$)", physical(stdout).read_text(encoding="utf-8", errors="replace"), re.MULTILINE)}
        if not REQUIRED.issubset(names):
            raise RuntimeError("Required tasks absent from actual discovery: " + ", ".join(sorted(REQUIRED - names)))
        write(work / "task-discovery.json", {"fingerprint": value["fingerprint"], "required": sorted(REQUIRED), "stdout": record(stdout)}, exclusive=True)
    for label, stage, target in COMPILES:
        if label == args.stage:
            verify_stage(value, stage)
            if target is not None:
                signature_stage(value, stage, args.run_attempt, args.native_home)
    print(json.dumps({"stage": args.stage, "exitCode": run.returncode, "fingerprint": value["fingerprint"], "stdout": record(stdout)}))


if __name__ == "__main__":
    main()
