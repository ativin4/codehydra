import unittest
from codehydra.routing.classifier import Classifier

class TestClassifier(unittest.TestCase):
    def setUp(self):
        self.classifier = Classifier()

    def test_low_effort(self):
        self.assertEqual(self.classifier.evaluate("hi"), "low")
        self.assertEqual(self.classifier.evaluate("tell me a joke"), "low")

    def test_medium_effort(self):
        self.assertEqual(self.classifier.evaluate("fix this bug in the login flow"), "medium")
        self.assertEqual(self.classifier.evaluate("implement a new feature for user profile"), "medium")

    def test_high_effort(self):
        self.assertEqual(self.classifier.evaluate("refactor the entire auth system"), "high")
        self.assertEqual(self.classifier.evaluate("architect a new microservice"), "high")

if __name__ == "__main__":
    unittest.main()
