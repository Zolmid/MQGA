#!/usr/bin/env python3
"""Pin module inputs, fetch those exact bytes, and verify LSPatch embeds them.

Run from the repository root. GitHub credentials are needed only by resolve and
fetch; finalize and verify-patched deliberately operate on local files only.
"""

import argparse
import copy
import fnmatch
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen
import zipfile


MODULES = {
    "QAuxiliary": {
        "repo": "cinit/QAuxiliary", "package": "io.github.qauxv",
        "workflow": "push_ci.yml", "artifact": "QAuxiliary-release-{sha}-arm64",
        "apk_pattern": "QAuxv-v*-arm64.apk",
    },
    "QFun": {
        "repo": "Xposed-Modules-Repo/me.yxp.qfun", "package": "me.yxp.qfun",
        "asset_pattern": "QFun_*.apk",
    },
    "QWish": {
        "repo": "Xposed-Modules-Repo/top.qwish.xposed", "package": "top.qwish.xposed",
        "asset_pattern": "QWish_*.apk",
    },
    "XAutoDaily": {
        "repo": "LuckyPray/XAutoDaily", "package": "me.teble.xposed.autodaily",
        "asset_pattern": "XAutoDaily_*.apk",
    },
    "TSBattery": {
        "repo": "fankes/TSBattery", "package": "com.fankes.tsbattery",
        "workflow": "commit_ci.yml", "artifact": "TSBattery-legacy-release-{sha}",
        "apk_pattern": "TSBattery-v*-legacy-release.apk",
    },
    "ImPad": {
        "repo": "NKU100/ImPad", "package": "io.github.nku100.impad",
        "workflow": "build.yml", "artifact": "impad-module-apk",
        "apk_pattern": "impad-*-release.apk",
    },
}
UPAGE_URL = "https://upage.html5.qq.com/wechat-apkinfo"


class BuildError(RuntimeError):
    pass


def redact(text):
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "CI_ARTIFACT_TOKEN"):
        value = os.environ.get(key)
        if value:
            text = text.replace(value, "[REDACTED]")
    return text


def gh_error(stderr, label):
    detail = redact(stderr.strip())
    raise BuildError(
        f"{label} failed: {detail}\n"
        "For upstream Actions read/download permission failures, set "
        "CI_ARTIFACT_TOKEN to a token authorized to read the upstream artifacts. "
        "No release fallback is used for CI modules."
    )


