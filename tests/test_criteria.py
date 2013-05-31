import unittest
import mock
from nose.tools import raises, eq_

import fedbadges.models


class TestCriteriaMatching(unittest.TestCase):
    @raises(KeyError)
    def test_malformed_criteria(self):
        criteria = fedbadges.models.Criteria(dict(
            watwat="does not exist",
        ))

class TestCriteriaCountGreaterThanOrEqualTo(unittest.TestCase):
    def setUp(self):
        self.criteria = fedbadges.models.Criteria(dict(
            datanommer={
                "filter": {
                    "topic": "{topic}",
                },
                "operation": "count",
                "condition": {
                    "greater than or equal to": 500,
                }
            }
        ))
        self.message = dict(
            topic="org.fedoraproject.dev.something.sometopic",
        )
        class MockQuery(object):
            def count(query):
                return self.returned_count
        self.mock_query = MockQuery()

    def test_basic_datanommer_query_undershoot(self):
        self.returned_count = 499
        expectation = False

        with mock.patch('datanommer.models.Message.grep') as f:
            f.return_value = None, None, self.mock_query
            result = self.criteria.matches(self.message)
            eq_(result, expectation)
            f.assert_called_once_with(
                topic="org.fedoraproject.dev.something.sometopic",
                defer=True,
            )

    def test_basic_datanommer_query_spot_on(self):
        self.returned_count = 500
        expectation = True

        with mock.patch('datanommer.models.Message.grep') as f:
            f.return_value = None, None, self.mock_query
            result = self.criteria.matches(self.message)
            eq_(result, expectation)
            f.assert_called_once_with(
                topic="org.fedoraproject.dev.something.sometopic",
                defer=True,
            )

    def test_basic_datanommer_query_overshoot(self):
        self.returned_count = 501
        expectation = True

        with mock.patch('datanommer.models.Message.grep') as f:
            f.return_value = None, None, self.mock_query
            result = self.criteria.matches(self.message)
            eq_(result, expectation)
            f.assert_called_once_with(
                topic="org.fedoraproject.dev.something.sometopic",
                defer=True,
            )

    # TODO -- test more complicated combinations of filter and must be
    # TODO -- test more malformed queries
