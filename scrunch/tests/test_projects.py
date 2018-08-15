# coding: utf-8

import json
from mock import Mock
from requests import Response
from unittest import TestCase

from pycrunch.shoji import Entity, Catalog, Order

from scrunch.order import InvalidPathError
from scrunch.datasets import Project, ProjectDatasetsOrder

from .mock_session import MockSession


class TestProjectNesting(TestCase):
    def test_detect_correct_handler(self):
        session = Mock(
            feature_flags={'old_projects_order': True}
        )
        dataset_order = Order(session, **{
            'graph': []
        })
        datasets_catalog = Catalog(session, **{
            'index': {},
            'order': dataset_order
        })
        shoji_resource = Entity(session, **{
            'self': '/project/url/',
            'body': {},
            'index': {},
            'datasets': datasets_catalog
        })
        project = Project(shoji_resource)
        self.assertTrue(isinstance(project.order, ProjectDatasetsOrder))

        session = Mock(
            feature_flags={'old_projects_order': False}
        )
        shoji_resource = Entity(session, **{
            'self': '/project/url/',
            'body': {},
            'index': {},
            'graph': []
        })
        project = Project(shoji_resource)
        self.assertTrue(isinstance(project.order, Project))

    def test_create_subproject(self):
        session = Mock(
            feature_flags={'old_projects_order': False}
        )
        shoji_resource = Entity(session, **{
            'self': '/project/url/',
            'body': {},
            'index': {},
            'graph': [],
        })
        response = Response()
        response.status_code = 201
        response.headers = {
            'Location': '/project/url/'
        }
        session.post.return_value = response
        project = Project(shoji_resource)

        # Create a new project
        pa = project.order.create_project("Project A")
        self.assertTrue(isinstance(pa, Project))

        # Check that we sent the correct payload to the server
        session.post.assert_called_once()
        func_name, _args, _kwargs = session.post.mock_calls[0]

        # The post happened to the project entity URL
        self.assertEqual(_args[0], project.url)
        # The payload is a valid payload containing the name
        self.assertEqual(json.loads(_args[1]), {
            'element': 'shoji:entity',
            'body': {
                'name': 'Project A'
            }
        })

    def make_tree(self):
        session = MockSession()
        session.feature_flags = {'old_projects_order': False}

        #       A
        #     /   \
        #    B     C
        #    |
        #    D
        a_res_url = 'http://example.com/api/projects/A/'
        b_res_url = 'http://example.com/api/projects/B/'
        c_res_url = 'http://example.com/api/projects/C/'
        d_res_url = 'http://example.com/api/projects/D/'
        a_payload = {
            'element': 'shoji:entity',
            'self': a_res_url,
            'catalogs': {
                'project': 'http://example.com/api/projects/'
            },
            'body': {
                'name': 'project A'
            },
            'index': {
                b_res_url: {
                    'id': 'idB',
                    'name': 'project B',
                    'icon': None,
                    'description': '',
                    'type': 'project'
                },
                c_res_url: {
                    'id': 'idC',
                    'name': 'project C',
                    'icon': None,
                    'description': '',
                    'type': 'project'
                }
            },
            'graph': [c_res_url, b_res_url]
        }
        b_payload = {
            'element': 'shoji:entity',
            'self': b_res_url,
            'catalogs': {
                'project': a_res_url
            },
            'body': {'name': 'project B'},
            'index': {
                d_res_url: {
                    'id': 'idD',
                    'name': 'project D',
                    'icon': None,
                    'description': '',
                    'type': 'project'
                }
            },
            'graph': [d_res_url]
        }
        c_payload = {
            'element': 'shoji:entity',
            'self': c_res_url,
            'catalogs': {
                'project': a_res_url
            },
            'body': {'name': 'project C'},
            'index': {},
            'graph': []
        }
        d_payload = {
            'element': 'shoji:entity',
            'self': d_res_url,
            'catalogs': {
                'project': b_res_url
            },
            'body': {'name': 'project D'},
            'index': {},
            'graph': []
        }
        session.add_fixture(a_res_url, a_payload)
        session.add_fixture(b_res_url, b_payload)
        session.add_fixture(c_res_url, c_payload)
        session.add_fixture(d_res_url, d_payload)
        return session

    def test_follow_path(self):
        a_res_url = 'http://example.com/api/projects/A/'
        d_res_url = 'http://example.com/api/projects/D/'

        session = self.make_tree()
        a_res = session.get(a_res_url).payload
        project_a = Project(a_res)
        project_c = project_a.order['| project C ']
        project_d = project_a.order['| project B | project D']
        self.assertTrue(isinstance(project_d, Project))
        self.assertEqual(project_d.resource.self, d_res_url)

        with self.assertRaises(InvalidPathError):
            project_a.order['| project B | Invalid']

    def test_rename(self):
        a_res_url = 'http://example.com/api/projects/A/'
        session = self.make_tree()
        a_res = session.get(a_res_url).payload
        project_a = Project(a_res)
        project_d = project_a.order['| project B | project D']
        project_d.rename('Renamed Project D')
        # This works because .rename() implementation calls shoji Entity.edit
        # which will make the request an update the resource's internal payload
        # as well. If this passes it means that Scrunch is correct and pycrunch
        # did its thing.
        self.assertEqual(project_d.resource.body.name, 'Renamed Project D')
        self.assertEqual(project_d.name, 'Renamed Project D')

    def test_move_things(self):
        a_res_url = 'http://example.com/api/projects/A/'
        dataset_url = 'http://example.com/api/datasets/1/'
        session = self.make_tree()
        project_a = Project(session.get(a_res_url).payload)
        project_c = project_a.order['| project C ']
        project_d = project_a.order['| project B | project D']
        dataset = Mock(url=dataset_url)

        # Moving project C under project D
        project_d.move_here([project_c, dataset])

        # After a move_here there is a PATCH and a GET
        # the PATCH performs the changes and the GET is a resource.refresh()
        patch_request = session.requests[-2]
        refresh_request = session.requests[-1]
        self.assertEqual(refresh_request.method, 'GET')
        self.assertEqual(refresh_request.url, project_d.url)
        self.assertEqual(patch_request.method, 'PATCH')
        self.assertEqual(patch_request.url, project_d.url)
        self.assertEqual(json.loads(patch_request.body), {
            'element': 'shoji:entity',
            'body': {},
            'index': {
                project_c.url: {},
                dataset.url: {}
            },
            'graph': [project_c.url, dataset.url]
        })

    def test_place(self):
        a_res_url = 'http://example.com/api/projects/A/'
        dataset1_url = 'http://example.com/api/datasets/1/'
        dataset2_url = 'http://example.com/api/datasets/2/'
        session = self.make_tree()
        project_a = Project(session.get(a_res_url).payload)
        project_b = project_a.order['| project B']
        project_d = project_a.order['| project B | project D']
        dataset1 = Mock(url=dataset1_url)
        dataset2 = Mock(url=dataset2_url)
        dataset1.name = 'Dataset 1'
        dataset2.name = 'Dataset 2'

        # Do a .place call
        project_a.place(dataset1, '| project B', before='project D')

        # After a move_here there is a PATCH and a GET
        # the PATCH performs the changes and the GET is a resource.refresh()
        patch_request = session.requests[-2]
        self.assertEqual(patch_request.method, 'PATCH')

        # Note the patch is to project B even though we did `.place` on
        # project A, but the target path pointed to B
        self.assertEqual(patch_request.url, project_b.url)
        self.assertEqual(json.loads(patch_request.body), {
            'element': 'shoji:entity',
            'body': {},
            'index': {
                dataset1.url: {}
            },
            # Note how the graph sent includes dataset1.url before project D
            'graph': [dataset1.url, project_d.url]
        })

        # Since the PATCH did not really update the server or the session
        # test fixtures, we need to update the fixtures to reflect the fact
        # that they've been modified by the recent PATCH request
        session.adapter.fixtures[project_b.url]['index'][dataset1.url] = {
            'name': dataset1.name,
            'type': 'dataset'
        }
        session.adapter.fixtures[project_b.url]['graph'] = [dataset1.url, project_d.url]
        project_a.place(dataset2, '| project B', after='Dataset 1')
        patch_request = session.requests[-2]
        self.assertEqual(patch_request.method, 'PATCH')
        self.assertEqual(patch_request.url, project_b.url)
        self.assertEqual(json.loads(patch_request.body), {
            'element': 'shoji:entity',
            'body': {},
            'index': {
                dataset2.url: {}
            },
            # Dataset 2 got placed after dataset 1 :)
            'graph': [dataset1.url, dataset2.url, project_d.url]
        })

    def test_reorder(self):
        session = self.make_tree()
        a_res_url = 'http://example.com/api/projects/A/'
        b_res_url = 'http://example.com/api/projects/B/'
        c_res_url = 'http://example.com/api/projects/C/'
        project_a = Project(session.get(a_res_url).payload)
        project_a.reorder(["project C", "project B"])
        patch_request = session.requests[-2]
        self.assertEqual(patch_request.method, 'PATCH')
        self.assertEqual(patch_request.url, project_a.url)
        self.assertEqual(json.loads(patch_request.body), {
            'element': 'shoji:entity',
            'body': {},
            'index': {},
            'graph': [c_res_url, b_res_url]
        })

    def test_move(self):
        session = self.make_tree()
        a_res_url = 'http://example.com/api/projects/A/'
        b_res_url = 'http://example.com/api/projects/B/'
        c_res_url = 'http://example.com/api/projects/C/'
        project_a = Project(session.get(a_res_url).payload)
        project_a.reorder(["project C", "project B"])
        patch_request = session.requests[-2]
        self.assertEqual(patch_request.method, 'PATCH')
        self.assertEqual(patch_request.url, project_a.url)
        self.assertEqual(json.loads(patch_request.body), {
            'element': 'shoji:entity',
            'body': {},
            'index': {},
            'graph': [c_res_url, b_res_url]
        })

    def test_is_root(self):
        a_res_url = 'http://example.com/api/projects/A/'
        session = self.make_tree()
        project_a = Project(session.get(a_res_url).payload)
        project_b = project_a.order['| project B']
        self.assertTrue(project_a.is_root)
        self.assertFalse(project_b.is_root)