def github_api(endpoint):
    result = subprocess.run(
        ["gh", "api", "--method", "GET", "-H", "Accept: application/vnd.github+json", endpoint],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        gh_error(result.stderr, f"GitHub API {endpoint}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BuildError(f"Invalid JSON from GitHub API {endpoint}") from exc


def enabled(name):
    value = os.environ.get(name, "false").strip().lower()
    if value not in {"true", "false", ""}:
        raise BuildError(f"{name} must be true or false")
    return value == "true"


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildError(f"Cannot read valid JSON from {path}: {exc}") from exc


def output(**values):
    lines = []
    for key, value in values.items():
        value = str(value).lower() if isinstance(value, bool) else str(value)
        if "\n" in value or "\r" in value:
            raise BuildError(f"Unexpected newline in Actions output {key}")
        lines.append(f"{key}={value}\n")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as target:
            target.writelines(lines)
    else:
        print("".join(lines), end="")


def eligible_run(run, repo, branch, workflow):
    return (
        run.get("status") == "completed"
        and run.get("conclusion") == "success"
        and run.get("head_branch") == branch
        and run.get("event") in {"push", "workflow_dispatch"}
        and (run.get("head_repository") or {}).get("full_name", "").lower() == repo.lower()
        and run.get("path", "").split("@", 1)[0] == f".github/workflows/{workflow}"
        and re.fullmatch(r"[0-9a-f]{40}", run.get("head_sha", "")) is not None
    )


def choose_artifact(run, artifacts, pattern, now=None):
    """Match the intended flavor and commit, never a PR/symbol/modern build."""
    names = {pattern.format(sha=run["head_sha"])}
    # QA's workflow uses event.head_commit.id, which is empty for manual runs.
    if run["event"] == "workflow_dispatch":
        names.add(pattern.format(sha=""))
    now = now or datetime.now(timezone.utc)
    matches = []
    for artifact in artifacts:
        if artifact.get("expired", True) or artifact.get("name") not in names:
            continue
        if artifact.get("expires_at"):
            expiry = datetime.fromisoformat(artifact["expires_at"].replace("Z", "+00:00"))
            if expiry <= now:
                continue
        origin = artifact.get("workflow_run") or {}
        if origin.get("id", run["id"]) != run["id"]:
            continue
        if origin.get("head_sha", run["head_sha"]) != run["head_sha"]:
            continue
        if artifact.get("size_in_bytes", 0) <= 0:
            continue
        matches.append(artifact)
    if len(matches) > 1:
        raise BuildError(f"Run {run['id']} has multiple matching unexpired artifacts: {sorted(names)}")
    return matches[0] if matches else None


def resolve_ci(name, spec, api=github_api):
    repo, workflow = spec["repo"], spec["workflow"]
    branch = api(f"repos/{repo}")["default_branch"]
    # GitHub's filtered runs endpoint exposes at most 1,000 runs.
    for page in range(1, 11):
        query = urlencode({"branch": branch, "status": "success", "per_page": 100, "page": page})
        runs = api(f"repos/{repo}/actions/workflows/{quote(workflow, safe='')}/runs?{query}")["workflow_runs"]
        for run in sorted(runs, key=lambda r: (r["created_at"], r["id"]), reverse=True):
            if not eligible_run(run, repo, branch, workflow):
                continue
            artifacts = []
            artifact_page = 1
            while True:
                response = api(
                    f"repos/{repo}/actions/runs/{run['id']}/artifacts?per_page=100&page={artifact_page}"
                )
                artifacts.extend(response["artifacts"])
                if len(response["artifacts"]) < 100:
                    break
                artifact_page += 1
            artifact = choose_artifact(run, artifacts, spec["artifact"])
            if artifact is None:
                continue
            return {
                "kind": "upstream-ci", "repo": repo, "branch": branch,
                "workflow": workflow, "sha": run["head_sha"], "event": run["event"],
                "run_id": run["id"], "run_attempt": run.get("run_attempt", 1),
                "run_url": run["html_url"], "artifact_id": artifact["id"],
                "artifact_name": artifact["name"], "digest": artifact.get("digest"),
                "artifact_size": artifact["size_in_bytes"], "apk_pattern": spec["apk_pattern"],
                "package": spec["package"],
            }
        if len(runs) < 100:
            break
    raise BuildError(
        f"{name}: no unexpired {spec['artifact']} artifact in successful default-branch "
        f"push/manual runs of {repo}/{workflow}; no release fallback."
    )


def resolve_release(repo, pattern, api=github_api):
    release = api(f"repos/{repo}/releases/latest")
    if release.get("draft") or release.get("prerelease"):
        raise BuildError(f"{repo}: latest release unexpectedly is draft/prerelease")
    assets = [a for a in release.get("assets", []) if fnmatch.fnmatchcase(a["name"], pattern)]
    if len(assets) != 1 or assets[0].get("size", 0) <= 0:
        raise BuildError(f"{repo}: expected one nonempty {pattern} release asset, found {len(assets)}")
    asset = assets[0]
    return {
        "kind": "official-release", "repo": repo, "tag": release["tag_name"],
        "release_id": release["id"], "release_url": release["html_url"],
        "asset_id": asset["id"], "asset_name": asset["name"],
        "url": asset["browser_download_url"], "digest": asset.get("digest"),
        "asset_size": asset["size"], "asset_updated_at": asset.get("updated_at"),
    }


def checked_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc or parsed.username or parsed.password:
        raise BuildError("Download URL must be an HTTP(S) URL without embedded credentials")
    return url


def checked_version(value):
    if not value or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,99}", value) is None:
        raise BuildError("QQ version must be a nonempty filename-safe version string")
    return value


def resolve_qq():
    override_version = os.environ.get("INPUT_QQ_VERSION", "").strip()
    override_url = os.environ.get("INPUT_QQ_URL", "").strip()
    if bool(override_version) != bool(override_url):
        raise BuildError("INPUT_QQ_VERSION and INPUT_QQ_URL must be provided together")
    selections = []
    if override_version or enabled("QQ_CUSTOM_ENABLE"):
        version = checked_version(override_version or os.environ.get("QQ_CUSTOM_VERSION", "").strip())
        url = checked_url(override_url or os.environ.get("QQ_CUSTOM_URL", "").strip())
        selections.append({"kind": "custom", "version": version, "url": url,
                           "path": f"inputs/QQ-{version}.apk"})
    if not override_version and enabled("QQ_LATEST_ENABLE"):
        request = Request(
            UPAGE_URL, data=json.dumps({"packagename": "com.tencent.mobileqq"}).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "MQGA/CI"}, method="POST",
        )
        try:
            with urlopen(request, timeout=60) as response:
                data = json.load(response)["app_detail_records"]["com.tencent.mobileqq"]["apk_all_data"]
            version, url = checked_version(data["version_name"]), checked_url(data["url"])
        except (KeyError, TypeError, ValueError, HTTPError, URLError) as exc:
            raise BuildError(f"QQ official version lookup failed ({type(exc).__name__})") from exc
        selections.append({"kind": "latest", "version": version, "url": url,
                           "path": f"inputs/QQ-{version}-latest.apk"})
    if not selections:
        raise BuildError("Enable at least one QQ input (custom/latest), or supply both QQ input overrides")
    return selections


