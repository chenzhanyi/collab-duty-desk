"""Project/source workspaces use temporary repositories and fake agents only."""
import json
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runner
import server
import workspace
import test_advance as fixtures


SOURCE_AGENT = r'''
if "项目交付清单（绝对路径）：" in prompt and "执行子任务" in prompt:
    import json
    task_id = re.search(r"执行子任务 (T\d+-\d+)", prompt).group(1)
    output = Path(re.search(r"输出目录（绝对路径）：([^\n]+)", prompt).group(1))
    output.mkdir(parents=True, exist_ok=True)
    source = Path.cwd() / "app.py"
    with source.open("a") as stream:
        stream.write("# changed by " + task_id + "\n")
    (output / "changes.json").write_text(json.dumps({"files": ["app.py"], "checks": ["fake verification: passed"]}))
    attempt = re.search(r"attempt:\s*(\d+)", prompt)
    (output / "DONE.md").write_text("# DONE\n## 做了什么\n修改源码\n## 怎么验\n假 Agent 验证\n## 结果\n通过\n" + ("attempt: " + attempt.group(1) if attempt else ""))
    record("source cwd " + str(Path.cwd()))
    raise SystemExit(0)
'''


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AdvanceTest()
        self.fixture.setUp()
        os.environ["T14_AGENT_SLEEP"] = "0.01"
        script = self.fixture.script
        script.write_text(script.read_text().replace("fd = None", SOURCE_AGENT + "\nfd = None"))

    def tearDown(self):
        self.fixture.tearDown()

    def make_task(self, work_mode="project", statuses=None):
        project = self.fixture.make_task("T90", ["pending", "pending"] if statuses is None else statuses)
        meta_path = project / "对接/task.yaml"
        meta = server._read_task_meta(project, "T90")
        meta.update(work_mode=work_mode, workdir=str(self.fixture.root if work_mode == "self" else self.fixture.external_workdir))
        server._write_task_meta(meta_path, meta)
        return server._find_task("T90")

    def test_source_cycle_modifies_shared_project_and_keeps_records_in_desk(self):
        task = self.make_task()
        (task.workdir / "app.py").write_text("# original\n")
        server._start_advance(task)
        result = self.fixture.wait_advance("T90")
        self.assertEqual(result["state"], "completed", result)
        self.assertEqual(result["conclusion"], "passed", result)
        self.assertIn("T90-1", (task.workdir / "app.py").read_text())
        self.assertIn("T90-2", (task.workdir / "app.py").read_text())
        self.assertFalse((task.workdir / "子任务").exists())
        latest = server._find_task("T90")
        for subtask in latest.subtasks:
            output = server._subtask_output_dir(latest, subtask)
            self.assertTrue(output.is_relative_to(task.workspace_dir))
            self.assertTrue((output / "changes.json").is_file())
        self.assertTrue(all(check["passed"] for check in server._gate_g2(latest)))
        self.assertFalse(server.RUNNER.lock_info().get("active"))

    def test_self_maintenance_runs_on_desk_source_and_preserves_private_config(self):
        task = self.make_task("self")
        config = self.fixture.roles / "controller.yaml"
        before = config.read_bytes()
        server._start_advance(task)
        result = self.fixture.wait_advance("T90")
        self.assertEqual(result["state"], "completed", result)
        self.assertEqual(result["conclusion"], "passed", result)
        self.assertEqual(config.read_bytes(), before)
        self.assertIn("T90-2", (self.fixture.root / "app.py").read_text())
        self.assertFalse((self.fixture.root / "子任务").exists())

    def test_root_requires_explicit_self_mode_in_server_and_runner(self):
        for work_mode in ["isolated", "project"]:
            with self.assertRaises(server.FormError):
                server._validate_project_folder(str(self.fixture.root), work_mode)
            with self.assertRaises(runner.RunnerConfigError):
                runner._prepare_workdir(self.fixture.root, self.fixture.root, work_mode)
        self.assertEqual(server._validate_project_folder(str(self.fixture.root), "self"), self.fixture.root)
        self.assertEqual(runner._prepare_workdir(self.fixture.root, self.fixture.root, "self"), self.fixture.root)
        for path in [self.fixture.roles, self.fixture.external_workdir, self.fixture.dispatch_dir]:
            with self.assertRaises(server.FormError):
                server._validate_project_folder(str(path), "self")
        with self.assertRaises(server.FormError):
            server._validate_project_folder(str(self.fixture.root), "invalid")

    def test_manifest_rejects_escape_symlink_runtime_and_personal_config(self):
        task = self.make_task("self")
        (self.fixture.root / "app.py").write_text("# code\n")
        for path in ["../outside.py", "协作/roles/controller.yaml", str(self.fixture.root / "app.py")]:
            self.assertTrue(workspace.source_file_problem(path, task.workdir, self.fixture.root), path)
        (self.fixture.root / "linked.py").symlink_to(self.fixture.root / "app.py")
        self.assertTrue(workspace.source_file_problem("linked.py", task.workdir, self.fixture.root))
        self.assertEqual(workspace.source_file_problem("app.py", task.workdir, self.fixture.root), "")
        (self.fixture.dispatch_dir / "state.py").write_text("# source\n")
        self.assertEqual(workspace.source_file_problem("协作/调度台/state.py", task.workdir, self.fixture.root), "")

    def test_project_g2_requires_manifest_and_validation_records(self):
        task = self.make_task(statuses=["delivered"])
        output = server._subtask_output_dir(task, task.subtasks[0])
        output.mkdir(parents=True)
        (output / "DONE.md").write_text("## 做了什么\n## 怎么验\n## 结果\n")
        self.assertFalse(server._gate_g2(task)[0]["passed"])
        (output / "changes.json").write_text('{"files": [], "checks": []}')
        self.assertFalse(server._gate_g2(task)[0]["passed"])
        (output / "changes.json").write_text('{"files": [], "checks": ["git status: clean"]}')
        self.assertTrue(server._gate_g2(task)[0]["passed"])

    def test_existing_source_is_not_delivery_for_task_without_subtasks(self):
        task = self.make_task(statuses=[])
        (task.workdir / "app.py").write_text("existing code")
        self.assertFalse(server._delivery_evidence(task)[0])

    def test_legacy_task_keeps_old_output_path(self):
        task = self.make_task("isolated")
        self.assertEqual(server._subtask_output_dir(task, task.subtasks[0]), task.workdir / "子任务/T90-1")

    def test_mode_cannot_change_after_execution(self):
        task = self.make_task("isolated", statuses=["delivered"])
        with self.assertRaises(server.FormError):
            server._update_task_workdir(task, str(self.fixture.root), "self")
        self.assertEqual(server._find_task("T90").work_mode, "isolated")

    def test_delivery_symlink_cannot_redirect_records_to_source(self):
        task = self.make_task()
        task.workspace_dir.mkdir(exist_ok=True)
        (task.workspace_dir / "交付").symlink_to(task.workdir)
        with self.assertRaises(server.ConfigError):
            server._subtask_output_dir(task, task.subtasks[0])

    def test_folder_validation_accepts_root_only_in_self_mode(self):
        self.fixture.start_server()
        from urllib.parse import urlencode
        for chosen, expected in [("self", 200), ("project", 400), ("isolated", 400)]:
            status, body, _ = self.fixture.request("POST", "/api/validate-folder", body=urlencode({"workdir": str(self.fixture.root), "work_mode": chosen}).encode())
            self.assertEqual(status, expected, body)

    def test_publish_source_and_self_modes_preserves_mode_through_registration(self):
        self.fixture.start_server()
        from urllib.parse import urlencode
        for chosen, folder in [("project", self.fixture.external_workdir), ("self", self.fixture.root)]:
            status, body, _ = self.fixture.request("POST", "/publish", accept="application/json", body=urlencode({
                "title": "共享源码测试", "idea": "修改 app.py 并验证。", "workdir": str(folder), "work_mode": chosen, "auto_advance": "1"
            }).encode())
            self.assertEqual(status, 202, body)
            task_id = json.loads(body)["id"]
            meta = self.fixture.wait_registered(task_id)
            self.assertEqual(meta["work_mode"], chosen)
            result = self.fixture.wait_advance(task_id)
            self.assertEqual(result["state"], "completed", result)
            self.assertEqual(result["conclusion"], "passed", result)

    def test_project_mode_cannot_silently_fall_back_to_task_archive(self):
        with self.assertRaises(server.FormError):
            server._validate_workdir("", default=self.fixture.projects / "T90_test/工作区", work_mode="project")
        self.assertEqual(server._validate_workdir("", default=self.fixture.projects, work_mode="self"), self.fixture.root)

    def test_source_revision_keeps_code_and_uses_source_scope(self):
        task = self.make_task()
        os.environ["T14_MODE"] = "revise"
        server._start_advance(task)
        result = self.fixture.wait_advance("T90")
        self.assertEqual(result["conclusion"], "revise", result)
        latest = server._find_task("T90")
        original = (task.workdir / "app.py").read_text()
        server._archive_subtask_outputs(latest, [latest.subtasks[0].id], 1)
        self.assertEqual((task.workdir / "app.py").read_text(), original)
        prompt = server._dispatch_prompt(server._load_roles()[0][1], latest.subtasks[0], latest.workdir,
            task=latest, output_dir=server._subtask_output_dir(latest, latest.subtasks[0]), revise_context={"round": 1})
        self.assertNotIn("只写本子任务输出目录", prompt)
        self.assertIn("项目源码", prompt)

    def test_gitignore_excludes_runtime_but_keeps_watcher_source(self):
        import subprocess
        import tempfile
        ignore = Path(__file__).resolve().parents[1] / ".gitignore"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".gitignore").write_text(ignore.read_text())
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            paths = ["项目/T01/对接/idea.md", "协作/调度台/menubar.control-token", "协作/调度台/snapshots/a/data", "协作/agents/codex-sol.yaml", "协作/调度台/state.py", "console/workspace.py"]
            result = subprocess.run(["git", "-c", "core.quotepath=false", "-C", str(root), "check-ignore", "--stdin"], input="\n".join(paths), capture_output=True, text=True)
            self.assertEqual(set(result.stdout.splitlines()), set(paths[:4]))


if __name__ == "__main__":
    unittest.main()
