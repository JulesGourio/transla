"""Guards for the deployment configuration: bundle, per-target settings and deploy script must agree."""
import glob
import json
import os
import re
import subprocess
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET_ENV = os.path.join(ROOT, "utils", "deploy", "target_env.json")
REQUIRED_TAGS = {"activity-type", "job-purpose", "project-name"}


def _load(path):
    with open(path, encoding="utf-8-sig") as f:
        return yaml.safe_load(f)


def _bundle_files():
    return [os.path.join(ROOT, "databricks.yml")] + sorted(glob.glob(os.path.join(ROOT, "resources", "*.yml")))


def _jobs():
    """Yields (file, job key, job definition) for every complete job; a target block that only overrides a job (no tasks) is skipped."""
    for path in _bundle_files():
        doc = _load(path) or {}
        declared = list(((doc.get("resources") or {}).get("jobs") or {}).items())
        for target in (doc.get("targets") or {}).values():
            declared += list(((target.get("resources") or {}).get("jobs") or {}).items())
        for key, job in declared:
            if "tasks" in job:
                yield path, key, job


def _targets():
    return set(_load(os.path.join(ROOT, "databricks.yml"))["targets"])


def _target_env():
    with open(TARGET_ENV, encoding="utf-8") as f:
        return {k: v for k, v in json.load(f).items() if not k.startswith("_")}


def test_deploy_script_targets_exist_in_the_bundle():
    script = open(os.path.join(ROOT, "utils", "deploy", "deploy_latlang.ps1"), encoding="utf-8").read()
    mapped = set(re.findall(r"Target\s*=\s*'([^']+)'", script))
    assert mapped, "no target found in deploy_latlang.ps1"
    assert mapped <= _targets()


def test_every_deploy_environment_has_target_env_settings():
    script = open(os.path.join(ROOT, "utils", "deploy", "deploy_latlang.ps1"), encoding="utf-8").read()
    envs = set(re.findall(r"^\s+(\w+)\s*=\s*@\{\s*Target", script, re.M))
    assert envs <= set(_target_env())


def test_every_target_sets_the_same_keys_and_its_own_catalog():
    envs = _target_env()
    key_sets = {frozenset(v) for v in envs.values()}
    assert len(key_sets) == 1, "targets of target_env.json do not set the same keys"
    for name, values in envs.items():
        assert values["LAKEBASE_DATABASE"] == "latlang"
        for key in ("TRANSLATE_VOLUME_PATH", "SOFFICE_ARCHIVE_VOLUME_PATH"):
            assert values[key].startswith(f"/Volumes/{name}_proj/latlang/"), (name, key)


def test_app_yaml_declares_every_overridden_setting():
    declared = {e["name"] for e in _load(os.path.join(ROOT, "app.yaml"))["env"]}
    for values in _target_env().values():
        assert set(values) <= declared


def test_rendered_target_config_is_lf_only(tmp_path):
    out = tmp_path / "target_config.env"
    subprocess.run(
        [sys.executable, os.path.join(ROOT, "utils", "deploy", "render_target_config_env.py"), "dev", str(out)],
        check=True, capture_output=True,
    )
    data = out.read_bytes()
    assert data and b"\r" not in data


def test_every_job_follows_the_job_format():
    for path, key, job in _jobs():
        where = f"{os.path.relpath(path, ROOT)}:{key}"
        assert job.get("queue", {}).get("enabled") is True, where
        assert job.get("email_notifications", {}).get("on_failure"), where
        assert job.get("notification_settings", {}).get("no_alert_for_canceled_runs") is True, where
        assert REQUIRED_TAGS <= set(job.get("tags", {})), where


def test_every_notebook_task_points_to_an_existing_file():
    for path, key, job in _jobs():
        for task in job.get("tasks", []):
            notebook = (task.get("notebook_task") or {}).get("notebook_path")
            if notebook:
                assert os.path.isfile(os.path.normpath(os.path.join(os.path.dirname(path), notebook))), (key, notebook)


def test_shell_scripts_are_kept_lf_by_gitattributes():
    attrs = open(os.path.join(ROOT, ".gitattributes"), encoding="utf-8").read()
    assert re.search(r"^\*\.sh\s+text\s+eol=lf", attrs, re.M)
    with open(os.path.join(ROOT, "start.sh"), "rb") as f:
        assert b"\r" not in f.read()


def test_requirements_are_pinned():
    for line in open(os.path.join(ROOT, "requirements.txt"), encoding="utf-8"):
        line = line.split("#")[0].strip()
        if line:
            assert "==" in line, line