def version_identity(sources, file_hashes):
    """Ignore expiring latest-QQ URLs, but retain source IDs, config, and builder."""
    identity = copy.deepcopy(sources)
    for item in identity["qq"]:
        if item["kind"] == "latest":
            item.pop("url", None)
    identity["builder_files"] = file_hashes
    return identity


def builder_file_hashes(root="."):
    """Fingerprint both variant recipes, including additions/removals of Java files."""
    root = Path(root)
    required = [
        Path("config.yml"), Path(".github/scripts/ci_modules.py"),
        Path(".github/scripts/ci_presets.py"), Path("presets/default.json"),
    ]
    for relative in required:
        if not (root / relative).is_file():
            raise BuildError(f"Required build recipe file is missing: {relative}")
    files = {root / relative for relative in required}
    files.update((root / ".github/workflows").glob("*.y*ml"))
    files.update((root / "preset-runtime").rglob("*.java"))
    return {path.relative_to(root).as_posix(): sha256(path)
            for path in sorted(files) if path.is_file()}


def previous_versions():
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise BuildError("GITHUB_REPOSITORY is required to compare the previous release manifest")
    with tempfile.TemporaryDirectory(prefix="mqga-history-") as directory:
        destination = Path(directory) / "versions.json"
        result = subprocess.run(
            ["gh", "release", "download", "--repo", repo, "--pattern", "versions.json",
             "--output", str(destination)], capture_output=True, text=True, check=False,
        )
        if result.returncode:
            error = redact(result.stderr.strip())
            if re.search(r"HTTP 404|\b404 Not Found\b|no releases? found|release not found|no assets match", error, re.I):
                print("No previous versions.json release asset; this build establishes the manifest.")
                return {}
            gh_error(error, "Reading previous release versions.json")
        previous = read_json(destination)
        if not isinstance(previous, dict):
            raise BuildError("Previous versions.json must contain a JSON object")
        return previous


def resolve(args):
    selected = {name: enabled("MODULES_" + name.upper()) for name in MODULES}
    if not any(selected.values()):
        raise BuildError("At least one module must be enabled")
    resolved = {}
    for name, spec in MODULES.items():
        if not selected[name]:
            continue
        if "workflow" in spec:
            source = resolve_ci(name, spec)
        elif "asset_pattern" in spec:
            source = resolve_release(spec["repo"], spec["asset_pattern"])
            source["package"] = spec["package"]
        else:
            raise BuildError(f"{name}: no supported upstream CI or official release source")
        resolved[name] = source
        print(f"{name}: {source['kind']} {source.get('sha', source.get('tag'))}")
    lspatch_repo = os.environ.get("LSPATCH_REPO", "JingMatrix/LSPatch").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", lspatch_repo):
        raise BuildError("Invalid LSPATCH_REPO")
    jar_pattern = "lspatch-*-release.jar" if lspatch_repo.lower() == "jingmatrix/lspatch" else "*.jar"
    sources = {
        "schema": 1, "modules": resolved, "qq": resolve_qq(),
        "lspatch": resolve_release(lspatch_repo, jar_pattern),
        "config": {"modules": selected, "lspatch_repo": lspatch_repo},
    }
    versions = version_identity(sources, builder_file_hashes())
    # Read once even in force mode, so authorization/network errors are not hidden.
    previous = previous_versions()
    patch_needed = enabled("FORCE_ENABLE") or enabled("INPUT_FORCE") or previous != versions
    write_json(args.sources, sources)
    write_json(args.versions, versions)
    latest = next((item for item in sources["qq"] if item["kind"] == "latest"), sources["qq"][0])
    output(patch_needed=patch_needed, qq_version=latest["version"])


