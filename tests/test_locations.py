import unittest
from backend.locations import matches
from backend.pipeline_skeleton import _matches_location

class LocationsTests(unittest.TestCase):
    def test_india_expansion(self):
        for location in ('Bangalore','Bengaluru','Pune','Mumbai, Maharashtra','Hyderabad, India','Remote - India','IN','Pune / Chennai','Tamil Nadu','IND'):
            with self.subTest(location=location):
                self.assertTrue(matches(location,'india'))
                self.assertTrue(_matches_location({'location':location},['india']))

    def test_foreign_ambiguous_and_unknown_locations(self):
        for location in ('Indiana, USA','Indianapolis','Remote','London, UK','Punjab, Pakistan','Kochi, Japan','Working in Singapore','',None):
            with self.subTest(location=location):
                self.assertFalse(matches(location,'India'))
        self.assertTrue(matches('Pune, India; London, UK','India'))

    def test_countries_and_codes_worldwide(self):
        for location,country in [('Berlin, Germany','Germany'),('Tokyo','Japan'),('Sydney, AU','Australia'),('Toronto, Canada','CA'),('Sao Paulo, Brazil','Brazil'),('Cape Town, ZA','South Africa'),('Dubai, UAE','United Arab Emirates'),('New York, USA','US')]:
            with self.subTest(location=location,country=country):
                self.assertTrue(matches(location,country))
                self.assertTrue(_matches_location({'location':location},[country.lower()]))
        self.assertFalse(matches('Berlin, Germany','Japan'))
        self.assertFalse(matches('Working in Singapore','IN'))

    def test_city_aliases_and_context(self):
        self.assertTrue(matches('Bangalore, India','Bengaluru'))
        self.assertTrue(matches('Munich, Germany','M\u00fcnchen'))
        self.assertTrue(matches('London, Canada','London'))
        self.assertFalse(matches('London, Canada','London, UK'))
        self.assertFalse(matches('London','UK'))
        self.assertFalse(matches('Berlin','Germany'))
        self.assertTrue(matches('San Francisco, CA','US'))
        self.assertFalse(matches('San Francisco, CA','Canada'))
        self.assertTrue(matches('Tokyo - Japan','JP'))
        self.assertTrue(matches('London, UK; Toronto, Canada','Canada'))

    def test_other_searches_remain_literal(self):
        self.assertTrue(matches('Bengaluru, India','bengaluru'))
        self.assertFalse(matches('Bengaluru','Mumbai'))
        self.assertTrue(_matches_location({'location':None},['india']))
