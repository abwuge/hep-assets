"""Independent trial configuration without model API calls."""

import unittest
from ams_flux_server.trial import argument_parser, task_prompt


class TrialTests(unittest.TestCase):
    def test_model_and_budget_can_be_selected(self):
        args = argument_parser().parse_args([
            "--run", "runs/fixture", "--model", "Sage-A1.5", "--budget", "0", "--minutes", "60"])
        self.assertEqual(args.model, "Sage-A1.5")
        self.assertEqual(args.minutes, 60)
        self.assertEqual(args.budget, 0)
        prompt = task_prompt(args)
        self.assertIn("task_info", prompt)
        self.assertIn("paginated", prompt)
        self.assertNotIn("get_progress", prompt)

    def test_continuation_preserves_independent_work(self):
        args = argument_parser().parse_args([
            "--run", "runs/resume", "--resume-task", "runs/source/task"])
        self.assertIn("prior partial implementation", task_prompt(args))


if __name__ == "__main__":
    unittest.main()