def verify_digest(path, digest, label):
    if not digest:
        print(f"{label}: upstream published no digest; downloaded SHA-256 is {sha256(path)}")
        return
    match = re.fullmatch(r"sha256:([0-9a-fA-F]{64})", digest)
    if not match or sha256(path) != match.group(1).lower():
        raise BuildError(f"{label}: GitHub SHA-256 digest mismatch or unsupported digest")


def download(url, destination, label, digest=None, size=None):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part")
    request = Request(checked_url(url), headers={"User-Agent": "MQGA/CI"})
    try:
        with urlopen(request, timeout=120) as response, temporary.open("wb") as target:
            shutil.copyfileobj(response, target, 1024 * 1024)
        if temporary.stat().st_size <= 0 or (size is not None and temporary.stat().st_size != size):
            raise BuildError(f"{label}: empty download or size differs from locked GitHub metadata")
        if digest is not None or size is not None:
            verify_digest(temporary, digest, label)
        temporary.replace(destination)
    except (HTTPError, URLError, OSError) as exc:
        raise BuildError(f"{label}: download failed ({type(exc).__name__}); no fallback") from exc
    finally:
        temporary.unlink(missing_ok=True)


def validate_apk(path_or_bytes, label="APK"):
    try:
        source = io.BytesIO(path_or_bytes) if isinstance(path_or_bytes, bytes) else path_or_bytes
        with zipfile.ZipFile(source) as archive:
            entries = archive.infolist()
            manifests = [e for e in entries if e.filename == "AndroidManifest.xml"]
            dex = [e for e in entries if re.fullmatch(r"classes(?:[0-9]+)?\.dex", e.filename)]
            if len(manifests) != 1 or not manifests[0].file_size or not dex or any(not e.file_size for e in dex):
                raise BuildError(f"{label}: APK must contain one nonempty AndroidManifest.xml and DEX")
            # Read the manifest and every DEX to detect truncation/CRC failures early.
            for entry in manifests + dex:
                archive.read(entry)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        if isinstance(exc, BuildError):
            raise
        raise BuildError(f"{label}: corrupt or unreadable APK") from exc


