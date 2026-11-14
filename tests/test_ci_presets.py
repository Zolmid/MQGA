"""Preset policy and release gates, using only tiny in-memory ZIP fixtures.

These tests do not download QQ/modules, run JADX, or claim Android execution.
The checked-in approved profile is exercised with mocked artifact digests;
separate synthetic inputs exercise real file hashing and paired-output checks.
"""

import base64
import copy
from contextlib import contextmanager, redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest import mock
import warnings
import zipfile
import zlib


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ci_presets_tested", ROOT / ".github/scripts/ci_presets.py")
presets = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(presets)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def tiny_apk(label):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("AndroidManifest.xml", b"unit-test-manifest:" + label)
        archive.writestr("classes.dex", b"unit-test-dex:" + label)
    return output.getvalue()


def checked_in_profile():
    return json.loads((ROOT / "presets/default.json").read_text(encoding="utf-8"))


def seal(profile):
    unsigned = {key: value for key, value in profile.items() if key != "profile_id"}
    profile["profile_id"] = digest(presets.canonical(unsigned))


@contextmanager
def working_directory(path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class Inputs:
    """Real byte/hash inputs with the approved profile's actual write schema."""

    def __init__(self, root):
        self.root = root
        self.profile = checked_in_profile()
        self.manifest = {"modules": []}
        for index, name in enumerate(self.profile["module_apk_sha256"]):
            path = root / (name + ".apk")
            path.write_bytes(tiny_apk(name.encode("utf-8")))
            value = digest(path.read_bytes())
            self.profile["module_apk_sha256"][name] = value
            self.manifest["modules"].append({
                "name": name, "package": "unit.module.m" + str(index),
                "path": str(path), "sha256": value,
                "source": {"kind": "upstream-ci", "sha": "a" * 40},
            })
        self.jar = root / "LSPatch.jar"
        with zipfile.ZipFile(self.jar, "w") as archive:
            archive.writestr(presets.LOADER_ENTRY, b"original-loader")
        self.profile["lspatch_sha256"] = digest(self.jar.read_bytes())
        self.qq = root / "input-QQ.apk"
        self.qq.write_bytes(tiny_apk(b"QQ-base"))
        self.sources = {"qq": [{"version": self.profile["reviewed_qq_version"],
                                "kind": "latest", "path": str(self.qq)}]}
        seal(self.profile)

    def validate(self):
        return presets.validate_profile(self.profile, self.manifest, self.sources, self.jar)


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.inputs = Inputs(Path(self.temporary.name))

    def test_actual_checked_in_profile_passes_approval_schema_and_identity(self):
        profile = checked_in_profile()
        manifest = {"modules": [{"name": name, "path": name + ".apk", "sha256": value}
                                for name, value in profile["module_apk_sha256"].items()]}
        hashes = {module["path"]: module["sha256"] for module in manifest["modules"]}
        hashes["LSPatch.jar"] = profile["lspatch_sha256"]
        sources = {"qq": [{"version": profile["reviewed_qq_version"]}]}
        # No downloaded APKs are required to check the exact approved document.
        with mock.patch.object(presets, "sha256", side_effect=lambda path: hashes[str(path)]):
            self.assertIs(presets.validate_profile(profile, manifest, sources, "LSPatch.jar"), profile)

    def test_real_file_hashes_accept_matching_synthetic_inputs(self):
        self.assertIs(self.inputs.validate(), self.inputs.profile)

    def test_unapproved_or_modified_unsealed_profile_is_rejected(self):
        original = copy.deepcopy(self.inputs.profile)
        self.inputs.profile["approved"] = False
        with self.assertRaisesRegex(presets.PresetError, "approved"):
            self.inputs.validate()
        self.inputs.profile = original
        self.inputs.profile["writes"][0]["proposed_value"] = False
        with self.assertRaisesRegex(presets.PresetError, "identity"):
            self.inputs.validate()

    def test_changed_module_bytes_fail_even_when_manifest_is_updated(self):
        module = self.inputs.manifest["modules"][0]
        path = Path(module["path"])
        path.write_bytes(tiny_apk(b"new-upstream-CI-APK"))
        with self.assertRaisesRegex(presets.PresetError, "differs from the reviewed"):
            self.inputs.validate()
        module["sha256"] = digest(path.read_bytes())
        with self.assertRaisesRegex(presets.PresetError, "differs from the reviewed"):
            self.inputs.validate()

    def test_stale_manifest_or_missing_module_cannot_pass_the_gate(self):
        module = self.inputs.manifest["modules"][0]
        original = module["sha256"]
        module["sha256"] = "0" * 64
        with self.assertRaises(presets.PresetError):
            self.inputs.validate()
        module["sha256"] = original
        self.inputs.manifest["modules"].pop()
        with self.assertRaisesRegex(presets.PresetError, "six reviewed"):
            self.inputs.validate()

    def test_changed_ci_identity_with_identical_reviewed_bytes_is_allowed(self):
        self.inputs.manifest["modules"][0]["source"]["sha"] = "b" * 40
        self.inputs.validate()

    def test_changed_loader_or_unreviewed_qq_is_rejected(self):
        original = self.inputs.jar.read_bytes()
        self.inputs.jar.write_bytes(original + b"different-patcher")
        with self.assertRaisesRegex(presets.PresetError, "LSPatch loader changed"):
            self.inputs.validate()
        self.inputs.jar.write_bytes(original)
        self.inputs.sources["qq"][0]["version"] = "unreviewed-QQ"
        with self.assertRaisesRegex(presets.PresetError, "not been reviewed"):
            self.inputs.validate()

    def test_unsafe_scope_conflict_and_duplicate_writes_are_rejected(self):
        original = copy.deepcopy(self.inputs.profile)
        cases = ("existing_scope", "incomplete_task", "duplicate", "qwish_default")
        for case in cases:
            with self.subTest(case=case):
                self.inputs.profile = copy.deepcopy(original)
                writes = self.inputs.profile["writes"]
                if case == "existing_scope":
                    writes[0]["automatic_seed_policy"]["missing_key_alone_is_not_sufficient"] = False
                elif case == "incomplete_task":
                    next(w for w in writes if w["item_id"] == "XAutoDaily:task:好友点赞")["proposed_value"] = True
                elif case == "duplicate":
                    writes.append(copy.deepcopy(writes[0]))
                else:
                    next(w for w in writes if w["storage"]["adapter"] == "qwish_feature_map")["storage"]["entry_fields"]["bypassDefaultLoad"] = False
                seal(self.inputs.profile)
                with self.assertRaises(presets.PresetError):
                    self.inputs.validate()

    def test_native_type_errors_are_rejected_before_compilation(self):
        original = copy.deepcopy(self.inputs.profile)
        cases = [("Boolean", "true"), ("String", 4), ("Int", 1.5), ("Long", 1000.5),
                 ("Int", 2 ** 31), ("Long", 2 ** 63), ("Int", True),
                 ("Float", float("inf")), ("Float", 1e100), ("file_marker", "false"),
                 ("Set<String>", [1]), ("Set<Int>", [True])]
        for kind, value in cases:
            with self.subTest(kind=kind, value=value):
                self.inputs.profile = copy.deepcopy(original)
                row = next(w for w in self.inputs.profile["writes"] if w["storage"]["type"] == kind)
                row["proposed_value"] = value
                seal(self.inputs.profile)
                with self.assertRaises(presets.PresetError):
                    self.inputs.validate()


class LoaderAnchorTests(unittest.TestCase):
    APP = """.class public Lorg/lsposed/lspatch/loader/LSPApplication;
.method public static onLoad()V
    .registers 4
    invoke-static {}, Lorg/lsposed/lspatch/loader/LSPApplication;->createLoadedApkWithContext()Landroid/content/Context;
    move-result-object v1
    invoke-static {v2}, Lorg/matrix/vector/Startup;->bootstrapXposed(Z)V
    invoke-static {v1}, Lorg/lsposed/lspatch/loader/LSPApplication;->loadModulesAndDeliver(Landroid/content/Context;)V
    return-void
.end method
"""
    XPOSED = """.class public Lde/robv/android/xposed/XposedInit;
.method private static initModule(Ljava/lang/ClassLoader;Ljava/lang/String;Ljava/util/List;)Z
    .registers 10
    .line 1
    const/4 v0, 0x1
    return v0
.end method
"""
    MODERN = """.class public Lorg/matrix/vector/impl/core/VectorModuleManager;
.method private final buildGeneration(Lorg/matrix/vector/ipc/LoadedModule;ZLjava/lang/String;)Lkotlin/Pair;
    .registers 24
    iget-object v8, v1, Lorg/matrix/vector/ipc/LoadedModule;->apkPath:Ljava/lang/String;
    invoke-virtual/range {v7 .. v12}, Lorg/matrix/vector/impl/utils/VectorModuleClassLoader$Companion;->loadApk(Ljava/lang/String;Ljava/util/List;Ljava/lang/String;Ljava/lang/ClassLoader;Z)Ljava/lang/ClassLoader;

    move-result-object v15
    if-eq v0, v11, :cond_af
    return-object v0
    :cond_af
    new-instance v4, Lorg/matrix/vector/impl/VectorContext;
    return-object v4
.end method
"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.app = self.root / "org/lsposed/lspatch/loader/LSPApplication.smali"
        self.xposed = self.root / "de/robv/android/xposed/XposedInit.smali"
        self.modern = self.root / "org/matrix/vector/impl/core/VectorModuleManager.smali"
        self.app.parent.mkdir(parents=True)
        self.xposed.parent.mkdir(parents=True)
        self.modern.parent.mkdir(parents=True)
        self.restore()

    def restore(self):
        self.app.write_text(self.APP)
        self.xposed.write_text(self.XPOSED)
        self.modern.write_text(self.MODERN)

    def test_three_calls_are_inserted_before_the_original_lifecycle_work(self):
        presets.inject_loader_smali(self.root)
        app, xposed = self.app.read_text(), self.xposed.read_text()
        modern = self.modern.read_text()
        self.assertEqual(app.count("->start(Landroid/content/Context;)V"), 1)
        self.assertLess(app.index("->start("), app.index("->bootstrapXposed("))
        self.assertIn("invoke-static {v1}, Lio/github/mqga/preset/Bootstrap;->start", app)
        self.assertEqual(xposed.count("->moduleLoaded("), 1)
        self.assertLess(xposed.index("->moduleLoaded("), xposed.index(".line 1"))
        self.assertEqual(modern.count("->moduleLoaded("), 1)
        self.assertIn("invoke-static {v15, v8}, Lio/github/mqga/preset/Bootstrap;->moduleLoaded", modern)
        self.assertLess(modern.index("move-result-object v15"), modern.index("->moduleLoaded("))
        self.assertLess(modern.index("    :cond_af\n"), modern.index("->moduleLoaded("))
        self.assertLess(modern.index("->moduleLoaded("), modern.index("new-instance v4"))
        with self.assertRaises(presets.PresetError):
            presets.inject_loader_smali(self.root)

    def test_changed_loader_context_or_startup_anchor_is_rejected(self):
        cases = {
            "register_changed": self.APP.replace("invoke-static {v1}", "invoke-static {v3}"),
            "startup_renamed": self.APP.replace("->bootstrapXposed(Z)V", "->bootstrapNew(Z)V"),
            "duplicate_startup": self.APP.replace(
                "    return-void", "    invoke-static {v2}, Lorg/matrix/vector/Startup;->bootstrapXposed(Z)V\n    return-void"),
        }
        for name, value in cases.items():
            with self.subTest(anchor=name):
                self.restore()
                self.app.write_text(value)
                with self.assertRaises(presets.PresetError):
                    presets.inject_loader_smali(self.root)

    def test_changed_legacy_method_or_register_layout_is_rejected(self):
        cases = {
            "method_changed": self.XPOSED.replace("initModule(", "initDifferentModule("),
            "registers_changed": self.XPOSED.replace(".registers 10", ".registers 11"),
            "no_code_anchor": self.XPOSED.replace("    .line 1\n", ""),
        }
        for name, value in cases.items():
            with self.subTest(anchor=name):
                self.restore()
                self.xposed.write_text(value)
                with self.assertRaises(presets.PresetError):
                    presets.inject_loader_smali(self.root)

    def test_changed_modern_identity_or_classloader_registers_are_rejected(self):
        cases = {
            "method_changed": self.MODERN.replace("buildGeneration(", "buildOtherGeneration("),
            "registers_changed": self.MODERN.replace(".registers 24", ".registers 25"),
            "path_register_changed": self.MODERN.replace("iget-object v8, v1", "iget-object v9, v1"),
            "classloader_register_changed": self.MODERN.replace("move-result-object v15", "move-result-object v14"),
            "identity_branch_changed": self.MODERN.replace("if-eq v0, v11, :cond_af", "if-eq v0, v11, :cond_aa"),
            "entry_anchor_changed": self.MODERN.replace("    :cond_af\n", "    :cond_aa\n"),
        }
        for name, value in cases.items():
            with self.subTest(anchor=name):
                self.restore()
                self.modern.write_text(value)
                with self.assertRaises(presets.PresetError):
                    presets.inject_loader_smali(self.root)


class PairedEditionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.inputs = Inputs(self.root)
        self.dist = self.root / "dist"
        self.dist.mkdir()
        self.profile = self.inputs.profile
        self.loaders = {"blank": b"original-loader", "preset": b"loader-with-preset-code"}
        self.names = {edition: "QQ-test-" + edition + ".apk" for edition in self.loaders}
        self.certificates = {name: "a" * 64 for name in self.names.values()}
        self.package_name = self.profile["host_package"]
        self.pair = {"qq_version": self.profile["reviewed_qq_version"], "qq_kind": "latest",
                     "input_path": str(self.inputs.qq), "input_sha256": digest(self.inputs.qq.read_bytes()),
                     "variants": self.names}
        self.loader_info = {"profile_id": self.profile["profile_id"],
                            **{edition + "_loader_sha256": digest(value) for edition, value in self.loaders.items()}}
        self.documents = {
            "variants-build.json": {"profile_id": self.profile["profile_id"], "pairs": [self.pair]},
            "preset-loader.json": self.loader_info,
            "preset-effective.json": self.profile,
            "modules.json": self.inputs.manifest,
            "sources.json": self.inputs.sources,
            "versions.json": {"unit_test": True},
        }
        self.save_documents()
        (self.root / "release-notes.md").write_text("# Unit fixture\n")
        for edition in self.names:
            self.write_edition(edition)

    def save_documents(self):
        for name, value in self.documents.items():
            (self.root / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def write_edition(self, edition, *, loader=None, origin=None, changed_module=False):
        with zipfile.ZipFile(self.dist / self.names[edition], "w") as archive:
            archive.writestr("AndroidManifest.xml", b"unit-only")
            archive.writestr("classes.dex", b"unit-only")
            archive.writestr(presets.LOADER_ENTRY, self.loaders[edition] if loader is None else loader)
            archive.writestr(presets.ORIGIN_ENTRY, self.inputs.qq.read_bytes() if origin is None else origin)
            for index, module in enumerate(self.inputs.manifest["modules"]):
                contents = Path(module["path"]).read_bytes()
                if changed_module and index == 0:
                    contents = tiny_apk(b"modified-plugin")
                archive.writestr("assets/lspatch/modules/" + module["package"] + ".apk", contents)

    def verify(self):
        fake_modules = ModuleType("ci_modules")
        fake_modules.apk_package = lambda path: self.package_name
        with working_directory(self.root), \
                mock.patch.dict(sys.modules, {"ci_modules": fake_modules}), \
                mock.patch.object(presets, "find_apksigner", return_value=Path("unused-apksigner")), \
                mock.patch.object(presets, "signing_digest", side_effect=lambda path, _: self.certificates[path.name]):
            presets.verify(SimpleNamespace(dist="dist"))

    def assert_rejected(self):
        with self.assertRaises(presets.PresetError):
            self.verify()
        self.assertFalse((self.root / "variants.json").exists())
        self.assertFalse((self.root / "SHA256SUMS").exists())

    def test_matching_pair_produces_two_variant_records_and_checksums(self):
        self.verify()
        result = json.loads((self.root / "variants.json").read_text())
        self.assertEqual({v["edition"] for v in result["variants"]}, {"blank", "preset"})
        self.assertTrue(all(v["embedded_qq_sha256"] == self.pair["input_sha256"] for v in result["variants"]))
        self.assertFalse(result["device_runtime_verified"])
        sums = (self.root / "SHA256SUMS").read_text()
        for name in self.names.values():
            self.assertIn(name, sums)

    def test_missing_variant_is_not_a_complete_release(self):
        (self.dist / self.names["preset"]).unlink()
        self.assert_rejected()

    def test_extra_apk_is_not_a_complete_release(self):
        (self.dist / "unexpected.apk").write_bytes(tiny_apk(b"extra"))
        self.assert_rejected()

    def test_changed_embedded_module_fails(self):
        self.write_edition("preset", changed_module=True)
        self.assert_rejected()

    def test_wrong_loader_in_one_edition_fails(self):
        self.write_edition("preset", loader=self.loaders["blank"])
        self.assert_rejected()

    def test_identical_loaders_fail_even_if_metadata_claims_both_are_expected(self):
        self.write_edition("preset", loader=self.loaders["blank"])
        self.loader_info["preset_loader_sha256"] = self.loader_info["blank_loader_sha256"]
        self.save_documents()
        self.assert_rejected()

    def test_two_different_signers_fail(self):
        self.certificates[self.names["preset"]] = "b" * 64
        self.assert_rejected()

    def test_wrong_host_package_fails(self):
        self.package_name = "unit.wrong.host"
        self.assert_rejected()

    def test_two_different_qq_bases_fail(self):
        self.write_edition("preset", origin=tiny_apk(b"different-QQ"))
        self.assert_rejected()

    def test_same_wrong_qq_in_both_variants_still_fails(self):
        wrong = tiny_apk(b"same-but-wrong-QQ")
        self.write_edition("blank", origin=wrong)
        self.write_edition("preset", origin=wrong)
        self.assert_rejected()

    def test_changed_input_file_after_packaging_fails(self):
        self.inputs.qq.write_bytes(tiny_apk(b"input-file-changed"))
        self.assert_rejected()

    def test_mixed_profile_id_in_pair_metadata_fails(self):
        self.documents["variants-build.json"]["profile_id"] = "another-profile"
        self.save_documents()
        self.assert_rejected()

    def test_mixed_profile_id_in_loader_metadata_fails(self):
        self.loader_info["profile_id"] = "another-profile"
        self.save_documents()
        self.assert_rejected()


class EmbeddedOriginalTests(unittest.TestCase):
    # Real NestedZip output from the reviewed LSPatch.jar, not a hand-written
    # approximation. Its linked entries intentionally overlap the stored origin.
    ORIGINAL = (
        "eJwL8GZmEWEAgS9+ebGrrPhYLrAzMIAwN1AsOSexuDi1WC8ltSK/KDM9My8xRwHIVihIrMzJT0wZFRsVGxUbFRsVGxUbFRtaYgEoFX9o8/5VgkA2CAsDsWNeSlF+ZopvYl5mWmpxiV5Fbg7ckFyoYIA3I5MIM+7GAww0MDKgNiXQNaJbjtD4kx2rUwK8WdlACpiAsApIW3OAeACJyD6H"
    )
    NESTED = (
        "eJwL8GZm4WIAga9+ebGFj2ayneZgYABhSaBYYnFxakmxfk5xQWJJcoZ+flFmemaeXmJBdgBQmwhY2xegtlVWfCwX2BkYQJgbKJacA9JYrJeSWgHRkpijAGQrFCRW5uQnpoyKjYqNio2KjYqNio2KDS0x1Io/tHn/KkEgG4SFgdgxL6UoPzPFNzEvMy21uESvIjcHbkguVDDAm5FJhBl34wEGGhgZUJsS6BrRLUdo/MmO1SkB3qxsIAVMQFgFpK05QDzijTXgwGEsI5MEA+5GFCrA06QiNlzM0cMF4i1mIDwI8h4niAcAY4x4ow=="
    )

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.apk = Path(self.temporary.name) / "nested.apk"
        self.original = zlib.decompress(base64.b64decode(self.ORIGINAL))
        self.nested = zlib.decompress(base64.b64decode(self.NESTED))
        self.apk.write_bytes(self.nested)

    def test_real_linked_origin_hash_matches_original_despite_zipfile_overlap_rejection(self):
        self.assertEqual(digest(self.original), "a3834a33ca78ccf2df63859c02d129a4e59da2d432f89c58b05f11a6ce19bff2")
        with zipfile.ZipFile(self.apk) as archive:
            with self.assertRaises(zipfile.BadZipFile):
                archive.read(presets.ORIGIN_ENTRY)
        self.assertEqual(presets.embedded_original_sha256(self.apk, len(self.original)), digest(self.original))

    def test_wrong_expected_size_is_rejected(self):
        for size in (len(self.original) - 1, len(self.original) + 1):
            with self.subTest(size=size), self.assertRaises(presets.PresetError):
                presets.embedded_original_sha256(self.apk, size)

    def test_damaged_local_header_is_rejected(self):
        with zipfile.ZipFile(self.apk) as archive:
            offset = archive.getinfo(presets.ORIGIN_ENTRY).header_offset
        cases = {}
        damaged = bytearray(self.nested)
        damaged[offset] ^= 1
        cases["signature"] = damaged
        damaged = bytearray(self.nested)
        struct.pack_into("<H", damaged, offset + 8, zipfile.ZIP_DEFLATED)
        cases["method"] = damaged
        damaged = bytearray(self.nested)
        damaged[offset + 30] ^= 1
        cases["name"] = damaged
        for name, contents in cases.items():
            with self.subTest(header=name):
                self.apk.write_bytes(contents)
                with self.assertRaises(presets.PresetError):
                    presets.embedded_original_sha256(self.apk, len(self.original))

    def test_changed_stored_data_fails_crc_validation(self):
        with zipfile.ZipFile(self.apk) as archive:
            offset = archive.getinfo(presets.ORIGIN_ENTRY).header_offset
        name_length, extra_length = struct.unpack_from("<HH", self.nested, offset + 26)
        data_offset = offset + 30 + name_length + extra_length
        damaged = bytearray(self.nested)
        damaged[data_offset + 100] ^= 1
        self.apk.write_bytes(damaged)
        with self.assertRaises(presets.PresetError):
            presets.embedded_original_sha256(self.apk, len(self.original))

    def test_duplicate_origin_is_rejected(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(self.apk, "w") as archive:
                archive.writestr(presets.ORIGIN_ENTRY, self.original)
                archive.writestr(presets.ORIGIN_ENTRY, self.original)
        with self.assertRaises(presets.PresetError):
            presets.embedded_original_sha256(self.apk, len(self.original))

    def test_compressed_origin_is_rejected(self):
        with zipfile.ZipFile(self.apk, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(presets.ORIGIN_ENTRY, self.original)
        with self.assertRaises(presets.PresetError):
            presets.embedded_original_sha256(self.apk, len(self.original))


class SigningDigestTests(unittest.TestCase):
    CERTIFICATE = "ab" * 32
    OTHER_CERTIFICATE = "cd" * 32

    def read_report(self, body, *, count="Number of signers: 1\n", returncode=0, stderr=""):
        result = SimpleNamespace(returncode=returncode, stdout="Verifies\n" + count + body,
                                 stderr=stderr)
        with mock.patch.object(presets.subprocess, "run", return_value=result), redirect_stdout(io.StringIO()):
            return presets.signing_digest(Path("sample.apk"), Path("/sdk/apksigner"))

    def test_numbered_signer_digest_is_normalized(self):
        body = "Signer #1 certificate SHA-256 digest: " + self.CERTIFICATE.upper() + "\n"
        self.assertEqual(self.read_report(body), self.CERTIFICATE)

    def test_repeated_same_certificate_across_sdk_ranges_is_allowed(self):
        for development in ("", " (dev release=true)"):
            ranges = (
                "Signer (minSdkVersion=24, maxSdkVersion=32) certificate SHA-256 digest: "
                + self.CERTIFICATE + "\n"
                + "Signer (minSdkVersion=33" + development
                + ", maxSdkVersion=2147483647) certificate SHA-256 digest: "
                + self.CERTIFICATE.upper() + "\n"
            )
            for prefix in ("", "Signer #1 certificate SHA-256 digest: " + self.CERTIFICATE + "\n"):
                with self.subTest(development=development, numbered_section=bool(prefix)):
                    self.assertEqual(self.read_report(prefix + ranges), self.CERTIFICATE)

    def test_different_certificates_across_sdk_ranges_are_rejected(self):
        body = (
            "Signer (minSdkVersion=28, maxSdkVersion=32) certificate SHA-256 digest: "
            + self.CERTIFICATE + "\n"
            "Signer (minSdkVersion=33, maxSdkVersion=2147483647) certificate SHA-256 digest: "
            + self.OTHER_CERTIFICATE + "\n"
        )
        with self.assertRaises(presets.PresetError):
            self.read_report(body)

    def test_extra_signer_number_is_rejected_even_for_the_same_certificate(self):
        second = "Signer #2 certificate SHA-256 digest: " + self.CERTIFICATE + "\n"
        for prefix in ("", "Signer #1 certificate SHA-256 digest: " + self.CERTIFICATE + "\n"):
            with self.subTest(first_signer_present=bool(prefix)), self.assertRaises(presets.PresetError):
                self.read_report(prefix + second)

    def test_source_stamp_certificate_does_not_change_the_apk_identity(self):
        body = (
            "Signer #1 certificate SHA-256 digest: " + self.CERTIFICATE + "\n"
            "Source Stamp Signer certificate SHA-256 digest: " + self.OTHER_CERTIFICATE + "\n"
        )
        self.assertEqual(self.read_report(body), self.CERTIFICATE)

    def test_source_stamp_alone_cannot_replace_an_apk_signer(self):
        body = "Source Stamp Signer certificate SHA-256 digest: " + self.CERTIFICATE + "\n"
        with self.assertRaises(presets.PresetError):
            self.read_report(body)

    def test_signer_count_must_be_present_and_exactly_one(self):
        body = "Signer #1 certificate SHA-256 digest: " + self.CERTIFICATE + "\n"
        for count in ("", "Number of signers: 0\n", "Number of signers: 2\n",
                      "Number of signers: 1\nNumber of signers: 1\n"):
            with self.subTest(count=count), self.assertRaises(presets.PresetError):
                self.read_report(body, count=count)

    def test_failed_verification_is_rejected_despite_a_plausible_digest(self):
        body = "Signer #1 certificate SHA-256 digest: " + self.CERTIFICATE + "\n"
        with self.assertRaises(presets.PresetError):
            self.read_report(body, returncode=1, stderr="DOES NOT VERIFY")


if __name__ == "__main__":
    unittest.main()
