import unittest
from codehydra.routing.classifier import Classifier


class TestClassifier(unittest.TestCase):
    def setUp(self):
        self.classifier = Classifier()

    def test_low_effort_greetings(self):
        self.assertEqual(self.classifier.evaluate("hi"), "low")
        self.assertEqual(self.classifier.evaluate("thanks"), "low")
        self.assertEqual(self.classifier.evaluate("ok"), "low")

    def test_medium_effort_coding_tasks(self):
        self.assertEqual(self.classifier.evaluate("fix this bug in the login flow"), "medium")
        self.assertEqual(self.classifier.evaluate("implement a new feature for user profile"), "medium")
        self.assertEqual(self.classifier.evaluate("plan a new auth flow for the app"), "medium")
        self.assertEqual(self.classifier.evaluate("explain the gateway module"), "medium")
        self.assertEqual(self.classifier.evaluate("what is this code doing"), "medium")
        self.assertEqual(self.classifier.evaluate("tell me a joke"), "medium")

    def test_high_effort(self):
        self.assertEqual(self.classifier.evaluate("refactor the entire auth system"), "high")
        self.assertEqual(self.classifier.evaluate("architect a new microservice"), "high")
        self.assertEqual(self.classifier.evaluate("rewrite from scratch"), "high")


if __name__ == "__main__":
    unittest.main()