def extract_apk(archive_path, pattern, destination, source=None):
    try:
        with zipfile.ZipFile(archive_path) as archive:
            candidates = [e for e in archive.infolist() if not e.is_dir()
                          and fnmatch.fnmatchcase(PurePosixPath(e.filename).name, pattern)]
            if len(candidates) != 1:
                raise BuildError(f"Artifact must contain exactly one {pattern} APK; found {len(candidates)}")
            entry = candidates[0]
            path = PurePosixPath(entry.filename)
            if path.is_absolute() or ".." in path.parts or "\\" in entry.filename or not entry.file_size:
                raise BuildError("Artifact APK has an unsafe path or is empty")
            data = archive.read(entry)
            validate_apk(data, entry.filename)
            if source and source.get("repo", "").lower() == "nku100/impad":
                metadata = [e for e in archive.infolist() if PurePosixPath(e.filename).name == "build.json"]
                if len(metadata) != 1:
                    raise BuildError("ImPad artifact must contain exactly one build.json")
                build = json.loads(archive.read(metadata[0]))
                if build.get("buildType") != "release":
                    raise BuildError("ImPad build.json must describe a release build")
                if build.get("commit") != source["sha"]:
                    raise BuildError("ImPad build.json commit does not match the locked CI commit")
                if build.get("sha256", {}).get(path.name) != hashlib.sha256(data).hexdigest():
                    raise BuildError("ImPad APK SHA-256 does not match build.json")
            Path(destination).parent.mkdir(parents=True, exist_ok=True)
            Path(destination).write_bytes(data)
    except (OSError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        raise BuildError(f"Cannot read module artifact {Path(archive_path).name}") from exc


def fetch(args):
    sources = read_json(args.sources)
    Path("modules").mkdir(exist_ok=True)
    Path("inputs").mkdir(exist_ok=True)
    for name, source in sources["modules"].items():
        if name not in MODULES:
            raise BuildError(f"Unknown module in sources.json: {name}")
        destination = Path("modules") / f"{name}.apk"
        destination.unlink(missing_ok=True)
        if source["kind"] == "upstream-ci":
            with tempfile.TemporaryDirectory(prefix="mqga-artifact-") as directory:
                archive_path = Path(directory) / "artifact.zip"
                endpoint = f"repos/{source['repo']}/actions/artifacts/{source['artifact_id']}/zip"
                with archive_path.open("wb") as target:
                    result = subprocess.run(["gh", "api", endpoint], stdout=target,
                                            stderr=subprocess.PIPE, check=False)
                if result.returncode:
                    gh_error(result.stderr.decode(errors="replace"), f"Downloading {name} CI artifact")
                if not archive_path.stat().st_size:
                    raise BuildError(f"{name}: empty CI artifact download")
                verify_digest(archive_path, source.get("digest"), f"{name} artifact")
                extract_apk(archive_path, source["apk_pattern"], destination, source)
        elif source["kind"] == "official-release":
            download(source["url"], destination, name, source.get("digest"), source["asset_size"])
            validate_apk(destination, name)
        else:
            raise BuildError(f"Unsupported source kind for {name}: {source['kind']}")
        print(f"Fetched {name}: {sha256(destination)}")
    for qq in sources["qq"]:
        download(qq["url"], qq["path"], f"QQ {qq['version']} ({qq['kind']})")
        validate_apk(qq["path"], "QQ")
    patcher = sources["lspatch"]
    download(patcher["url"], "LSPatch.jar", "LSPatch", patcher.get("digest"), patcher["asset_size"])
    try:
        with zipfile.ZipFile("LSPatch.jar") as archive:
            if not archive.read("META-INF/MANIFEST.MF") or archive.testzip() is not None:
                raise BuildError("LSPatch.jar is empty or corrupt")
    except (KeyError, OSError, zipfile.BadZipFile) as exc:
        raise BuildError("LSPatch download is not a valid executable JAR") from exc


def apk_package(path):
    sdk = Path(os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or "/nonexistent")
    aapt2 = shutil.which("aapt2")
    if not aapt2:
        candidates = sorted(sdk.glob("build-tools/*/aapt2"), reverse=True)
        aapt2 = str(candidates[0]) if candidates else None
    if aapt2:
        result = subprocess.run([aapt2, "dump", "badging", str(path)], capture_output=True, text=True, check=False)
        match = re.search(r"^package: name='([^']+)'", result.stdout, re.M)
        if result.returncode == 0 and match:
            return match.group(1)
    analyzer = shutil.which("apkanalyzer")
    if not analyzer:
        candidate = sdk / "cmdline-tools/latest/bin/apkanalyzer"
        analyzer = str(candidate) if candidate.is_file() else None
    if analyzer:
        result = subprocess.run([analyzer, "manifest", "application-id", str(path)],
                                capture_output=True, text=True, check=False)
        if result.returncode == 0 and re.fullmatch(r"[A-Za-z0-9_.]+", result.stdout.strip()):
            return result.stdout.strip()
    raise BuildError(f"Cannot inspect {path} package ID; a working Android SDK aapt2 or apkanalyzer is required")


def source_description(source):
    if source["kind"] == "upstream-ci":
        return (f"upstream CI [{source['sha'][:12]}](https://github.com/{source['repo']}/commit/{source['sha']}); "
                f"[run {source['run_id']}]({source['run_url']}); `{source['artifact_name']}`")
    return f"**official release** [{source['tag']}]({source['release_url']}); `{source['asset_name']}`"


def finalize(args):
    sources = read_json(args.sources)
    modules = []
    for name, source in sources["modules"].items():
        if name not in MODULES or source.get("package") != MODULES[name]["package"]:
            raise BuildError(f"Unexpected module identity in sources.json: {name}")
        path = Path("modules") / f"{name}.apk"
        validate_apk(path, name)
        actual_package = apk_package(path)
        if actual_package != source["package"]:
            raise BuildError(f"{name}: expected package {source['package']}, got {actual_package}")
        modules.append({"name": name, "package": actual_package, "path": str(path),
                        "sha256": sha256(path), "size": path.stat().st_size, "source": source})
    if not modules:
        raise BuildError("No selected modules to finalize")
    write_json(args.manifest, {"schema": 1, "modules": modules})
    notes = ["## Bundled modules", "", "| Module | Exact source |", "| --- | --- |"]
    for module in modules:
        notes.append(f"| {module['name']} | {source_description(module['source'])} |")
    release_modules = [m["name"] for m in modules if m["source"]["kind"] == "official-release"]
    if release_modules:
        notes.extend(["", ", ".join(release_modules) + " use their latest official releases because their "
                      "public repositories do not publish downloadable APK CI artifacts."])
    notes.extend(["", "MQGA downloads upstream APKs only; no module source is compiled."])
    notes.extend(["", "## QQ and LSPatch", ""])
    for qq in sources["qq"]:
        notes.append(f"- QQ {qq['version']} ({qq['kind']})")
    notes.append(f"- LSPatch: {source_description(sources['lspatch'])}")
    notes.extend(["", f"All {len(modules)} selected module APKs are checked for their package IDs, and their bytes "
                  "are verified inside every patched APK. `modules.json` records the pinned sources and SHA-256 values.",
                  "", "The six upstream module APKs remain unchanged. The paired-variant verification adds "
                  "the preset/blank loader identity and configuration policy below. ImPad "
                  "has no internal switch and attempts tablet behavior when loaded. Bundling and integrity verification do not establish "
                  "runtime compatibility or guarantee tablet login / simultaneous sessions.", ""])
    Path(args.notes).write_text("\n".join(notes), encoding="utf-8")
    output(module_list=",".join(m["path"] for m in modules), module_count=len(modules))


def verify_patched(dist, manifest):
    modules = read_json(manifest)["modules"]
    expected = {module["sha256"]: module["name"] for module in modules}
    if not modules or len(expected) != len(modules):
        raise BuildError("modules.json must contain selected modules with distinct SHA-256 values")
    outputs = sorted(Path(dist).glob("*.apk"))
    if not outputs:
        raise BuildError(f"No *.apk outputs found in {dist}")
    for path in outputs:
        validate_apk(path, path.name)
        try:
            with zipfile.ZipFile(path) as archive:
                entries = [e for e in archive.infolist() if not e.is_dir()
                           and e.filename.startswith("assets/lspatch/modules/") and e.filename.endswith(".apk")]
                hashes = [hashlib.sha256(archive.read(entry)).hexdigest() for entry in entries]
        except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
            raise BuildError(f"{path.name}: cannot read embedded module APKs") from exc
        if len(hashes) != len(expected) or set(hashes) != set(expected):
            missing = [expected[h] for h in expected if h not in hashes]
            raise BuildError(f"{path.name}: embedded module bytes differ; missing/mismatched {missing}, "
                             f"expected {len(expected)} APKs, found {len(hashes)}")
        print(f"Verified {path.name}: all {len(expected)} embedded module APK hashes match modules.json")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("resolve", "fetch", "finalize"):
        subparser = commands.add_parser(command)
        subparser.add_argument("--sources", default="sources.json")
        if command == "resolve":
            subparser.add_argument("--versions", default="versions.json")
        if command == "finalize":
            subparser.add_argument("--manifest", default="modules.json")
            subparser.add_argument("--notes", default="release-notes.md")
    verify = commands.add_parser("verify-patched")
    verify.add_argument("--dist", default="dist")
    verify.add_argument("--manifest", default="modules.json")
    args = parser.parse_args()
    try:
        if args.command == "verify-patched":
            verify_patched(args.dist, args.manifest)
        else:
            {"resolve": resolve, "fetch": fetch, "finalize": finalize}[args.command](args)
    except (BuildError, OSError, KeyError, ValueError) as exc:
        print(f"::error::{redact(str(exc))}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
