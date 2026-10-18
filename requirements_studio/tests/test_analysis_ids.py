"""Regression coverage for answered question IDs in requirement deletions."""
import copy
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.domain import AnalysisResult, analysis_schema, apply_analysis, empty_document
from backend.store import Store


def question(qid="phone_platform"):
    return {"id": qid, "question": "手机端需要兼容哪些平台？", "reason": "明确兼容范围。", "options": ["Android", "iOS"], "requirement_ids": ["REQ-003"]}


def response(**fields):
    return AnalysisResult.model_validate({"reply": "已补充 Android 平台要求。", "title": "蓝牙温度记录器", "summary": "室内蓝牙温度记录器。", "updates": [], "remove_ids": [], "questions": [], "flow": [], "changes": ["补充手机平台。"], **fields})


class AnalysisIdTests(unittest.TestCase):
    def setUp(self):
        folder = ROOT / ".runtime" / "tests"
        folder.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=folder)
        self.store = Store(Path(self.temp.name) / "studio.sqlite3")
        self.project = self.store.create_project(example=True)
        self.doc = self.project["document"]
        self.doc["questions"] = [question()]
        self.messages = self.project["messages"] + [{"id": "answer", "role": "user", "content": "关于「手机端需要兼容哪些平台？」：Android"}]
        self.update = {k: self.doc["requirements"][2][k] for k in ["id", "category", "name", "value", "unit", "priority", "state", "source_message_id", "source_quote"]}
        self.update.update(value="Android 手机通过蓝牙读取历史数据", source_message_id="answer", source_quote="Android")

    def tearDown(self):
        self.temp.cleanup()

    def test_answered_question_in_remove_ids_saves_answer_without_losing_requirements(self):
        with self.store.connect() as db:
            self.store._save(db, self.project["id"], self.doc, 2)
        failed, _ = self.store.begin_run(self.project["id"], self.messages[-1]["content"], "first")
        self.store.finish(failed["id"], "failed", "需求删除项无效，请重试。")
        retry, _ = self.store.begin_run(self.project["id"], "", "retry", retry=True)
        project = self.store.get_project(self.project["id"])
        update = {**self.update, "source_message_id": retry["message_id"]}
        result = response(updates=[update], remove_ids=["phone_platform"], questions=[{**question("temperature_range"), "question": "需要覆盖怎样的室内温度范围？"}])
        before = copy.deepcopy(project["document"])
        after = apply_analysis(before, result, project["messages"], retry["message_id"])
        self.store.commit_analysis(retry["id"], after, result.reply)
        saved = self.store.get_project(self.project["id"])
        self.assertEqual(saved["latest_run"]["status"], "completed")
        self.assertEqual(saved["document"]["version"], 3)
        self.assertEqual(len(saved["messages"]), 4)  # No duplicate user answer on retry.
        self.assertEqual([r["id"] for r in saved["document"]["requirements"]], [r["id"] for r in before["requirements"]])
        row = next(r for r in saved["document"]["requirements"] if r["id"] == "REQ-003")
        self.assertIn("Android", row["value"])
        self.assertEqual(row["state"], "stated")
        self.assertEqual(row["source_message_id"], retry["message_id"])
        self.assertEqual([q["id"] for q in saved["document"]["questions"]], ["temperature_range"])
        self.assertIn("Android", saved["markdown"])
        for old in before["requirements"]:
            if old["id"] != "REQ-003":
                self.assertEqual(old, next(r for r in saved["document"]["requirements"] if r["id"] == old["id"]))
        self.assertEqual(before, project["document"])

    def test_unknown_deletions_are_rejected_without_mutating_document(self):
        before = copy.deepcopy(self.doc)
        for rid in ("REQ-999", "not_a_previous_question"):
            with self.subTest(rid=rid), self.assertRaisesRegex(ValueError, "不是当前需求编号"):
                apply_analysis(self.doc, response(updates=[self.update], remove_ids=[rid]), self.messages, "answer")
        self.assertEqual(self.doc, before)

    def test_question_cannot_be_both_removed_and_retained(self):
        with self.assertRaises(ValueError):
            apply_analysis(self.doc, response(remove_ids=["phone_platform"], questions=[question()]), self.messages, "answer")

    def test_same_requirement_cannot_be_updated_and_deleted(self):
        before = copy.deepcopy(self.doc)
        with self.assertRaisesRegex(ValueError, "同时修改和删除"):
            apply_analysis(self.doc, response(updates=[self.update], remove_ids=["REQ-003"]), self.messages, "answer")
        self.assertEqual(self.doc, before)

    def test_repeated_real_deletion_is_idempotent_and_removes_dangling_references(self):
        result = response(remove_ids=["REQ-003", "REQ-003"], questions=[question("remaining")], flow=[{"label": "读取记录", "requirement_ids": ["REQ-003", "REQ-004"]}])
        after = apply_analysis(self.doc, result, self.messages, "answer")
        self.assertEqual(len(after["requirements"]), len(self.doc["requirements"]) - 1)
        self.assertNotIn("REQ-003", [r["id"] for r in after["requirements"]])
        self.assertEqual(after["changed_ids"], ["REQ-003"])
        self.assertEqual(after["questions"][0]["requirement_ids"], [])
        self.assertEqual(after["flow"][0]["requirement_ids"], ["REQ-004"])

    def test_real_requirement_id_takes_precedence_over_colliding_question_id(self):
        self.doc["questions"][0]["id"] = "REQ-003"
        after = apply_analysis(self.doc, response(remove_ids=["REQ-003"]), self.messages, "answer")
        self.assertNotIn("REQ-003", [r["id"] for r in after["requirements"]])

    def test_output_schema_only_accepts_current_requirement_deletions(self):
        before = copy.deepcopy(self.doc)
        schema = analysis_schema(self.doc)
        allowed = schema["properties"]["remove_ids"]["items"]["enum"]
        self.assertEqual(set(allowed), {r["id"] for r in self.doc["requirements"]})
        self.assertNotIn("phone_platform", allowed)
        other = empty_document()
        other["requirements"] = [{"id": "REQ-987"}]
        self.assertEqual(analysis_schema(other)["properties"]["remove_ids"]["items"]["enum"], ["REQ-987"])
        self.assertEqual(schema["properties"]["remove_ids"]["items"]["enum"], allowed)
        self.assertEqual(self.doc, before)

    def test_output_schema_disallows_any_deletion_from_empty_document(self):
        removals = analysis_schema(empty_document())["properties"]["remove_ids"]
        self.assertEqual(removals["maxItems"], 0)
        self.assertNotIn("enum", removals["items"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
