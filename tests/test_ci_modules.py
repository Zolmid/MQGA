import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile


SCRIPT = Path(__file__).resolve().parents[1] / ".github/scripts/ci_modules.py"
spec = importlib.util.spec_from_file_location("ci_modules", SCRIPT)
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


def apk(payload=b"dex-code"):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("AndroidManifest.xml", b"manifest")
        archive.writestr("classes.dex", payload)
    return data.getvalue()


def run(run_id, **changes):
    value = {
        "id": run_id, "status": "completed", "conclusion": "success", "head_branch": "main",
        "head_sha": "a" * 40, "event": "push", "head_repository": {"full_name": "cinit/QAuxiliary"},
        "path": ".github/workflows/push_ci.yml", "created_at": f"2026-11-{run_id:02d}T00:00:00Z",
        "html_url": f"https://github.com/cinit/QAuxiliary/actions/runs/{run_id}",
    }
    value.update(changes)
    return value


def artifact(artifact_id, run_id, name=None, **changes):
    value = {
        "id": artifact_id, "name": name or "QAuxiliary-release-" + "a" * 40 + "-arm64",
        "expired": False, "size_in_bytes": 42,
        "workflow_run": {"id": run_id, "head_sha": "a" * 40},
    }
    value.update(changes)
    return value


class ModuleInputsTests(unittest.TestCase):
    def test_latest_ci_skips_pr_wrong_branch_and_expired(self):
        runs = [run(8, event="pull_request"), run(7, head_branch="other"), run(6), run(5)]
        calls = []

        def api(endpoint):
            calls.append(endpoint)
            if endpoint == "repos/cinit/QAuxiliary":
                return {"default_branch": "main"}
            if "/workflows/" in endpoint:
                return {"workflow_runs": runs}
            if "/runs/6/" in endpoint:
                return {"artifacts": [artifact(60, 6, expired=True)]}
            if "/runs/5/" in endpoint:
                return {"artifacts": [artifact(50, 5)]}
            self.fail("Unexpected API call " + endpoint)

        source = ci.resolve_ci("QAuxiliary", ci.MODULES["QAuxiliary"], api)
        self.assertEqual(source["run_id"], 5)
        self.assertEqual(source["artifact_id"], 50)
        self.assertFalse(any("/runs/8/" in call or "/runs/7/" in call for call in calls))

    def test_ambiguous_artifacts_fail(self):
        with self.assertRaisesRegex(ci.BuildError, "multiple matching"):
            ci.choose_artifact(run(5), [artifact(1, 5), artifact(2, 5)],
                               ci.MODULES["QAuxiliary"]["artifact"])

    def test_exact_qa_and_legacy_ts_flavors(self):
        sha = "a" * 40
        qa = [artifact(1, 5, f"QAuxiliary-release-{sha}-universal"),
              artifact(2, 5, f"QAuxiliary-release-{sha}-arm64-symbol"), artifact(3, 5)]
        self.assertEqual(ci.choose_artifact(run(5), qa, ci.MODULES["QAuxiliary"]["artifact"])["id"], 3)
        ts = [artifact(1, 5, f"TSBattery-modern-release-{sha}"),
              artifact(2, 5, f"TSBattery-legacy-debug-{sha}"),
              artifact(3, 5, f"TSBattery-legacy-release-{sha}")]
        self.assertEqual(ci.choose_artifact(run(5), ts, ci.MODULES["TSBattery"]["artifact"])["id"], 3)
        with tempfile.TemporaryDirectory() as directory:
            archive_path, destination = Path(directory) / "artifact.zip", Path(directory) / "module.apk"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("QAuxv-v1.6.1-arm64.apk", apk())
                archive.writestr("QAuxv-v1.6.1-universal.apk", apk(b"other"))
            ci.extract_apk(archive_path, ci.MODULES["QAuxiliary"]["apk_pattern"], destination)
            self.assertEqual(destination.read_bytes(), apk())

    def test_missing_multiple_empty_or_unsafe_apk_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path, target = Path(directory) / "artifact.zip", Path(directory) / "module.apk"
            cases = [[], [("mod-a.apk", apk()), ("mod-b.apk", apk())],
                     [("mod-a.apk", b"")], [("../mod-a.apk", apk())], [("mod-a.apk", b"not zip")]]
            for entries in cases:
                with self.subTest(entries=[e[0] for e in entries]):
                    with zipfile.ZipFile(path, "w") as archive:
                        for name, data in entries:
                            archive.writestr(name, data)
                    with self.assertRaises(ci.BuildError):
                        ci.extract_apk(path, "mod-*.apk", target)
            for missing in ("AndroidManifest.xml", "classes.dex"):
                data = io.BytesIO()
                with zipfile.ZipFile(data, "w") as archive:
                    for name in {"AndroidManifest.xml", "classes.dex"} - {missing}:
                        archive.writestr(name, b"value")
                with self.assertRaises(ci.BuildError):
                    ci.validate_apk(data.getvalue())

    def test_patched_apk_must_contain_exact_module_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            one, two = apk(b"one"), apk(b"two")
            manifest = root / "modules.json"
            manifest.write_text(json.dumps({"modules": [
                {"name": "one", "sha256": hashlib.sha256(one).hexdigest()},
                {"name": "two", "sha256": hashlib.sha256(two).hexdigest()},
            ]}))
            patched = root / "QQ-lspatched.apk"
            for embedded, succeeds in ([one], False), ([one, apk(b"wrong")], False), ([one, two], True):
                with zipfile.ZipFile(patched, "w") as archive:
                    archive.writestr("AndroidManifest.xml", b"manifest")
                    archive.writestr("classes.dex", b"wrapper")
                    for index, value in enumerate(embedded):
                        archive.writestr(f"assets/lspatch/modules/{index}.apk", value)
                if succeeds:
                    ci.verify_patched(root, manifest)
                else:
                    with self.assertRaisesRegex(ci.BuildError, "embedded module bytes differ"):
                        ci.verify_patched(root, manifest)

    def test_version_identity_retains_sources_and_builder_but_not_temporary_qq_url(self):
        base = {"modules": {"XAutoDaily": {"sha": "a"}}, "config": {"modules": ["XAutoDaily"]},
                "qq": [{"kind": "latest", "version": "9.3.85", "url": "https://qq.test/a"},
                       {"kind": "custom", "version": "9.3.85", "url": "https://qq.test/custom"}]}
        original = ci.version_identity(base, {"builder": "hash"})
        changed = copy.deepcopy(base)
        changed["qq"][0]["url"] = "https://qq.test/b"
        self.assertEqual(original, ci.version_identity(changed, {"builder": "hash"}))
        for change in ("source", "custom_url", "config", "builder"):
            changed = copy.deepcopy(base)
            files = {"builder": "hash"}
            if change == "source":
                changed["modules"]["XAutoDaily"]["sha"] = "b"
            elif change == "custom_url":
                changed["qq"][1]["url"] += "2"
            elif change == "config":
                changed["config"]["modules"].append("ImPad")
            else:
                files["builder"] = "new"
            self.assertNotEqual(original, ci.version_identity(changed, files))


if __name__ == "__main__":
    unittest.main()
