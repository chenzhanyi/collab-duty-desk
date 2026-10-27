#!/usr/bin/env python3
"""T20/T21 回归：F01 结论解析只认明确结论来源。

覆盖 T19 审查 P1-F01：
- 结论待定 + 正文“其余项均通过”不得判 passed；
- 无结论行 + 正文“建议通过”不得判 passed；
- 明确结论仍按 通过/须修正 进入终态；
- 无明确结论时不得回退全文关键词。

T21 补充：
- 明确结论来源内按语义优先级取词（修正 > 待定 > 通过），不按字符位置；
- 围栏代码块（``` / ~~~）中的示例结论不参与判定；
- 空的结论标签跳过并继续扫描后续来源。

所有用例只在临时目录构造 Task，不触碰真实 项目/、协作/ 或 8787。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

import server  # noqa: E402


class T20ReviewVerdictTest(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_dispatch_dir = server.DISPATCH_DIR
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name).resolve()
        self.dispatch_dir = self.root / "调度台"
        self.dispatch_dir.mkdir(parents=True)
        server.DISPATCH_DIR = self.dispatch_dir

        self.project = self.root / "项目" / "T20_结论解析"
        self.docking = self.project / "对接"
        self.docking.mkdir(parents=True)
        self.review_path = self.docking / server.REVIEW_NAME
        self.review_done_path = self.docking / server.REVIEW_DONE_NAME

    def tearDown(self) -> None:
        server.DISPATCH_DIR = self._saved_dispatch_dir
        self._temp.cleanup()

    def make_task(self, text: str, *, review_done: bool = True):
        self.review_path.write_text(text, encoding="utf-8")
        if review_done:
            self.review_done_path.write_text("审核完成\n", encoding="utf-8")
        # 本文件测的是 T20/T21/T22 历史结论解析；固定为 T24 之前的旧文件。
        for path in (self.review_path, self.review_done_path):
            if path.is_file():
                os.utime(path, (0, 0))
        return server.Task(
            task_id="T20",
            title="T20 结论解析回归",
            path=self.project,
            mtime=0.0,
            rounds=0,
            status=server.STATUS_DELIVERED,
            project_dir=self.project,
            project_name=self.project.name,
            review_path=self.review_path,
            review_text=text,
            review_mtime=0.0,
            delivery=None,
            workdir=self.project / "工作区",
            meta_path=self.docking / "task.yaml",
            meta_created_at="",
            registration_status="registered",
            controller_agent="",
            register_log="",
            register_error="",
            registered_at="",
            subtasks=(),
            subtasks_error="",
        )

    def assert_unresolved(self, text: str) -> None:
        task = self.make_task(text)
        self.assertEqual(server._review_verdict(text), "")
        self.assertEqual(server._review_terminal_state(task), (False, ""))
        self.assertNotEqual(server._task_phase(task), server.PHASE_PASSED)
        self.assertNotEqual(server._task_phase(task), server.PHASE_REVISE)

    # ------------------------------------------------------------------
    # 反向回归：不得回退正文关键词
    # ------------------------------------------------------------------

    def test_t20_pending_conclusion_with_passing_body_is_not_passed(self) -> None:
        self.assert_unresolved(
            "# 审查\n\n"
            "## 结论\n\n"
            "结论：**待定**\n\n"
            "## 逐项核对\n\n"
            "- 其余项均通过。\n"
        )

    def test_t20_pending_conclusion_beats_passing_words_on_same_line(self) -> None:
        self.assert_unresolved(
            "# 审查\n\n## 结论\n\n结论：**待定** + 正文“其余项均通过”\n"
        )

    def test_t20_no_conclusion_with_passing_body_is_not_passed(self) -> None:
        self.assert_unresolved(
            "# 审查\n\n"
            "逐项核对完成，未发现阻塞；建议通过。\n"
            "其余项均通过。\n"
        )

    def test_t20_body_keywords_without_conclusion_source_are_ignored(self) -> None:
        for text in (
            "# 审查\n\n逐项讨论：有的通过，有的不通过，尚无结论。\n",
            "# 审查\n\n结论尚待补充，正文先写“建议通过”。\n",
        ):
            with self.subTest(text=text):
                self.assertEqual(server._review_verdict(text), "")

    # ------------------------------------------------------------------
    # 正向兼容：明确结论仍进终态
    # ------------------------------------------------------------------

    def test_t20_bold_conclusion_passed_legacy_format(self) -> None:
        text = "# 审查\n\n- **结论**：**通过**\n\n## 逐项证据\n\n- 已核对\n"
        task = self.make_task(text)
        self.assertEqual(server._review_verdict(text), "passed")
        self.assertEqual(server._review_terminal_state(task), (True, "passed"))

    def test_t20_conclusion_section_passed(self) -> None:
        text = (
            "# 审查\n\n## 三、结论\n\n"
            "功能、边界、证据均满足，**审查通过**。\n"
        )
        task = self.make_task(text)
        self.assertEqual(server._review_verdict(text), "passed")
        self.assertEqual(server._review_terminal_state(task), (True, "passed"))

    def test_t20_inline_header_conclusion_still_parses(self) -> None:
        # 兼容 T01/T03/T04 历史报告：**审查时间**：… **结论**：**通过** ✓
        text = "# 审查\n\n**审查时间**：2026-10-26　**结论**：**通过** ✓\n"
        self.assertEqual(server._review_verdict(text), "passed")

    def test_t20_review_conclusion_label_still_parses(self) -> None:
        # 兼容 T17 历史报告：- **审查结论**：**通过**
        text = "# 审查\n\n- **审查结论**：**通过**\n\n## 一、总体结论\n"
        self.assertEqual(server._review_verdict(text), "passed")

    def test_t20_conclusion_revise(self) -> None:
        text = "# 审查\n\n## 结论\n\n结论：须修正\n"
        task = self.make_task(text)
        self.assertEqual(server._review_verdict(text), "revise")
        self.assertEqual(server._review_terminal_state(task), (True, "revise"))
        self.assertEqual(server._task_phase(task), server.PHASE_REVISE)

    def test_t20_negative_phrases_map_to_revise(self) -> None:
        for value in ("不通过", "未通过", "须修正", "revise", "Not passed"):
            text = "## 结论\n\n结论：%s\n" % value
            with self.subTest(value=value):
                self.assertEqual(server._review_verdict(text), "revise")

    def test_t20_passed_english_token_maps_to_passed(self) -> None:
        text = "## 结论\n\n结论：passed\n"
        self.assertEqual(server._review_verdict(text), "passed")

    # ------------------------------------------------------------------
    # T21：语义优先级（语序）—— 修正 > 待定 > 通过
    # ------------------------------------------------------------------

    def test_t21_pending_after_passed_phrase_is_not_passed(self) -> None:
        self.assert_unresolved(
            "## 结论\n\n结论：通过与否待定\n"
        )

    def test_t21_pending_after_suggested_passed_is_not_passed(self) -> None:
        self.assert_unresolved(
            "## 结论\n\n建议通过，最终结论待定。\n"
        )

    def test_t21_pending_variants_map_to_empty(self) -> None:
        for value in ("待定", "不确定", "待议"):
            with self.subTest(value=value):
                self.assert_unresolved("## 结论\n\n结论：%s\n" % value)

    def test_t21_revise_after_passed_phrase_is_revise(self) -> None:
        text = "## 结论\n\n通过，但 B 项须修正。\n"
        task = self.make_task(text)
        self.assertEqual(server._review_verdict(text), "revise")
        self.assertEqual(server._review_terminal_state(task), (True, "revise"))
        self.assertEqual(server._task_phase(task), server.PHASE_REVISE)

    def test_t21_mixed_negation_prefers_revise(self) -> None:
        for text in (
            "## 结论\n\n整体通过，但 B 项不通过，验收未通过。\n",
            "## 结论\n\n外观检查通过，功能验证未通过。\n",
            "## 结论\n\n**通过**（附注：T21-2 尚未通过）。\n",
        ):
            with self.subTest(text=text):
                self.assertEqual(server._review_verdict(text), "revise")

    # ------------------------------------------------------------------
    # T21：围栏代码块不参与结论判定
    # ------------------------------------------------------------------

    def test_t21_backtick_fence_example_is_ignored(self) -> None:
        self.assert_unresolved(
            "## 问题\n\n```\n结论：通过\n```\n"
        )

    def test_t21_tilde_fence_example_is_ignored(self) -> None:
        self.assert_unresolved(
            "## 问题\n\n~~~text\n结论：通过\n~~~\n"
        )

    def test_t21_fenced_conclusion_section_is_ignored(self) -> None:
        self.assert_unresolved(
            "## 结论\n\n```text\n结论：通过\n```\n"
        )

    def test_t21_fence_does_not_hide_later_explicit_conclusion(self) -> None:
        text = (
            "## 问题\n\n```\n结论：通过\n```\n\n"
            "## 结论\n\n结论：须修正\n"
        )
        self.assertEqual(server._review_verdict(text), "revise")

    # ------------------------------------------------------------------
    # T21：空结论标签跳过并继续扫描
    # ------------------------------------------------------------------

    def test_t21_empty_conclusion_label_is_skipped(self) -> None:
        text = "**结论**：\n\n## 结论\n\n结论：通过\n"
        task = self.make_task(text)
        self.assertEqual(server._review_verdict(text), "passed")
        self.assertEqual(server._review_terminal_state(task), (True, "passed"))

    def test_t21_empty_inline_conclusion_label_is_skipped(self) -> None:
        text = (
            "# 审查\n\n"
            "**审查时间**：2026-10-26　**结论**：\n\n"
            "## 结论\n\n结论：revise\n"
        )
        self.assertEqual(server._review_verdict(text), "revise")

    def test_t21_empty_conclusion_heading_is_skipped(self) -> None:
        text = "## 结论\n\n## 结论\n\n结论：通过\n"
        self.assertEqual(server._review_verdict(text), "passed")

    # ------------------------------------------------------------------
    # T22：显式结论行优先 + 标题来源收窄（T20 复审 P1-A）
    # ------------------------------------------------------------------

    def test_t22_document_title_with_conclusion_word_does_not_shadow_explicit_line(
        self,
    ) -> None:
        # 任务单样例①：H1 含“结论解析”，显式通过不得被标题段落覆盖。
        text = (
            "# 报告 · 结论解析\n\n"
            "- **结论**：**通过**\n\n"
            "## 证据\n\n"
            "- 有一项须修正\n"
        )
        task = self.make_task(text)
        self.assertEqual(server._review_verdict(text), "passed")
        self.assertEqual(server._review_terminal_state(task), (True, "passed"))

    def test_t22_h1_conclusion_analysis_title_does_not_use_body_keywords(self) -> None:
        # 任务单样例②：H1 不是结论来源，正文“建议通过”不得回退。
        self.assert_unresolved("# 结论解析\n\n逐项完成，建议通过。\n")

    def test_t22_narrative_conclusion_heading_does_not_use_body_keywords(self) -> None:
        # 任务单样例③：叙述性 H2“结论与建议”不是结论来源。
        self.assert_unresolved("## 结论与建议\n\n建议通过。\n")

    def test_t22_numbered_conclusion_heading_still_passes(self) -> None:
        # 任务单样例④：标题序号规范化后仍识别为“结论”。
        text = "## 三、结论\n\n功能、边界、证据均满足，**审查通过**。\n"
        task = self.make_task(text)
        self.assertEqual(server._review_verdict(text), "passed")
        self.assertEqual(server._review_terminal_state(task), (True, "passed"))

    def test_t22_explicit_line_after_heading_wins(self) -> None:
        # 标题在前、显式结论行在后：显式行仍优先，标题段落不得覆盖。
        text = (
            "## 结论解析\n\n"
            "本段正文提到须修正。\n\n"
            "- **结论**：**通过**\n"
        )
        self.assertEqual(server._review_verdict(text), "passed")

    def test_t22_explicit_line_inside_conclusion_heading_wins(self) -> None:
        # 即使标题本身是“结论”，显式行也先于标题段落取词。
        text = "## 结论\n\n正文提到须修正。\n\n**结论**：**通过**\n"
        self.assertEqual(server._review_verdict(text), "passed")

    def test_t22_h1_conclusion_heading_is_ignored(self) -> None:
        self.assert_unresolved("# 结论\n\n审查通过\n")

    def test_t22_heading_ordinal_and_suffix_normalization(self) -> None:
        # 等于“结论”或以“结论”结尾才接受；序号前缀先被剔除。
        accepted = (
            "## 结论",
            "## 审查结论",
            "## 总体结论",
            "## 三、结论",
            "## 3. 结论",
            "## （一）结论",
            "## 第 3 节 结论",
            "## 5. 审查结论",
            "## **结论**",
        )
        for heading in accepted:
            with self.subTest(heading=heading):
                text = heading + "\n\n审查通过\n"
                self.assertEqual(server._review_verdict(text), "passed")

        rejected = (
            "## 结论解析",
            "## 结论与建议",
            "## 结论部分",
            "## 结论摘要",
            "## 3. 结论解析",
        )
        for heading in rejected:
            with self.subTest(heading=heading):
                self.assert_unresolved(heading + "\n\n建议通过。\n")

    def test_t22_pending_explicit_line_still_stops_heading_fallback(self) -> None:
        # 第一个非空显式来源为待定：返回空，后续标题段落不得覆盖。
        text = "结论：待定\n\n## 结论\n\n审查通过\n"
        self.assert_unresolved(text)

    # ------------------------------------------------------------------
    # T22：P3-02 否定短语预处理（非阻塞项）
    # ------------------------------------------------------------------

    def test_t22_negated_revise_phrases_do_not_force_revise(self) -> None:
        for source in (
            "结论：通过，无须修正。",
            "结论：全部通过，无不通过项。",
            "结论：审查通过，未发现任何须修正项。",
        ):
            with self.subTest(source=source):
                self.assertEqual(
                    server._review_verdict("## 结论\n\n" + source + "\n"),
                    "passed",
                )

    def test_t22_bare_revise_terms_still_map_to_revise(self) -> None:
        # 否定预处理只剔除带否定前缀的短语，裸修正词仍优先。
        for source in (
            "结论：须修正",
            "结论：不通过",
            "结论：未通过",
            "结论：无须修正，但接口须修正",
        ):
            with self.subTest(source=source):
                self.assertEqual(
                    server._review_verdict("## 结论\n\n" + source + "\n"),
                    "revise",
                )

    # ------------------------------------------------------------------
    # 终态闸门：结论非空 + 审核完成.md 同时存在
    # ------------------------------------------------------------------

    def test_t20_terminal_requires_review_done(self) -> None:
        text = "## 结论\n\n结论：**通过**\n"
        task = self.make_task(text, review_done=False)
        self.assertEqual(server._review_verdict(text), "passed")
        self.assertEqual(server._review_terminal_state(task), (False, ""))
        self.assertNotEqual(server._task_phase(task), server.PHASE_PASSED)

    def test_t20_pending_with_review_done_is_still_not_terminal(self) -> None:
        text = "## 结论\n\n结论：**待定**\n\n其余项均通过\n"
        self.assertEqual(server._review_terminal_state(self.make_task(text)), (False, ""))

    def test_t20_no_conclusion_with_review_done_is_still_not_terminal(self) -> None:
        text = "# 审查\n\n建议通过，正文提到通过，但没有明确结论。\n"
        self.assertEqual(server._review_terminal_state(self.make_task(text)), (False, ""))


if __name__ == "__main__":
    unittest.main()
