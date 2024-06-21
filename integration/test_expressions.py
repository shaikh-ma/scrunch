# coding: utf-8

import os
from unittest import TestCase

from pycrunch.importing import Importer

from integration.fixtures import MR_CATS, as_entity
from scrunch import connect
from scrunch.mutable_dataset import get_mutable_dataset

HOST = os.environ['SCRUNCH_HOST']
username = os.environ['SCRUNCH_USER']
password = os.environ['SCRUNCH_PASS']


class TestExpressions(TestCase):

    SITE = None

    @property
    def site(self):
        if self.SITE is None:
            _site = connect(username, password, HOST)
            assert _site is not None, "Unable to connect to %s" % HOST
            self.SITE = _site
        return self.SITE

    def _create_mr_dataset(self, name, rows):
        _dataset_metadata = {
            "mr_variable": {
                "name": "Multiple Response",
                "type": "multiple_response",
                "categories": MR_CATS,
                "subreferences": [{
                    "name": "Response 1",
                    "alias": "response_1"
                }, {
                    "name": "Response 2",
                    "alias": "response_2"
                }, {
                    "name": "Response 3",
                    "alias": "response_3"
                }]
            },
        }
        ds = self.site.datasets.create({
            'element': 'shoji:entity',
            'body': {
                'name': name,
                'table': {
                    'element': 'crunch:table',
                    'metadata': _dataset_metadata
                },
            }
        }).refresh()
        Importer().append_rows(ds, rows)
        scrunch_dataset = get_mutable_dataset(ds.body.id, self.site)
        return ds, scrunch_dataset

    def test_multiple_response_any_add_filter(self):
        ds_rows = [
            ["response_1", "response_2", "response_3"],
            [1, 2, 1],
            [1, 2, 2],
            [2, 1, 1]
        ]
        ds, scrunch_dataset = self._create_mr_dataset('test_mr_any', ds_rows)
        _filter = "mr_variable.any([response_1])"
        try:
            scrunch_dataset.add_filter(name='filter_1', expr=_filter)
            data = ds.follow("table", "limit=20")['data']
            ds_variables = ds.variables.by("alias")
            mr_variable_id = ds_variables["mr_variable"].id
            assert data[mr_variable_id] == [
                [1, 2, 1],
                [1, 2, 2],
                [2, 1, 1]
            ]
        finally:
            # cleanup
            ds.delete()
    
    def test_categorical_any_add_filter(self):
        ds = self.site.datasets.create(as_entity({"name": "test_any_categorical_add_filter"})).refresh()
        categories = [
            {"id": 1, "name": "One", "missing": False, "numeric_value": None},
            {"id": 2, "name": "Two", "missing": False, "numeric_value": None},
            {"id": 3, "name": "Three", "missing": False, "numeric_value": None},
            {"id": -1, "name": "No Data", "missing": True, "numeric_value": None}
        ]
        ds.variables.create(as_entity({
            "name": "Categorical Var",
            "alias": "categorical_var",
            "type": "categorical",
            "categories": categories,
            "values": [
                1, 2, 3, -1, -1, -1, 1, 2, 1
            ]
        }))
        scrunch_dataset = get_mutable_dataset(ds.body.id, self.site)
        _filter = "categorical_var.any([1])"
        try:
            resp = scrunch_dataset.add_filter(name='filter_1', expr=_filter)
            data = ds.follow("table", "limit=20")['data']
            ds_variables = ds.variables.by("alias")
            cat_var_id = ds_variables["categorical_var"].id
        finally:
            # cleanup
            ds.delete()

    def test_append_dataset_any_filter_multiple_response(self):
        ds_rows = [
            ["response_1", "response_2", "response_3"],
            [1, 2, 1],
            [1, 2, 2],
            [1, 1, 1]
        ]
        ds_to_append_rows = [
            ["response_1", "response_2", "response_3"],
            [1, 1, 2],
            [2, 1, 1],
            [1, 1, 2]
        ]
        ds, scrunch_dataset = self._create_mr_dataset('test_mr_any_subvar', ds_rows)
        ds_to_append, scrunch_dataset_to_append = self._create_mr_dataset(
            'test_mr_any_to_append_subvar',
            ds_to_append_rows
        )
        # This filter should get only the rows that have the news_source variable with the value 1
        # at the same time for both news_source_1 and news_source_2
        _filter = "mr_variable.any([response_1, response_2])"
        try:
            scrunch_dataset.append_dataset(scrunch_dataset_to_append, filter=_filter)
            ds_variables = ds.variables.by("alias")
            mr_variable_id = ds_variables["mr_variable"].id
            data = ds.follow("table", "limit=20")['data']
            assert data[mr_variable_id] == [
                [1, 2, 1],
                [1, 2, 2],
                [1, 1, 1],
                [1, 1, 2],
                [1, 1, 2]
            ]
        finally:
            # cleanup
            ds.delete()
            ds_to_append.delete()

    def test_any_filter_categorical(self):
        ds = self.site.datasets.create(as_entity({"name": "test_any_category"})).refresh()
        ds_to_append = self.site.datasets.create(as_entity({"name": "test_any_category_to_append"})).refresh()
        categories = [
            {"id": 1, "name": "One", "missing": False, "numeric_value": None},
            {"id": 2, "name": "Two", "missing": False, "numeric_value": None},
            {"id": 3, "name": "Three", "missing": False, "numeric_value": None},
            {"id": -1, "name": "No Data", "missing": True, "numeric_value": None}
        ]
        ds.variables.create(as_entity({
            "name": "Categorical Variable",
            "alias": "categorical_var",
            "type": "categorical",
            "categories": categories,
            "values": [
                1, 2, 3, -1, -1, -1, 1, 2, 1
            ]
        }))
        ds_to_append.variables.create(as_entity({
            "name": "Categorical Variable",
            "alias": "categorical_var",
            "type": "categorical",
            "categories": categories,
            "values": [
                1, 3, -1, 2
            ]
        }))
        scrunch_dataset = get_mutable_dataset(ds.body.id, self.site)
        scrunch_dataset_to_append = get_mutable_dataset(ds_to_append.body.id, self.site)

        # This filter should get only the rows that have the news_source variable with the value 1
        # at the same time for both news_source_1 and news_source_2
        _filter = "categorical_var.any([1])"
        try:
            resp = scrunch_dataset.append_dataset(scrunch_dataset_to_append, filter=_filter)
            ds_variables = ds.variables.by("alias")
            data = ds.follow("table", "limit=20")['data']
        finally:
            # cleanup
            ds.delete()
            ds_to_append.delete()
