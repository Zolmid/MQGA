#!/usr/bin/env python3
"""Build paired QQ editions without rebuilding or altering upstream module APKs."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
import zlib

LOADER_ENTRY = 'assets/lspatch/loader.dex'
ORIGIN_ENTRY = 'assets/lspatch/origin.apk'
JADX_TAG = 'v1.5.6'
JADX_ASSET = 'jadx-1.5.6.zip'
JADX_SHA256 = '545ea2be9c242511bc145755cf4bda2485ade42966e096f8b4d3da2a230e8974'
PACKAGE = 'io/github/mqga/preset/'


class PresetError(RuntimeError):
    pass


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def run(command, **kwargs):
    print('+ ' + ' '.join(map(str, command)), flush=True)
    subprocess.run(list(map(str, command)), check=True, **kwargs)


def canonical(profile):
    return json.dumps(profile, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def validate_profile(profile, manifest, sources, jar):
    if profile.get('approved') is not True or profile.get('host_package') != 'com.tencent.mobileqq':
        raise PresetError('An explicitly approved QQ preset is required')
    unsigned = {k: v for k, v in profile.items() if k != 'profile_id'}
    if hashlib.sha256(canonical(unsigned)).hexdigest() != profile.get('profile_id'):
        raise PresetError('Preset identity does not match its approved contents')
    modules = manifest.get('modules', [])
    locked = profile['module_apk_sha256']
    if len(modules) != 6 or {m['name'] for m in modules} != set(locked):
        raise PresetError('Both editions require the six reviewed modules')
    for module in modules:
        actual = sha256(module['path'])
        if actual != module['sha256'] or actual != locked[module['name']]:
            raise PresetError(f"{module['name']}: latest downloaded APK differs from the reviewed preset adapter. "
                              'Review its actual keys/lifecycle before publishing paired editions; no stale preset was applied.')
    if sha256(jar) != profile['lspatch_sha256']:
        raise PresetError('The downloaded LSPatch loader changed; review injection anchors before publishing')
    for qq in sources['qq']:
        if qq['version'] != profile['reviewed_qq_version']:
            raise PresetError(f"QQ {qq['version']} has not been reviewed for this preset")
    targets = set()
    values = {}
    for row in profile['writes']:
        s = row['storage']
        value = row['proposed_value']
        kind = s['type']
        if kind in ('Boolean', 'file_marker') and not isinstance(value, bool):
            raise PresetError(f"Invalid Boolean: {row['item_id']}")
        if kind == 'String' and not isinstance(value, str):
            raise PresetError(f"Invalid String: {row['item_id']}")
        if kind in ('Set<String>', 'List<String>') and not (isinstance(value, list) and all(isinstance(v, str) for v in value)):
            raise PresetError(f"Invalid string collection: {row['item_id']}")
        if kind in ('Int', 'Long'):
            bits = 32 if kind == 'Int' else 64
            if type(value) is not int or not -(2 ** (bits - 1)) <= value < 2 ** (bits - 1):
                raise PresetError(f"Invalid {kind}: {row['item_id']}")
        if kind == 'Float' and (not isinstance(value, (int, float)) or isinstance(value, bool)
                                or not math.isfinite(value) or abs(value) > 3.4028234663852886e38):
            raise PresetError(f"Invalid Float: {row['item_id']}")
        if kind == 'Set<Int>' and not (isinstance(value, list) and all(type(v) is int for v in value)):
            raise PresetError(f"Invalid integer collection: {row['item_id']}")
        if kind not in ('Boolean', 'String', 'Set<String>', 'List<String>', 'Set<Int>', 'Int', 'Long', 'Float', 'file_marker'):
            raise PresetError('Unsupported native preset type: ' + kind)
        identity = (row['module'], json.dumps(s.get('path') or s.get('upstream_storage'), sort_keys=True), s['key'])
        if identity in targets:
            raise PresetError('Duplicate native write target: ' + str(identity))
        targets.add(identity)
        values[row['item_id']] = value
        policy = row.get('automatic_seed_policy', {})
        if not policy.get('requires_new_or_pristine_module_configuration_scope') or not policy.get('missing_key_alone_is_not_sufficient'):
            raise PresetError('Unsafe existing-configuration policy')
        if s['adapter'] == 'qwish_feature_map' and s['entry_fields'].get('bypassDefaultLoad') is not True:
            raise PresetError('QWish explicit settings must override its default-loading policy')
    required_false = ['QWish:C1349', 'QWish:C1088', 'QWish:C0847', 'QWish:C0851',
                      'QAuxiliary:me.hd.hook.auxiliary.experimental.DisableFekitToAppDialog',
                      'QAuxiliary:cc.ioctl.hook.experimental.ForcePhoneMode',
                      'XAutoDaily:task:群打卡', 'XAutoDaily:task:好友点赞', 'XAutoDaily:task:QQ运动步数']
    if values.get('TSBattery:TS-01') is not True or any(values.get(k) is not False for k in required_false):
        raise PresetError('Reviewed conflict or incomplete-task rules do not hold')
    if any(row['storage'].get('path', '').startswith('<standalone') for row in profile['writes']):
        raise PresetError('QQ cannot initialize a standalone module app configuration')
    return profile


def fetch_tools(destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    existing = list(destination.glob('lib/jadx-*-all.jar'))
    marker = destination / '.verified-release'
    if len(existing) == 1 and marker.exists() and marker.read_text() == JADX_SHA256:
        return existing[0]
    with tempfile.TemporaryDirectory(prefix='mqga-jadx-') as temporary:
        run(['gh', 'release', 'download', JADX_TAG, '--repo', 'skylot/jadx', '--pattern', JADX_ASSET, '--dir', temporary])
        archive = Path(temporary) / JADX_ASSET
        if sha256(archive) != JADX_SHA256:
            raise PresetError('Official jadx tool archive digest mismatch')
        with zipfile.ZipFile(archive) as z:
            jars = [n for n in z.namelist() if re.fullmatch(r'lib/jadx-[^/]+-all\.jar', n)]
            if len(jars) != 1:
                raise PresetError('Expected one official jadx all-in-one CLI jar')
            target = destination / jars[0]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(jars[0]))
    marker.write_text(JADX_SHA256)
    return target


def tool_main(jar, role):
    candidates = {
        'baksmali': ['com.android.tools.smali.baksmali.Main', 'org.jf.baksmali.Main'],
        'smali': ['com.android.tools.smali.smali.Main', 'org.jf.smali.Main'],
        'd8': ['com.android.tools.r8.D8'],
    }
    with zipfile.ZipFile(jar) as z:
        names = set(z.namelist())
    for name in candidates[role]:
        if name.replace('.', '/') + '.class' in names:
            return name
    raise PresetError('Official CLI tool is missing ' + role)


def android_api(explicit=None):
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise PresetError('Android compile API does not exist')
        return path
    root = os.environ.get('ANDROID_HOME') or os.environ.get('ANDROID_SDK_ROOT')
    choices = list(Path(root).glob('platforms/android-*/android.jar')) if root else []
    if not choices:
        raise PresetError('An Android SDK platform is required to compile the preset initializer')
    return sorted(choices, key=lambda p: int(re.search(r'android-(\d+)', str(p)).group(1)))[-1]


def inject_loader_smali(root):
    app = root / 'org/lsposed/lspatch/loader/LSPApplication.smali'
    xposed = root / 'de/robv/android/xposed/XposedInit.smali'
    modern = root / 'org/matrix/vector/impl/core/VectorModuleManager.smali'
    source = app.read_text()
    # The source-locked loader keeps the Context returned by createLoadedApkWithContext
    # in the same register until loadModulesAndDeliver. Use both anchors to verify it.
    context = re.findall(r'createLoadedApkWithContext\(\)Landroid/content/Context;\s+move-result-object (v\d+)', source)
    delivery = re.findall(r'invoke-static \{(v\d+)\}, Lorg/lsposed/lspatch/loader/LSPApplication;->loadModulesAndDeliver\(Landroid/content/Context;\)V', source)
    if len(context) != 1 or context != delivery:
        raise PresetError('Cannot prove the loader Context register at the injection point')
    anchor = re.findall(r'^    invoke-static \{[^}]+\}, Lorg/matrix/vector/Startup;->bootstrapXposed\(Z\)V$', source, re.M)
    if len(anchor) != 1 or 'Lio/github/mqga/preset/Bootstrap;' in source:
        raise PresetError('Unexpected or already modified loader startup')
    call = f'    invoke-static {{{context[0]}}}, Lio/github/mqga/preset/Bootstrap;->start(Landroid/content/Context;)V\n\n'
    app.write_text(source.replace(anchor[0], call + anchor[0]))

    source = xposed.read_text()
    pattern = r'(\.method private static initModule\(Ljava/lang/ClassLoader;Ljava/lang/String;Ljava/util/List;\)Z\n)(.*?)(\.end method)'
    matches = list(re.finditer(pattern, source, re.S))
    if len(matches) != 1 or 'Lio/github/mqga/preset/Bootstrap;' in source:
        raise PresetError('Unexpected legacy module initialization anchor')
    match = matches[0]
    body = match.group(2)
    line = re.search(r'^    \.line \d+', body, re.M)
    if line is None or '.registers 10' not in body:
        raise PresetError('Unexpected legacy module register layout')
    call = '    invoke-static {p0, p1}, Lio/github/mqga/preset/Bootstrap;->moduleLoaded(Ljava/lang/ClassLoader;Ljava/lang/String;)V\n\n'
    replacement = match.group(1) + body[:line.start()] + call + body[line.start():] + match.group(3)
    xposed.write_text(source[:match.start()] + replacement + source[match.end():])

    # API 101/102 metadata takes precedence over assets/xposed_init. In the
    # reviewed APKs QA and QFun therefore enter through the modern loader.
    # Hook after its API class-identity check, before it loads any entry class.
    source = modern.read_text()
    pattern = r'(\.method private final buildGeneration\(Lorg/matrix/vector/ipc/LoadedModule;ZLjava/lang/String;\)Lkotlin/Pair;\n)(.*?)(\.end method)'
    matches = list(re.finditer(pattern, source, re.S))
    if len(matches) != 1 or 'Lio/github/mqga/preset/Bootstrap;' in source:
        raise PresetError('Unexpected modern module initialization anchor')
    match = matches[0]
    body = match.group(2)
    creation = ('invoke-virtual/range {v7 .. v12}, Lorg/matrix/vector/impl/utils/VectorModuleClassLoader$Companion;'
                '->loadApk(Ljava/lang/String;Ljava/util/List;Ljava/lang/String;Ljava/lang/ClassLoader;Z)Ljava/lang/ClassLoader;'
                '\n\n    move-result-object v15')
    path = 'iget-object v8, v1, Lorg/matrix/vector/ipc/LoadedModule;->apkPath:Ljava/lang/String;'
    anchor = '    :cond_af\n    new-instance v4, Lorg/matrix/vector/impl/VectorContext;'
    if ('.registers 24' not in body or body.count(creation) != 1 or body.count(path) != 1
            or body.count(anchor) != 1 or body.index(path) > body.index(creation)
            or body.index(creation) > body.index(anchor)
            or 'if-eq v0, v11, :cond_af' not in body):
        raise PresetError('Cannot prove the modern module ClassLoader and APK path registers')
    call = ('    :cond_af\n'
            '    invoke-static {v15, v8}, Lio/github/mqga/preset/Bootstrap;->moduleLoaded(Ljava/lang/ClassLoader;Ljava/lang/String;)V\n\n'
            '    new-instance v4, Lorg/matrix/vector/impl/VectorContext;')
    replacement = match.group(1) + body.replace(anchor, call) + match.group(3)
    modern.write_text(source[:match.start()] + replacement + source[match.end():])


def java_command(jar, role, *args):
    return ['java', '-XX:-UsePerfData', '-cp', str(jar), tool_main(jar, role), *map(str, args)]


def build_loader(profile, source_jar, api, tools_jar, directory, output):
    directory = Path(directory)
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    original = directory / 'original-loader.dex'
    with zipfile.ZipFile(source_jar) as z:
        original.write_bytes(z.read(LOADER_ENTRY))
    smali = directory / 'original-smali'
    run(java_command(tools_jar, 'baksmali', 'disassemble', original, '-o', smali))
    inject_loader_smali(smali)
    patched_base = directory / 'patched-base.dex'
    run(java_command(tools_jar, 'smali', 'assemble', smali, '-o', patched_base))

    generated = directory / 'generated/io/github/mqga/preset/PresetData.java'
    generated.parent.mkdir(parents=True)
    encoded = base64.b64encode(canonical(profile)).decode('ascii')
    chunks = [encoded[n:n + 16000] for n in range(0, len(encoded), 16000)]
    source = ['package io.github.mqga.preset;', 'final class PresetData {',
              f'  static final String PROFILE_ID = "{profile["profile_id"]}";',
              '  static String json() { StringBuilder b = new StringBuilder();']
    source += [f'    b.append("{part}");' for part in chunks]
    source += ['    return new String(android.util.Base64.decode(b.toString(), 0), java.nio.charset.StandardCharsets.UTF_8);', '  }', '}']
    generated.write_text('\n'.join(source) + '\n')
    classes = directory / 'classes'
    stubs = directory / 'stubs'
    classes.mkdir()
    stubs.mkdir()
    javac = ['java', '-XX:-UsePerfData', '-m', 'jdk.compiler/com.sun.tools.javac.Main', '-source', '8', '-target', '8', '-encoding', 'UTF-8']
    run(javac + ['-cp', str(api), '-d', str(stubs)] + sorted(map(str, Path('preset-runtime/stubs').rglob('*.java'))))
    inputs = sorted(Path('preset-runtime/src').rglob('*.java'))
    required = {'Bootstrap.java', 'QaAdapter.java', 'QFunAdapter.java', 'QWishAdapter.java', 'XadAdapter.java'}
    if {p.name for p in inputs} != required:
        raise PresetError('Preset adapter source set is incomplete or unexpected')
    run(javac + ['-cp', os.pathsep.join([str(api), str(stubs)]), '-d', str(classes)] + list(map(str, inputs)) + [str(generated)])
    runtime_jar = directory / 'runtime.jar'
    with zipfile.ZipFile(runtime_jar, 'w', zipfile.ZIP_DEFLATED) as z:
        for p in sorted(classes.rglob('*.class')):
            name = p.relative_to(classes).as_posix()
            if not name.startswith(PACKAGE):
                raise PresetError('Compile-only APIs must never enter the runtime DEX')
            z.write(p, name)
    dex_dir = directory / 'merged'
    dex_dir.mkdir()
    run(java_command(tools_jar, 'd8', '--release', '--min-api', '28', '--lib', api,
                     '--output', dex_dir, patched_base, runtime_jar))
    dexes = list(dex_dir.glob('*.dex'))
    if len(dexes) != 1 or dexes[0].name != 'classes.dex':
        raise PresetError('Native loader requires exactly one loader DEX')
    loader = dexes[0]
    checked = directory / 'verified-smali'
    run(java_command(tools_jar, 'baksmali', 'disassemble', loader, '-o', checked))
    app = (checked / 'org/lsposed/lspatch/loader/LSPApplication.smali').read_text()
    legacy = (checked / 'de/robv/android/xposed/XposedInit.smali').read_text()
    modern = (checked / 'org/matrix/vector/impl/core/VectorModuleManager.smali').read_text()
    module_call = '->moduleLoaded(Ljava/lang/ClassLoader;Ljava/lang/String;)V'
    if (app.count('->start(Landroid/content/Context;)V') != 1
            or legacy.count(module_call) != 1 or modern.count(module_call) != 1):
        raise PresetError('Final DEX must arm the preset before both legacy and modern module entries')
    for name in ('Bootstrap', 'PresetData', 'QaAdapter', 'QFunAdapter', 'QWishAdapter', 'XadAdapter'):
        if not (checked / PACKAGE / (name + '.smali')).is_file():
            raise PresetError('Final DEX is missing a preset class: ' + name)
    data = (checked / PACKAGE / 'PresetData.smali').read_text()
    if profile['profile_id'] not in data or any(chunk not in data for chunk in chunks):
        raise PresetError('Final DEX preset payload differs from the approved profile')
    with zipfile.ZipFile(source_jar) as source, zipfile.ZipFile(output, 'w') as dest:
        for info in source.infolist():
            dest.writestr(info, loader.read_bytes() if info.filename == LOADER_ENTRY else source.read(info))
    with zipfile.ZipFile(source_jar) as a, zipfile.ZipFile(output) as b:
        if a.namelist() != b.namelist():
            raise PresetError('Unexpected patcher archive entry changes')
        changed = [n for n in a.namelist() if a.read(n) != b.read(n)]
        if changed != [LOADER_ENTRY]:
            raise PresetError('Only the patcher loader DEX may change: ' + str(changed))
    return {'schema_version': 1, 'profile_id': profile['profile_id'],
            'original_patcher_sha256': sha256(source_jar), 'preset_patcher_sha256': sha256(output),
            'blank_loader_sha256': sha256(original), 'preset_loader_sha256': sha256(loader),
            'preset_payload_sha256': hashlib.sha256(canonical(profile)).hexdigest(),
            'runtime_sources': {p.as_posix(): sha256(p) for p in inputs},
            'tool_jar_sha256': sha256(tools_jar), 'android_compile_api_sha256': sha256(api),
            'modified_patcher_entries': changed, 'compiled_upstream_modules': [],
            'covered_module_entrypoints': ['legacy', 'modern'],
            'runtime_tested_on_device': False}


def prepare(args):
    profile = validate_profile(read_json(args.profile), read_json('modules.json'), read_json('sources.json'), args.lspatch)
    tools = Path(args.jadx_jar) if args.jadx_jar else fetch_tools(args.tools)
    info = build_loader(profile, Path(args.lspatch), android_api(args.android_jar), tools, args.build_dir, 'LSPatch-preset.jar')
    write_json('preset-effective.json', profile)
    write_json('preset-loader.json', info)
    print(f"Prepared preset {profile['profile_id']} with {len(profile['writes'])} typed writes; upstream APK bytes unchanged")


def patch(args):
    profile = validate_profile(read_json('preset-effective.json'), read_json('modules.json'), read_json('sources.json'), 'LSPatch.jar')
    info = read_json('preset-loader.json')
    if sha256('LSPatch-preset.jar') != info['preset_patcher_sha256']:
        raise PresetError('Prepared patcher digest mismatch')
    modules = read_json('modules.json')['modules']
    sources = read_json('sources.json')
    build_id = os.environ.get('GITHUB_RUN_ID', 'local')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', build_id):
        raise PresetError('Invalid build ID')
    dist = Path(args.dist)
    dist.mkdir(parents=True, exist_ok=True)
    if list(dist.glob('*.apk')):
        raise PresetError('Refusing to mix paired outputs with an existing dist directory')
    pairs = []
    for qq in sources['qq']:
        original = Path(qq['path'])
        pair = {'qq_version': qq['version'], 'qq_kind': qq['kind'], 'input_path': str(original),
                'input_sha256': sha256(original), 'variants': {}}
        for variant, jar in [('blank', 'LSPatch.jar'), ('preset', 'LSPatch-preset.jar')]:
            with tempfile.TemporaryDirectory(prefix='mqga-patch-') as temporary:
                run(['java', '-XX:-UsePerfData', '-jar', jar, original, '-m', ','.join(m['path'] for m in modules),
                     '-l', '2', '-o', temporary])
                outputs = list(Path(temporary).glob('*-lspatched.apk'))
                if len(outputs) != 1:
                    raise PresetError('Expected exactly one LSPatch output for ' + variant)
                name = f"QQ-{qq['version']}-{qq['kind']}-{build_id}-{variant}.apk"
                shutil.move(str(outputs[0]), dist / name)
                pair['variants'][variant] = name
        pairs.append(pair)
    write_json('variants-build.json', {'schema_version': 1, 'profile_id': profile['profile_id'], 'pairs': pairs})


def find_apksigner():
    root = os.environ.get('ANDROID_HOME') or os.environ.get('ANDROID_SDK_ROOT')
    paths = list(Path(root).glob('build-tools/*/apksigner')) if root else []
    if paths:
        return sorted(paths)[-1]
    found = shutil.which('apksigner')
    if found:
        return Path(found)
    raise PresetError('Android apksigner is required to verify both edition signatures')


def signing_digest(path, signer):
    result = subprocess.run([str(signer), 'verify', '--verbose', '--print-certs', str(path)], text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    print(f'APK signature report: {path.name}\n{result.stdout}', flush=True)
    if result.returncode:
        raise PresetError(f'APK signature verification failed: {path.name}: {result.stderr[:500]}')
    lines = result.stdout.splitlines()
    count = [line for line in lines if line.startswith('Number of signers')]
    if len(count) != 1 or not re.fullmatch(r'Number of signers: 1[ \t]*', count[0]):
        raise PresetError('Expected exactly one verified APK signer for ' + path.name)
    # AOSP ApkSignerTool uses SDK ranges rather than #N for v3.1 certificates.
    # Its v3/v3.1 sections may repeat the same certificate for different ranges.
    # Keep the one-identity rule: no extra signer, rotated/different certificate,
    # missing digest or Source Stamp certificate can stand in for the APK signer.
    pattern = (r'Signer (?:#([0-9]+)|\(minSdkVersion=[0-9]+'
               r'(?: \(dev release=true\))?, maxSdkVersion=[0-9]+\))'
               r' certificate SHA-256 digest: ([a-fA-F0-9]{64})[ \t]*')
    candidates = [line for line in lines if line.startswith('Signer ') and ' certificate SHA-256 digest:' in line]
    records = []
    for line in candidates:
        record = re.fullmatch(pattern, line)
        if record is None:
            raise PresetError('Unrecognized signer certificate record for ' + path.name)
        records.append(record.groups(default=''))
    digests = {digest.lower() for _, digest in records}
    if not records or any(number not in ('', '1') for number, _ in records) or len(digests) != 1:
        raise PresetError('Expected one signer certificate identity across all SDK ranges for ' + path.name)
    return next(iter(digests))


def embedded_original_sha256(apk, expected_size):
    """Hash LSPatch's stored original APK, including its intentional nested links.

    NestedZip exposes original entries in the outer ZIP without duplicating their
    bytes. Python's general ZipFile.read consequently rejects origin.apk as an
    overlapping entry. Read only this known, stored entry with explicit size,
    header, bounds and CRC checks; never disable overlap checks for other entries.
    """
    apk = Path(apk)
    with zipfile.ZipFile(apk) as archive:
        matches = [entry for entry in archive.infolist() if entry.filename == ORIGIN_ENTRY]
        if len(matches) != 1:
            raise PresetError('Expected one nested original QQ entry')
        entry = matches[0]
    if (expected_size <= 0 or entry.file_size != expected_size
            or entry.compress_size != expected_size or entry.compress_type != zipfile.ZIP_STORED
            or entry.flag_bits & 1):
        raise PresetError('Nested original QQ must be stored with the exact input size')
    digest = hashlib.sha256()
    crc = 0
    with apk.open('rb') as stream:
        stream.seek(entry.header_offset)
        header = stream.read(30)
        if len(header) != 30:
            raise PresetError('Truncated original QQ local header')
        fields = struct.unpack('<4s5H3I2H', header)
        name_size, extra_size = fields[-2:]
        if (fields[0] != b'PK\x03\x04' or fields[2] != entry.flag_bits
                or fields[3] != zipfile.ZIP_STORED or name_size != len(ORIGIN_ENTRY)
                or stream.read(name_size) != ORIGIN_ENTRY.encode('ascii')):
            raise PresetError('Unexpected original QQ local header')
        if not (entry.flag_bits & 8) and (fields[6], fields[7], fields[8]) != (entry.CRC, expected_size, expected_size):
            raise PresetError('Original QQ local header disagrees with the ZIP directory')
        offset = entry.header_offset + 30 + name_size + extra_size
        if offset < 0 or offset + expected_size > apk.stat().st_size:
            raise PresetError('Original QQ entry exceeds APK bounds')
        stream.seek(offset)
        remaining = expected_size
        while remaining:
            block = stream.read(min(remaining, 1024 * 1024))
            if not block:
                raise PresetError('Truncated nested original QQ entry')
            digest.update(block)
            crc = zlib.crc32(block, crc)
            remaining -= len(block)
    if crc & 0xffffffff != entry.CRC:
        raise PresetError('Nested original QQ CRC mismatch')
    return digest.hexdigest()


def verify(args):
    from ci_modules import apk_package
    spec = read_json('variants-build.json')
    loader = read_json('preset-loader.json')
    profile = read_json('preset-effective.json')
    modules = read_json('modules.json')['modules']
    if spec.get('profile_id') != profile.get('profile_id') or loader.get('profile_id') != profile.get('profile_id'):
        raise PresetError('Paired outputs and loader must refer to the same approved profile')
    if loader['blank_loader_sha256'] == loader['preset_loader_sha256']:
        raise PresetError('Preset and blank loaders must differ')
    dist = Path(args.dist)
    expected = {name for pair in spec['pairs'] for name in pair['variants'].values()}
    if {p.name for p in dist.glob('*.apk')} != expected or not expected:
        raise PresetError('Paired release output set is incomplete or has extra APKs')
    signer = find_apksigner()
    records = []
    for pair in spec['pairs']:
        if set(pair['variants']) != {'blank', 'preset'} or len(set(pair['variants'].values())) != 2:
            raise PresetError('Every QQ input requires one distinct preset and blank APK')
        if sha256(pair['input_path']) != pair['input_sha256']:
            raise PresetError('QQ input changed after paired patching')
        signatures = set()
        origins = set()
        for edition in ('blank', 'preset'):
            path = dist / pair['variants'][edition]
            if apk_package(path) != profile['host_package']:
                raise PresetError('Unexpected QQ package identity')
            certificate = signing_digest(path, signer)
            signatures.add(certificate)
            with zipfile.ZipFile(path) as z:
                actual_loader = hashlib.sha256(z.read(LOADER_ENTRY)).hexdigest()
                if actual_loader != loader[edition + '_loader_sha256']:
                    raise PresetError('Edition loader mismatch: ' + path.name)
                origin = embedded_original_sha256(path, Path(pair['input_path']).stat().st_size)
                if origin != pair['input_sha256']:
                    raise PresetError('Embedded QQ bytes differ from the locked original input')
                origins.add(origin)
                embedded = {Path(n).name[:-4]: hashlib.sha256(z.read(n)).hexdigest() for n in z.namelist()
                            if n.startswith('assets/lspatch/modules/') and n.endswith('.apk')}
                if embedded != {m['package']: m['sha256'] for m in modules}:
                    raise PresetError('Paired edition contains changed module bytes')
            records.append({'file': path.name, 'edition': edition, 'bytes': path.stat().st_size,
                            'sha256': sha256(path), 'qq_input_sha256': pair['input_sha256'],
                            'embedded_qq_sha256': origin, 'signer_sha256': certificate,
                            'loader_sha256': actual_loader, 'embedded_modules': embedded,
                            'preset_profile_id': profile['profile_id'] if edition == 'preset' else None})
        if len(signatures) != 1 or len(origins) != 1:
            raise PresetError('The two editions do not share the same signer or embedded QQ base')
    result = {'schema_version': 1, 'profile_id': profile['profile_id'], 'variants': records,
              'checks': {'paired_outputs_complete': True, 'same_qq_and_module_inputs': True,
                         'apk_signatures_valid_and_identical': True, 'blank_original_loader': True,
                         'preset_initializer_and_payload_present': True},
              'device_runtime_verified': False, 'simultaneous_login_verified': False}
    write_json('variants.json', result)
    sums = [f"{record['sha256']}  {record['file']}" for record in records]
    sums += [f'{sha256(p)}  {p}' for p in ('sources.json', 'versions.json', 'modules.json', 'variants.json', 'preset-effective.json', 'preset-loader.json')]
    Path('SHA256SUMS').write_text('\n'.join(sums) + '\n')
    notes = Path('release-notes.md')
    text = notes.read_text() + '\n## 预设版与空白版\n\n| 版本 | APK | SHA-256 |\n| --- | --- | --- |\n'
    for record in records:
        text += f"| {record['edition']} | `{record['file']}` | `{record['sha256']}` |\n"
    text += ('\n- 两版使用同一 QQ 底包、同一六模块原始 APK 和同一签名；六模块没有自行编译或修改。\n'
             '- **preset 预设版**：包含已确认配置和实际首次运行写入器；仅初始化全新/空白的模块或账号配置域。\n'
             '- **blank 空白版**：使用原始 LSPatch 加载器，不带 MQGA 预设代码；保留模块上游默认值。\n'
             '- 覆盖安装保留应用数据；空白版不会清除旧预设，也不代表全部 Hook 关闭。两版同包名，不能同时安装。\n'
             '- 账号、好友和群列表不内置。群打卡、好友点赞、步数及其它缺参项目按已确认方案暂缓；见 preset-effective.json。\n'
             '- QA 首次使用协议及其它模块正常资格/权限门槛仍需用户自行完成；预设不代替这些操作。\n'
             '- 初始化日志使用 MQGA-Preset 标签，状态标记在宿主 files/mqga-presets/；失败不会宣称完成。\n'
             '- 已验证构建、DEX接入、签名、变体和嵌入模块哈希；没有进行 Android 17 / QQ 实机验证，不保证平板并存登录。\n')
    notes.write_text(text)
    print(f'Verified {len(records)} APKs as {len(spec["pairs"])} complete preset/blank pairs')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('--profile', default='presets/default.json')
    prep.add_argument('--lspatch', default='LSPatch.jar')
    prep.add_argument('--android-jar')
    prep.add_argument('--jadx-jar')
    prep.add_argument('--tools', default='preset-tools')
    prep.add_argument('--build-dir', default='preset-build')
    for name in ('patch', 'verify'):
        cmd = sub.add_parser(name)
        cmd.add_argument('--dist', default='dist')
    args = parser.parse_args()
    try:
        {'prepare': prepare, 'patch': patch, 'verify': verify}[args.command](args)
    except (PresetError, OSError, KeyError, ValueError, subprocess.CalledProcessError, zipfile.BadZipFile) as error:
        print('Preset build failed: ' + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
