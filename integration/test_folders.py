# coding: utf-8

"""
Tests that the variable Folders API is properly suported
"""



import os
from unittest import TestCase

from scrunch import connect
from scrunch import get_dataset
from scrunch.datasets import Variable
from scrunch.folders import Folder
from scrunch.exceptions import InvalidPathError
from fixtures import NEWS_DATASET
from pycrunch.shoji import Catalog

HOST = os.environ['SCRUNCH_HOST']
username = os.environ['SCRUNCH_USER']
password = os.environ['SCRUNCH_PASS']

site = connect(username, password, HOST)


def setup_folders(ds):
    sess = ds.session
    ds.settings.edit(variable_folders=True)
    ds.variables.create({
        'element': 'shoji:entity',
        'body': {'name': 'testvar1', 'type': 'numeric'}
    })
    ds.variables.create({
        'element': 'shoji:entity',
        'body': {'name': 'testvar2', 'type': 'numeric'}
    })
    ds.variables.create({
        'element': 'shoji:entity',
        'body': {'name': 'testvar3', 'type': 'numeric'}
    })
    ds.refresh()
    sf1 = Catalog(sess, body={
        'name': 'Subfolder 1'
    })
    sf1 = ds.folders.create(sf1)
    sfa = Catalog(sess, body={
        'name': 'Subfolder A'
    })
    sfa = sf1.create(sfa)
    sf2 = Catalog(sess, body={
            'name': 'Subfolder 2'
        })
    sf2 = ds.folders.create(sf2)
    variables = ds.variables.by('alias')
    sf1.patch({'index': {
        variables['age'].entity_url: {}
    }})
    sfa.patch({'index': {
        variables['gender'].entity_url: {}
    }})
    sf2.patch({'index': {
        variables['socialmedia'].entity_url: {}
    }})


class TestFolders(TestCase):
    def setUp(self):
        self._ds = site.datasets.create({
            'element': 'shoji:entity',
            'body': {
                'name': 'test_folders',
                'table': {
                    'element': 'crunch:table',
                    'metadata': NEWS_DATASET
                },
            }
        }).refresh()
        ds = self._ds
        setup_folders(ds)
        self.ds = get_dataset(ds.body.id)

    def test_get_folders(self):
        ds = self.ds
        root = ds.folders.get('|')
        sf1 = ds.folders.get('| Subfolder 1')
        sfa = ds.folders.get('| Subfolder 1 | Subfolder A')

        # Equivalent ways of fetching Subfolder A
        sfa2 = root.get('Subfolder 1 | Subfolder A')
        sfa3 = sf1.get('Subfolder A')
        self.assertEqual(sfa.url, sfa2.url)
        self.assertEqual(sfa.url, sfa3.url)

        # Fetching a variable by path
        variable = ds.folders.get('| Subfolder 1 | Subfolder A | Gender')

        self.assertTrue(isinstance(sf1, Folder))
        self.assertTrue(isinstance(sfa, Folder))
        self.assertTrue(isinstance(variable, Variable))

        self.assertEqual(sf1.name, 'Subfolder 1')
        self.assertEqual(sfa.name, 'Subfolder A')
        self.assertEqual(sf1.parent, root)
        self.assertEqual(sfa.parent.name, sf1.name)
        self.assertEqual(sfa.parent.path, sf1.path)
        self.assertEqual(sfa.path, '| Subfolder 1 | Subfolder A')
        self.assertEqual(variable.alias, 'gender')
        self.assertEqual(variable.type, 'categorical')

        bad_path = '| bad folder'
        with self.assertRaises(InvalidPathError) as err:
            ds.folders.get(bad_path)
        self.assertEqual(err.exception.message, "Invalid path: %s" % bad_path)

    def test_make_subfolder(self):
        ds = self.ds
        root = ds.folders.get('|')
        mit = root.make_subfolder('Made in test')
        self.assertEqual(mit.path, "| Made in test")
        mit2 = root.get(mit.name)
        self.assertEqual(mit2.url, mit.url)
        nested = mit.make_subfolder('nested level')
        self.assertEqual(mit.get_child(nested.name).url, nested.url)

    def test_reorder_folder(self):
        ds = self.ds
        root = ds.folders.get('|')
        folder = root.make_subfolder('ToReorder')
        sf1 = folder.make_subfolder('1')
        sf2 = folder.make_subfolder('2')
        sf3 = folder.make_subfolder('3')
        var = ds['testvar1']
        folder.move_here([var])
        children = folder.children
        self.assertEqual([c.url for c in children],
            [c.url for c in [sf1, sf2, sf3, var]])

        # Reorder placing sf1 at the end
        folder.reorder([sf2, var, sf3, sf1])
        children = folder.children
        self.assertEqual([c.url for c in children],
            [c.url for c in [sf2, var, sf3, sf1]])

    def test_move_between_folders(self):
        root = self.ds.folders.root
        target1 = root.make_subfolder("t1")
        target2 = root.make_subfolder("t2")
        self.assertEqual(target1.children, [])
        self.assertEqual(target2.children, [])
        var1 = self.ds['testvar2']
        var2 = self.ds['testvar3']
        target1.move_here([var2, var1])
        self.assertEqual([c.url for c in target1.children],
            [var2.url, var1.url])

        nested = target1.make_subfolder('nested')
        self.assertEqual([c.url for c in target1.children],
            [var2.url, var1.url, nested.url])

        # Move var2 to t2
        target2.move_here(var2)
        # Now, t1 does not have var2, it is under t2
        self.assertEqual([c.url for c in target1.children],
            [var1.url, nested.url])
        self.assertEqual([c.url for c in target2.children],
            [var2.url])

        # Move nested and var1 to t2
        target2.move_here([nested, var1])

        # Now, they are all in t2, t1 is empty
        self.assertEqual([c.url for c in target2.children],
            [var2.url, nested.url, var1.url])
        self.assertEqual([c.url for c in target1.children], [])

        # Move var1 to nested
        nested.move_here(var1)

        # Now, t2 doesn't have var1, but nested does
        self.assertEqual([c.url for c in target2.children],
            [var2.url, nested.url])
        self.assertEqual([c.url for c in nested.children],
            [var1.url])

        # Move nested to t1
        target1.move_here(nested)

        # Now t2 has only var2 and t1 has nested in it
        self.assertEqual([c.url for c in target2.children],
            [var2.url])
        self.assertEqual([c.url for c in target1.children],
            [nested.url])

    def _test_hidden_folder(self):
        assert False

    def _test_hide_variable(self):
        assert False
