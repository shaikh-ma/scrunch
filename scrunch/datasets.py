import collections
import datetime
import json
import logging
import re
import os
import sys
import six

import pandas as pd
import pycrunch
from pycrunch.exporting import export_dataset

from scrunch.categories import CategoryList
from scrunch.exceptions import (InvalidPathError, AuthenticationError,
                                InvalidReferenceError, OrderUpdateError)
from scrunch.expressions import parse_expr, prettify, process_expr
from scrunch.helpers import (ReadOnly, _validate_category_rules,
                             download_file, shoji_entity_wrapper,
                             abs_url, case_expr, subvar_alias)
from scrunch.order import DatasetVariablesOrder, ProjectDatasetsOrder
from scrunch.subentity import Deck, Filter, Multitable
from scrunch.variables import (combinations_from_map, combine_categories_expr,
                               combine_responses_expr, responses_from_map)

_MR_TYPE = 'multiple_response'

if six.PY2:  # pragma: no cover
    from urlparse import urljoin
    import ConfigParser as configparser
else:
    import configparser
    from urllib.parse import urljoin


LOG = logging.getLogger('scrunch')


def _set_debug_log():
    # ref: http://docs.python-requests.org/en/master/api/#api-changes
    #
    #  These two lines enable debugging at httplib level
    # (requests->urllib3->http.client)
    # You will see the REQUEST, including HEADERS and DATA,
    # and RESPONSE with HEADERS but without DATA.
    # The only thing missing will be the response.body which is not logged.
    try:
        import http.client as http_client
    except ImportError:
        # Python 2
        import httplib as http_client
    http_client.HTTPConnection.debuglevel = 1
    LOG.setLevel(logging.DEBUG)
    requests_log = logging.getLogger("requests.packages.urllib3")
    requests_log.setLevel(logging.DEBUG)
    requests_log.propagate = True


def _get_connection(file_path='crunch.ini'):
    """
    Utilitarian function that reads credentials from
    file or from ENV variables
    """
    if pycrunch.session is not None:
        return pycrunch.session
    # try to get credentials from environment
    username = os.environ.get('CRUNCH_USERNAME')
    password = os.environ.get('CRUNCH_PASSWORD')
    site = os.environ.get('CRUNCH_URL')
    if username and password and site:
        return pycrunch.connect(username, password, site)
    elif username and password:
        return pycrunch.connect(username, password)
    # try reading from .ini file
    config = configparser.ConfigParser()
    config.read(file_path)
    try:
        username = config.get('DEFAULT', 'CRUNCH_USERNAME')
        password = config.get('DEFAULT', 'CRUNCH_PASSWORD')
    except:
        username = password = None
    try:
        site = config.get('DEFAULT', 'CRUNCH_URL')
    except:
        site = None
    # now try to login with obtained creds
    if username and password and site:
        return pycrunch.connect(username, password, site)
    elif username and password:
        return pycrunch.connect(username, password)
    else:
        raise AuthenticationError(
            "Unable to find crunch session, crunch.ini file or environment variables.")


def _get_dataset(dataset, connection=None, editor=False, streaming=False):
    """
    Helper method for specific get_dataset and get_streaming_dataset methods.
    Retrieve a reference to a given dataset (either by name, or ID) if it exists
    and the user has access permissions to it. If you have access to the dataset
    through a project you should do pass the project parameter.
    This method tries to use pycrunch singleton connection, environment variables
    or a crunch.ini config file if the optional "connection" parameter isn't provided.

    Also able to change editor while getting the dataset with the optional
    editor parameter.

    Returns a Dataset Entity record if the dataset exists.
    Raises a KeyError if no such dataset exists.

    To get a.BaseDataset from a Project we are building a url and making a request
    through pycrunch.session object, we instead should use the /search endpoint
    from crunch, but currently it's not working by id's.
    """
    if connection is None:
        connection = _get_connection()
        if not connection:
            raise AttributeError(
                "Authenticate first with scrunch.connect() or by providing "
                "config/environment variables")
    root = connection
    root_datasets = root.datasets
    # search by dataset name
    try:
        shoji_ds = root_datasets.by('name')[dataset].entity
    except KeyError:
        # search by dataset id
        try:
            shoji_ds = root_datasets.by('id')[dataset].entity
        except KeyError:
            # search by id on any project
            try:
                dataset_url = urljoin(
                    root.catalogs.datasets, '{}/'.format(dataset))
                shoji_ds = root.session.get(dataset_url).payload
            except Exception:
                raise KeyError(
                    "Dataset (name or id: %s) not found in context." % dataset)
    return shoji_ds, root


def get_project(project, connection=None):
    """
    :param project: Crunch project ID or Name
    :param connection: An scrunch session object
    :return: Project class instance
    """
    if connection is None:
        connection = _get_connection()
        if not connection:
            raise AttributeError(
                "Authenticate first with scrunch.connect() or by providing "
                "config/environment variables")
    try:
        ret = connection.projects.by('name')[project].entity
    except KeyError:
        try:
            ret = connection.projects.by('id')[project].entity
        except KeyError:
            raise KeyError("Project (name or id: %s) not found." % project)
    return Project(ret)


def get_user(user, connection=None):
    """
    :param user: Crunch user email address
    :param connection: An scrunch session object
    :return: User class instance
    """
    if connection is None:
        connection = _get_connection()
        if not connection:
            raise AuthenticationError(
                "Unable to find crunch session, crunch.ini file or \
                environment variables.")
    try:
        ret = connection.users.by('email')[user].entity
    except KeyError:
        raise KeyError("User email '%s' not found." % user)
    return User(ret)


class User:
    _MUTABLE_ATTRIBUTES = {'name', 'email'}
    _IMMUTABLE_ATTRIBUTES = {'id'}
    _ENTITY_ATTRIBUTES = _MUTABLE_ATTRIBUTES | _IMMUTABLE_ATTRIBUTES

    def __init__(self, user_resource):
        self.resource = user_resource
        self.url = self.resource.self

    def __getattr__(self, item):
        if item in self._ENTITY_ATTRIBUTES:
            return self.resource.body[item]  # Has to exist

        # Attribute doesn't exists, must raise an AttributeError
        raise AttributeError('User has no attribute %s' % item)

    def __repr__(self):
        return "<User: email='{}'; id='{}'>".format(self.email, self.id)

    def __str__(self):
        return self.email


class Project:
    _MUTABLE_ATTRIBUTES = {'name', 'description', 'icon'}
    _IMMUTABLE_ATTRIBUTES = {'id'}
    _ENTITY_ATTRIBUTES = _MUTABLE_ATTRIBUTES | _IMMUTABLE_ATTRIBUTES

    def __init__(self, project_resource):
        self.resource = project_resource
        self.url = self.resource.self
        self.order = ProjectDatasetsOrder(
            self.resource.datasets, self.resource.datasets.order)

    def __getattr__(self, item):
        if item in self._ENTITY_ATTRIBUTES:
            return self.resource.body[item]  # Has to exist

        # Attribute doesn't exists, must raise an AttributeError
        raise AttributeError('Project has no attribute %s' % item)

    def __repr__(self):
        return "<Project: name='{}'; id='{}'>".format(self.name, self.id)

    def __str__(self):
        return self.name

    @property
    def users(self):
        """
        :return: dictionary of User instances
        """
        # TODO: return a dictionary keyed by email and values should be User
        # instances, but when trying got 403 from Crunch
        return [e['email'] for e in self.resource.members.index.values()]
        # return {val['email']: User(val.entity) for val in self.resource.members.index.values()}

    def remove_user(self, user):
        """
        :param user: email or User instance
        :return: None
        """
        if not isinstance(user, User):
            user = get_user(user)

        found_url = None
        for url, tuple in self.resource.members.index.items():
            if tuple['email'] == user.email:
                found_url = url

        if found_url:
            self.resource.members.patch({found_url: None})
        else:
            raise KeyError("User %s not found in project %s" % (user.email, self.name))

    def add_user(self, user, edit=False):
        """
        :param user: email or User instance
        :return: None
        """
        if not isinstance(user, User):
            user = get_user(user)
        self.resource.members.patch({user.url: {'edit': edit}})

    def edit_user(self, user, edit):
        if not isinstance(user, User):
            user = get_user(user)
        self.resource.members.patch(
            {user.url: {'permissions': {'edit': edit}}}
        )


class CrunchBox(object):
    """
    A CrunchBox representation of boxdata.

    an instance cannot mutate it's metadata directly since boxdata doesn't
    support PATCHing. Instead, simply create a new `CrunchBox` instance with
    the same Filters and Variables. You'll get the same entity from the boxdata
    index with the updated metadata.

    :param shoji_tuple: pycrunch.shoji.Tuple of boxdata
    :param     dataset: scrunch.datasets.BaseDataset instance

    NOTE: since the boxdata entity is different regarding the mapping of body
          and metadata fields, methods etc... it is made `readonly`.
          Since an `edit` method would need to return a new
          instance (see above) the `__setattr__` method ist incorporated with
          CrunchBox specific messages.

          (an edit method returning an instance would most likely brake user
          expectations)

          In order to have a proper `remove` method we also need the Dataset
          instance.
    """

    WIDGET_URL = 'https://s.crunch.io/widget/index.html#/ds/{id}/'
    DIMENSIONS = dict(height=480, width=600)

    # the attributes on entity.body.metadata
    _METADATA_ATTRIBUTES = {'title', 'notes', 'header', 'footer'}

    _MUTABLE_ATTRIBUTES = _METADATA_ATTRIBUTES

    _IMMUTABLE_ATTRIBUTES = {
        'id', 'user_id', 'creation_time', 'filters', 'variables'}

    # removed `dataset` from the set above since it overlaps with the Dataset
    # instance on self. `boxdata.dataset` simply points to the dataset url

    _ENTITY_ATTRIBUTES = _MUTABLE_ATTRIBUTES | _IMMUTABLE_ATTRIBUTES

    def __init__(self, shoji_tuple, dataset):
        self.resource = shoji_tuple
        self.url = shoji_tuple.entity_url
        self.dataset = dataset

    def __setattr__(self, attr, value):
        """ known attributes should be readonly """

        if attr in self._IMMUTABLE_ATTRIBUTES:
            raise AttributeError(
                "Can't edit attibute '%s'" % attr)
        if attr in self._MUTABLE_ATTRIBUTES:
            raise AttributeError(
                "Can't edit '%s' of a CrunchBox. Create a new one with "
                "the same filters and variables to update its metadata" % attr)
        object.__setattr__(self, attr, value)

    def __getattr__(self, attr):
        if attr in self._METADATA_ATTRIBUTES:
            return self.resource.metadata[attr]

        if attr == 'filters':
            # return a list of `Filters` instead of the filters expr on `body`
            _filters = []
            for obj in self.resource.filters:
                f_url = obj['filter']
                _filters.append(
                    Filter(self.dataset.resource.filters.index[f_url]))
            return _filters

        if attr == 'variables':
            # return a list of `Variables` instead of the where expr on `body`
            _var_urls = []
            _var_map = self.resource.where.args[0].map
            for v in _var_map:
                _var_urls.append(_var_map[v]['variable'])

            return [
                Variable(entity, self.dataset)
                for url, entity in self.dataset._vars
                if url in _var_urls
            ]

        # all other attributes not catched so far
        if attr in self._ENTITY_ATTRIBUTES:
            return self.resource[attr]
        raise AttributeError('CrunchBox has no attribute %s' % attr)

    def __repr__(self):
        return "<CrunchBox: title='{}'; id='{}'>".format(
            self.title, self.id)

    def __str__(self):
        return self.title

    def remove(self):
        self.dataset.resource.session.delete(self.url)

    @property
    def widget_url(self):
        return self.WIDGET_URL.format(id=self.id)

    @widget_url.setter
    def widget_url(self, _):
        """ prevent edits to the widget_url """
        raise AttributeError("Can't edit 'widget_url' of a CrunchBox")

    def iframe(self, logo=None, dimensions=None):
        dimensions = dimensions or self.DIMENSIONS
        widget_url = self.widget_url

        if not isinstance(dimensions, dict):
            raise TypeError('`dimensions` needs to be a dict')

        def _figure(html):
            return '<figure style="text-align:left;" class="content-list-'\
                   'component image">' + '  {}'.format(html) + \
                   '</figure>'

        _iframe = (
            '<iframe src="{widget_url}" width="{dimensions[width]}" '
            'height="{dimensions[height]}" style="border: 1px solid #d3d3d3;">'
            '</iframe>')

        if logo:
            _img = '<img src="{logo}" stype="height:auto; width:200px;'\
                   ' margin-left:-4px"></img>'
            _iframe = _figure(_img) + _iframe

        elif self.title:
            _div = '<div style="padding-bottom: 12px">'\
                   '    <span style="font-size: 18px; color: #444444;'\
                   ' line-height: 1;">' + self.title + '</span>'\
                   '  </div>'
            _iframe = _figure(_div) + _iframe

        return _iframe.format(**locals())


class DatasetSettings(dict):

    def __readonly__(self, *args, **kwargs):
        raise RuntimeError('Please use the change_settings() method instead.')

    __setitem__ = __readonly__
    __delitem__ = __readonly__
    pop = __readonly__
    popitem = __readonly__
    clear = __readonly__
    update = __readonly__
    setdefault = __readonly__
    del __readonly__


class DatasetVariablesMixin(collections.Mapping):
    """
    Handles dataset variable iteration in a dict-like way
    """

    def __getitem__(self, item):
        # Check if the attribute corresponds to a variable alias
        variable = self._catalog.by('alias').get(item)
        if variable is None:
            variable = self._catalog.by('name').get(item)
            if variable is None:
                # Variable doesn't exists, must raise a ValueError
                raise ValueError('Entity %s has no (sub)variable with a name or alias %s' % (
                    self.name, item))
        return Variable(variable, self)

    def _set_catalog(self):
        self._catalog = self.resource.variables

    def _reload_variables(self):
        """
        Helper that takes care of updating self._vars on init and
        whenever the dataset adds a variable
        """
        self._set_catalog()
        self._vars = self._catalog.index.items()
        order = self._catalog.hier

        # The `order` property, which provides a high-level API for
        # manipulating the "Hierarchical Order" structure of a Dataset.
        self.order = DatasetVariablesOrder(self._catalog, order)

    def __iter__(self):
        for var in self._vars:
            yield var

    def __len__(self):
        return len(self._vars)

    def itervalues(self):
        for _, var_tuple in self._vars:
            yield Variable(var_tuple, self)

    def iterkeys(self):
        """
        Yield variable alias, since they are unique
        """
        for var in self._vars:
            yield var[1].alias

    def variable_names(self):
        """
        Simply return a list of all variable names in the Dataset
        """
        return [var[1].name for var in self._vars]

    def keys(self):
        return list(self.iterkeys())

    def values(self):
        return list(self.itervalues())

    def items(self):
        return zip(self.iterkeys(), self.itervalues())


class BaseDataset(ReadOnly, DatasetVariablesMixin):
    """
    A pycrunch.shoji.Entity wrapper that provides basic dataset methods.
    """

    _MUTABLE_ATTRIBUTES = {'name', 'notes', 'description', 'is_published',
                           'archived', 'end_date', 'start_date'}
    _IMMUTABLE_ATTRIBUTES = {'id', 'creation_time', 'modification_time'}
    _ENTITY_ATTRIBUTES = _MUTABLE_ATTRIBUTES | _IMMUTABLE_ATTRIBUTES
    _EDITABLE_SETTINGS = {'viewers_can_export', 'viewers_can_change_weight',
                          'viewers_can_share', 'dashboard_deck'}

    def __init__(self, resource):
        """
        :param resource: Points to a pycrunch Shoji Entity for a dataset.
        """
        super(BaseDataset, self).__init__(resource)
        self._settings = None
        # since we no longer have an __init__ on DatasetVariablesMixin because
        # of the multiple inheritance, we just initiate self._vars here
        self._reload_variables()

    def __getattr__(self, item):
        if item in self._ENTITY_ATTRIBUTES:
            return self.resource.body[item]  # Has to exist
        # Default behaviour
        return object.__getattribute__(self, item)

    def __repr__(self):
        return "<Dataset: name='{}'; id='{}'>".format(self.name, self.id)

    def __str__(self):
        return self.name

    @property
    def editor(self):
        try:
            return User(self.resource.follow('editor_url'))
        except pycrunch.lemonpy.ClientError:
            return self.resource.body.current_editor

    @editor.setter
    def editor(self, _):
        # Protect the `editor` from external modifications.
        raise TypeError('Unsupported operation on the editor property')

    def change_editor(self, user):
        """
        Change the current editor of the Crunch dataset.

        :param user:
            The email or a User instance of the user who should
            be set as the new current editor of the given dataset.
        """
        if not isinstance(user, User):
            user = get_user(user)
        self.resource.patch({'current_editor': user.url})
        self.resource.refresh()

    @property
    def owner(self):
        owner_url = self.resource.body.owner
        try:
            if '/users/' in owner_url:
                return User(self.resource.follow('owner_url'))
            else:
                return Project(self.resource.follow('owner_url'))
        except pycrunch.lemonpy.ClientError:
            return owner_url

    @owner.setter
    def owner(self, _):
        # Protect `owner` from external modifications.
        raise TypeError(
            'Unsupported operation on the owner property'
        )

    def change_owner(self, user=None, project=None):
        """
        :param user: email or User object
        :param project: id, name or Project object
        :return:
        """
        if user and project:
            raise AttributeError(
                "Must provide user or project. Not both"
            )
        owner_url = None
        if user:
            if not isinstance(user, User):
                user = get_user(user)
            owner_url = user.url
        if project:
            if not isinstance(project, Project):
                project = get_project(project)
            owner_url = project.url

        if not owner_url:
            raise AttributeError("Can't set owner")

        self.resource.patch({'owner': owner_url})
        self.resource.refresh()

    @property
    def settings(self):
        if self._settings is None:
            self._load_settings()
        return self._settings

    @settings.setter
    def settings(self, _):
        # Protect the `settings` property from external modifications.
        raise TypeError('Unsupported operation on the settings property')

    @property
    def filters(self):
        _filters = {}
        for f in self.resource.filters.index.values():
            filter_inst = Filter(f)
            _filters[filter_inst.name] = filter_inst
        return _filters

    @filters.setter
    def filters(self, _):
        # Protect the `filters` property from external modifications.
        raise TypeError('Use add_filter method to add filters')

    @property
    def decks(self):
        _decks = {}
        for d in self.resource.decks.index.values():
            deck_inst = Deck(d)
            _decks[deck_inst.id] = deck_inst
        return _decks

    @decks.setter
    def decks(self, _):
        # Protect the `decks` property from external modifications.
        raise TypeError('Use add_deck method to add a new deck')

    @property
    def multitables(self):
        _multitables = {}
        for mt in self.resource.multitables.index.values():
            mt_instance = Multitable(mt, self)
            _multitables[mt_instance.name] = mt_instance
        return _multitables

    @multitables.setter
    def multitables(self, _):
        # Protect the `multitables` property from direct modifications
        raise TypeError('Use the `create_multitable` method to add one')

    @property
    def crunchboxes(self):
        _crunchboxes = []
        for shoji_tuple in self.resource.boxdata.index.values():
            _crunchboxes.append(CrunchBox(shoji_tuple, self))
        return _crunchboxes

    @crunchboxes.setter
    def crunchboxes(self, _):
        # Protect the `crunchboxes` property from direct modifications
        raise TypeError('Use the `create_crunchbox` method to add one')

    def _load_settings(self):
        settings = self.resource.session.get(
            self.resource.fragments.settings).payload
        self._settings = DatasetSettings(
            (_name, _value) for _name, _value in settings.body.items()
        )
        return self._settings

    def change_settings(self, **kwargs):
        incoming_settings = set(kwargs.keys())
        invalid_settings = incoming_settings.difference(self._EDITABLE_SETTINGS)
        if invalid_settings:
            raise ValueError(
                'Invalid or read-only settings: %s'
                % ','.join(list(invalid_settings))
            )

        if 'dashboard_deck' in kwargs:
            ddeck = kwargs['dashboard_deck']
            if isinstance(ddeck, Deck):
                kwargs['dashboard_deck'] = ddeck.resource.self

        settings_payload = {
            setting: kwargs[setting] for setting in incoming_settings
        }
        if settings_payload:
            self.resource.session.patch(
                self.resource.fragments.settings,
                json.dumps(settings_payload),
                headers={'Content-Type': 'application/json'}
            )
            self._settings = None

    def edit(self, **kwargs):
        """
        Edit main Dataset attributes
        """
        for key in kwargs:
            if key not in self._MUTABLE_ATTRIBUTES:
                raise AttributeError("Can't edit attibute %s of variable %s" % (
                    key, self.name
                ))
            if key in ['start_date', 'end_date'] and \
                    (isinstance(kwargs[key], datetime.date) or
                     isinstance(kwargs[key], datetime.datetime)
                     ):
                kwargs[key] = kwargs[key].isoformat()

        return self.resource.edit(**kwargs)

    def create_single_response(self, categories, name, alias, description='',
                               missing=True, notes=''):
        """
        Creates a categorical variable deriving from other variables.
        Uses Crunch's `case` function.
        """
        cases = []
        for cat in categories:
            cases.append(cat.pop('case'))

        if not hasattr(self.resource, 'variables'):
            self.resource.refresh()

        args = [{
            'column': [c['id'] for c in categories],
            'type': {
                'value': {
                    'class': 'categorical',
                    'categories': categories}}}]

        for cat in args[0]['type']['value']['categories']:
            cat.setdefault('missing', False)

        if missing:
            args[0]['column'].append(-1)
            args[0]['type']['value']['categories'].append(dict(
                id=-1,
                name='No Data',
                numeric_value=None,
                missing=True))

        more_args = []
        for case in cases:
            more_args.append(parse_expr(case))

        more_args = process_expr(more_args, self.resource)

        expr = dict(function='case', args=args + more_args)

        payload = shoji_entity_wrapper(dict(
            alias=alias,
            name=name,
            expr=expr,
            description=description,
            notes=notes))

        new_var = self.resource.variables.create(payload)
        # needed to update the variables collection
        self._reload_variables()
        # return the variable instance
        return self[new_var['body']['alias']]

    def create_multiple_response(self, responses, name, alias, description='', notes=''):
        """
        Creates a Multiple response (array) using a set of rules for each
        of the responses(subvariables).
        """
        responses_map = collections.OrderedDict()
        responses_map_ids = []
        for resp in responses:
            case = resp['case']
            if isinstance(case, six.string_types):
                case = process_expr(parse_expr(case), self.resource)

            resp_id = '%04d' % resp['id']
            responses_map_ids.append(resp_id)
            responses_map[resp_id] = case_expr(
                case,
                name=resp['name'],
                alias='%s_%d' % (alias, resp['id'])
            )

        payload = shoji_entity_wrapper({
            'name': name,
            'alias': alias,
            'description': description,
            'notes': notes,
            'derivation': {
                'function': 'array',
                'args': [{
                    'function': 'select',
                    'args': [
                        {'map': responses_map},
                        {'value': responses_map_ids}
                    ]
                }]
            }
        })

        new_var = self.resource.variables.create(payload)
        # needed to update the variables collection
        self._reload_variables()
        # return an instance of Variable
        return self[new_var['body']['alias']]

    def create_numeric(self, alias, name, derivation, description='', notes=''):
        """
        Used to create new numeric variables using Crunchs's derived expressions
        """
        expr = parse_expr(derivation)

        if not hasattr(self.resource, 'variables'):
            self.resource.refresh()

        payload = shoji_entity_wrapper(dict(
            alias=alias,
            name=name,
            derivation=expr,
            description=description,
            notes=notes
        ))

        self.resource.variables.create(payload)
        # needed to update the variables collection
        self._reload_variables()
        # return the variable instance
        return self[alias]

    def create_categorical(self, categories, alias, name, multiple,
                           description='', notes=''):
        """
        Used to create new categorical variables using Crunchs's `case`
        function

        Will create either categorical variables or multiple response depending
        on the `multiple` parameter.
        """
        if multiple:
            return self.create_multiple_response(
                categories, alias=alias, name=name, description=description,
                notes=notes)
        else:
            return self.create_single_response(
                categories, alias=alias, name=name, description=description,
                notes=notes)

    def copy_variable(self, variable, name, alias):
        _subvar_alias = re.compile(r'.+_(\d+)$')

        def subrefs(_variable, _alias):
            # In the case of MR variables, we want the copies' subvariables
            # to have their aliases in the same pattern and order that the
            # parent's are, that is `parent_alias_#`.
            _subreferences = []
            for subvar in _variable.resource.subvariables.index.values():
                sv_alias = subvar['alias']
                match = _subvar_alias.match(sv_alias)
                if match:  # Does this var have the subvar pattern?
                    suffix = int(match.groups()[0], 10)  # Keep the position
                    sv_alias = subvar_alias(_alias, suffix)

                _subreferences.append({
                    'name': subvar['name'],
                    'alias': sv_alias
                })
            return _subreferences

        if variable.resource.body.get('derivation'):
            # We are dealing with a derived variable, we want the derivation
            # to be executed again instead of doing a `copy_variable`
            derivation = abs_url(variable.resource.body['derivation'],
                                 variable.resource.self)
            derivation.pop('references', None)
            payload = shoji_entity_wrapper({
                'name': name,
                'alias': alias,
                'derivation': derivation})

            if variable.type == _MR_TYPE:
                # We are re-executing a multiple_response derivation.
                # We need to update the complex `array` function expression
                # to contain the new suffixed aliases. Given that the map is
                # unordered, we have to iterated and find a name match.
                subvars = payload['body']['derivation']['args'][0]['args'][0]['map']
                subreferences = subrefs(variable, alias)
                for subref in subreferences:
                    for subvar_pos in subvars:
                        subvar = subvars[subvar_pos]
                        if subvar['references']['name'] == subref['name']:
                            subvar['references']['alias'] = subref['alias']
                            break
        else:
            payload = shoji_entity_wrapper({
                'name': name,
                'alias': alias,
                'derivation': {
                    'function': 'copy_variable',
                    'args': [{
                        'variable': variable.resource.self
                    }]
                }
            })
            if variable.type == _MR_TYPE:
                subreferences = subrefs(variable, alias)
                payload['body']['derivation']['references'] = {
                    'subreferences': subreferences
                }

        new_var = self.resource.variables.create(payload)
        # needed to update the variables collection
        self._reload_variables()
        # return an instance of Variable
        return self[new_var['body']['alias']]

    def combine_categories(self, variable, map, categories, missing=None, default=None,
                           name='', alias='', description=''):
        if not alias or not name:
            raise ValueError("Name and alias are required")
        if variable.type in _MR_TYPE:
            return self.combine_multiple_response(variable, map, categories, name=name,
                                                  alias=alias, description=description)
        else:
            return self.combine_categorical(variable, map, categories, missing, default,
                                            name=name, alias=alias, description=description)

    def combine_categorical(self, variable, map, categories=None, missing=None,
                            default=None, name='', alias='', description=''):
        """
        Create a new variable in the given dataset that is a recode
        of an existing variable
            map={
                1: (1, 2),
                2: 3,
                3: (4, 5)
            },
            default=9,
            missing=[-1, 9],
            categories={
                1: "low",
                2: "medium",
                3: "high",
                9: "no answer"
            },
            missing=9
        """
        if isinstance(variable, six.string_types):
            variable = self[variable]

        # TODO: Implement `default` parameter in Crunch API
        combinations = combinations_from_map(map, categories or {}, missing or [])
        payload = shoji_entity_wrapper({
            'name': name,
            'alias': alias,
            'description': description,
            'derivation': combine_categories_expr(
                variable.resource.self, combinations)
        })
        # this returns an entity
        new_var = self.resource.variables.create(payload)
        # needed to update the variables collection
        self._reload_variables()
        # at this point we are returning a Variable instance
        return self[new_var['body']['alias']]

    def combine_multiple_response(self, variable, map, categories=None, default=None,
                                  name='', alias='', description=''):
        """
        Creates a new variable in the given dataset that combines existing
        responses into new categorized ones
            map={
                1: 1,
                2: [2, 3, 4]
            },
            categories={
                1: "online",
                2: "notonline"
            }
        """
        if isinstance(variable, six.string_types):
            parent_alias = variable
            variable = self[variable]
        else:
            parent_alias = variable.alias

        # TODO: Implement `default` parameter in Crunch API
        responses = responses_from_map(variable, map, categories or {}, alias,
                                       parent_alias)
        payload = shoji_entity_wrapper({
            'name': name,
            'alias': alias,
            'description': description,
            'derivation': combine_responses_expr(
                variable.resource.self, responses)
        })
        new_var = self.resource.variables.create(payload)
        # needed to update the variables collection
        self._reload_variables()
        # return an instance of Variable
        return self[new_var['body']['alias']]

    def create_savepoint(self, description):
        """
        Creates a savepoint on the dataset.

        :param description:
            The description that should be given to the new savepoint. This
            function will not let you create a new savepoint with the same
            description as any other savepoint.
        """
        if len(self.resource.savepoints.index) > 0:
            if description in self.savepoint_attributes('description'):
                raise KeyError(
                    "A checkpoint with the description '{}' already"
                    " exists.".format(description)
                )

        self.resource.savepoints.create(shoji_entity_wrapper({'description': description}))

    def load_savepoint(self, description=None):
        """
        Load a savepoint on the dataset.

        :param description: default=None
            The description that identifies which savepoint to be loaded.
            When loading a savepoint, all savepoints that were saved after
            the loaded savepoint will be destroyed permanently.
        """
        if description is None:
            description = 'initial import'
        elif description not in self.savepoint_attributes('description'):
            raise KeyError(
                "No checkpoint with the description '{}'"
                " exists.".format(description)
            )

        revert = self.resource.savepoints.by('description').get(description).revert
        self.resource.session.post(revert)

    def savepoint_attributes(self, attrib):
        """
        Return list of attributes from the given dataset's savepoints.

        :param attrib:
            The attribute to be returned for each savepoint in the given
            dataset. Available attributes are:
                'creation_time'
                'description'
                'last_update'
                'revert'
                'user_name'
                'version'
        """
        svpoints = self.resource.savepoints
        if len(svpoints.index) != 0:
            attribs = [
                cp[attrib]
                for url, cp in six.iteritems(svpoints.index)
            ]
            return attribs
        return []

    def create_crunchbox(
            self, title='', header='', footer='', notes='',
            filters=None, variables=None, force=False, min_base_size=None,
            palette=None):
        """
        create a new boxdata entity for a CrunchBox.

        NOTE: new boxdata is only created when there is a new combination of
        where and filter data.

        Args:
            title       (str): Human friendly identifier
            notes       (str): Other information relevent for this CrunchBox
            header      (str): header information for the CrunchBox
            footer      (str): footer information for the CrunchBox
            filters    (list): list of filter names or `Filter` instances
            where      (list): list of variable aliases or `Variable` instances
                               If `None` all variables will be included.
            min_base_size (int): min sample size to display values in graph
            palette     dict : dict of colors as documented at docs.crunch.io
                i.e.
                {
                    "brand": ["#111111", "#222222", "#333333"],
                    "static_colors": ["#444444", "#555555", "#666666"],
                    "base": ["#777777", "#888888", "#999999"],
                    "category_lookup": {
                        "category name": "#aaaaaa",
                        "another category:": "bbbbbb"
                }

        Returns:
            CrunchBox (instance)
        """

        if filters:
            if not isinstance(filters, list):
                raise TypeError('`filters` argument must be of type `list`')

            # ensure we only have `Filter` instances
            filters = [
                f if isinstance(f, Filter) else self.filters[f]
                for f in filters
            ]

            if any(not f.is_public
                    for f in filters):
                raise ValueError('filters need to be public')

            filters = [
                {'filter': f.resource.self}
                for f in filters
            ]

        if variables:
            if not isinstance(variables, list):
                raise TypeError('`variables` argument must be of type `list`')

            # ensure we only have `Variable` Tuples
            # NOTE: if we want to check if variables are public we would have
            # to use Variable instances instead of their Tuple representation.
            # This would cause additional GET's
            variables = [
                var.shoji_tuple if isinstance(var, Variable)
                else self.resource.variables.by('alias')[var]
                for var in variables
            ]

            variables = dict(
                function='select',
                args=[
                    {'map': {
                        v.id: {'variable': v.entity_url}
                        for v in variables
                    }}
                ])

        if not title:
            title = 'CrunchBox for {}'.format(str(self))

        payload = shoji_entity_wrapper(dict(
            where=variables,
            filters=filters,
            force=force,
            title=title,
            notes=notes,
            header=header,
            footer=footer)
        )

        if min_base_size:
            payload['body'].setdefault('display_settings', {}).update(
                dict(minBaseSize=dict(value=min_base_size)))
        if palette:
            payload['body'].setdefault('display_settings', {}).update(
                dict(palette=palette))

        # create the boxdata
        self.resource.boxdata.create(payload)

        # NOTE: the entity from the response is a bit different compared to
        # others, i.e. no id, no delete method, different entity_url...
        # For now, return the shoji_tuple from the index
        for shoji_tuple in self.resource.boxdata.index.values():
            if shoji_tuple.metadata.title == title:
                return CrunchBox(shoji_tuple, self)

    def delete_crunchbox(self, **kwargs):
        """ deletes crunchboxes on matching kwargs """
        match = False
        for key in kwargs:
            if match:
                break
            for crunchbox in self.crunchboxes:
                attr = getattr(crunchbox, key, None)
                if attr and attr == kwargs[key]:
                    crunchbox.remove()
                    match = True
                    break

    def forks_dataframe(self):
        """
        Return a dataframe summarizing the forks on the dataset.

        :returns _forks : pandas.DataFrame
            A DataFrame representation of all attributes from all forks
            on the given dataset.
        """

        if len(self.resource.forks.index) == 0:
            return None
        else:
            _forks = pd.DataFrame(
                [fk for url, fk in six.iteritems(self.resource.forks.index)]
            )
            _forks = _forks[[
                'name',
                'description',
                'is_published',
                'owner_name',
                'current_editor_name',
                'creation_time',
                'modification_time',
                'id'
            ]]
            _forks['creation_time'] = pd.to_datetime(_forks['creation_time'])
            _forks['modification_time'] = pd.to_datetime(_forks['modification_time'])
            _forks.sort(columns='creation_time', inplace=True)

            return _forks

    def export(self, path, format='csv', filter=None, variables=None,
               hidden=False, options=None, metadata_path=None, timeout=None):
        """
        Downloads a dataset as CSV or as SPSS to the given path. This
        includes hidden variables.

        Dataset viewers can't download hidden variables so we default
        to False. Dataset editors will need to add hidden=True if they
        need this feature.

        By default, categories in CSV exports are provided as id's.
        """
        valid_options = ['use_category_ids', 'prefix_subvariables',
                         'var_label_field', 'missing_values']

        # Only CSV and SPSS exports are currently supported.
        if format not in ('csv', 'spss'):
            raise ValueError(
                'Invalid format %s. Allowed formats are: "csv" and "spss".'
                % format
            )

        if format == 'csv':
            # Default options for CSV exports.
            export_options = {'use_category_ids': True}
        else:
            # Default options for SPSS exports.
            export_options = {
                'prefix_subvariables': False,
                'var_label_field': 'description'
            }

        # Validate the user-provided export options.
        options = options or {}
        if not isinstance(options, dict):
            raise ValueError(
                'The options argument must be a dictionary.'
            )

        for k in options.keys():
            if k not in valid_options:
                raise ValueError(
                    'Invalid options for format "%s": %s.'
                    % (format, ','.join(k))
                )
        if 'var_label_field' in options \
                and not options['var_label_field'] in ('name', 'description'):
            raise ValueError(
                'The "var_label_field" export option must be either "name" '
                'or "description".'
            )

        # All good. Update the export options with the user-provided values.
        export_options.update(options)

        # the payload should include all hidden variables by default
        payload = {'options': export_options}

        # Option for exporting metadata as json
        if metadata_path is not None:
            metadata = self.resource.table['metadata']
            if variables is not None:
                if sys.version_info >= (3, 0):
                    metadata = {
                        key: value
                        for key, value in metadata.items()
                        if value['alias'] in variables
                    }
                else:
                    metadata = {
                        key: value
                        for key, value in metadata.iteritems()
                        if value['alias'] in variables
                    }
            with open(metadata_path, 'w+') as f:
                json.dump(metadata, f, sort_keys=True)

        # add filter to rows if passed
        if filter:
            if isinstance(filter, Filter):
                payload['filter'] = {'filter': filter.resource.self}
            else:
                payload['filter'] = process_expr(
                    parse_expr(filter), self.resource)

        # convert variable list to crunch identifiers
        if variables and isinstance(variables, list):
            id_vars = []
            for var in variables:
                id_vars.append(self[var].url)
            if len(id_vars) != len(variables):
                LOG.debug(
                    "Variables passed: %s Variables detected: %s"
                    % (variables, id_vars)
                )
                raise AttributeError("At least a variable was not found")
            # Now build the payload with selected variables
            payload['where'] = {
                'function': 'select',
                'args': [{
                    'map': {
                        x: {'variable': x} for x in id_vars
                    }
                }]
            }
        # hidden is mutually exclusive with
        # variables to include in the download
        if hidden and not variables:
            if not self.resource.body.permissions.edit:
                raise AttributeError("Only Dataset editors can export hidden variables")
            payload['where'] = {
                'function': 'select',
                'args': [{
                    'map': {
                        x: {'variable': x}
                        for x in self.resource.variables.index.keys()
                    }
                }]
            }

        progress_tracker = pycrunch.progress.DefaultProgressTracking(timeout)
        url = export_dataset(
            dataset=self.resource,
            options=payload,
            format=format,
            progress_tracker=progress_tracker
        )
        download_file(url, path)

    def exclude(self, expr=None):
        """
        Given a dataset object, apply an exclusion filter to it (defined as an
        expression string).

        If the `expr` parameter is None, an empty expression object is sent
        as part of the PATCH request, which effectively removes the exclusion
        filter (if any).

        Exclusion filters express logic that defines a set of rows that should be
        dropped from the dataset. The rows aren't permanently deleted---you can
        recover them at any time by removing the exclusion filter---but they are
        omitted from all views and calculations, as if they had been deleted.

        Note that exclusion filters work opposite from how "normal" filters work.
        That is, a regular filter expression defines the subset of rows to operate
        on: it says "keep these rows." An exclusion filter defines which rows to
        omit. Applying a filter expression as a query filter will have the
        opposite effect if applied as an exclusion. Indeed, applying it as both
        query filter and exclusion at the same time will result in 0 rows.
        """
        if isinstance(expr, six.string_types):
            expr_obj = parse_expr(expr)
            expr_obj = process_expr(expr_obj, self.resource)  # cause we need URLs
        elif expr is None:
            expr_obj = {}
        else:
            expr_obj = expr
        return self.resource.session.patch(
            self.resource.fragments.exclusion,
            data=json.dumps(dict(expression=expr_obj))
        )

    def get_exclusion(self):
        exclusion = self.resource.exclusion
        if 'body' not in exclusion:
            return None
        expr = exclusion['body'].get('expression')
        return prettify(expr, self) if expr else None

    def add_filter(self, name, expr, public=False):
        payload = shoji_entity_wrapper(dict(
            name=name,
            expression=process_expr(parse_expr(expr), self.resource),
            is_public=public))
        new_filter = self.resource.filters.create(payload)
        return self.filters[new_filter.body['name']]

    def add_deck(self, name, description="", public=False):
        payload = shoji_entity_wrapper(dict(
            name=name,
            description=description,
            is_public=public))
        new_deck = self.resource.decks.create(payload)
        return self.decks[new_deck.self.split('/')[-2]]

    def fork(self, description=None, name=None, is_published=False,
             preserve_owner=False, **kwargs):
        """
        Create a fork of ds and add virgin savepoint.

        :param description: str, default=None
            If given, the description to be applied to the fork. If not
            given the description will be copied from ds.
        :param name: str, default=None
            If given, the name to be applied to the fork. If not given a
            default name will be created which numbers the fork based on
            how many other forks there are on ds.
        :param is_published: bool, default=False
            If True, the fork will be visible to viewers of ds. If False it
            will only be viewable to editors of ds.
        :param preserve_owner: bool, default=False
            If True, the owner of the fork will be the same as the parent
            dataset. If the owner of the parent dataset is a Crunch project,
            then it will be preserved regardless of this parameter.

        :returns _fork: scrunch.datasets.BaseDataset
            The forked dataset.
        """
        nforks = len(self.resource.forks.index)
        if name is None:
            name = "FORK #{} of {}".format(nforks + 1, self.resource.body.name)
        if description is None:
            description = self.resource.body.description

        body = dict(
            name=name,
            description=description,
            is_published=is_published,
            **kwargs)

        if preserve_owner or '/api/projects/' in self.resource.body.owner:
            body['owner'] = self.resource.body.owner
        # not returning a dataset
        payload = shoji_entity_wrapper(body)
        _fork = self.resource.forks.create(payload).refresh()
        return BaseDataset(_fork)

    def merge(self, fork_id=None, autorollback=True):
        """
        :param fork_id: str or int
            can either be the fork id, name or its number as string or int

        :param autorollback: bool, default=True
            if True the original dataset is rolled back to the previous state
            in case of a merge conflict.
            if False the dataset and fork are beeing left 'dirty'
        """
        if isinstance(fork_id, int) or (
                isinstance(fork_id, six.string_types) and
                fork_id.isdigit()):
            fork_id = "FORK #{} of {}".format(fork_id, self.resource.body.name)

        elif fork_id is None:
            raise ValueError('fork id, name or number missing')

        fork_index = self.resource.forks.index

        forks = [f for f in fork_index
                 if fork_index[f].get('name') == fork_id or
                 fork_index[f].get('id') == fork_id]
        if len(forks) == 1:
            fork_url = forks[0]
        else:
            raise ValueError(
                "Couldn't find a (unique) fork. "
                "Please try again using its id")

        body = dict(
            dataset=fork_url,
            autorollback=autorollback)
        self.resource.actions.create(body)

    def delete_forks(self):
        """
        Deletes all the forks on the dataset. CANNOT BE UNDONE!
        """
        for fork in six.itervalues(self.resource.forks.index):
            fork.entity.delete()

    def create_multitable(self, name, template, is_public=False):
        """
        template: List of dictionaries with the following keys
        {"query": <query>, "transform": <transform>|optional}.
        A query is a variable or a function on a variable:
        {"query": bin(birthyr)}

        If transform is specified it must have the form
        {
            "query": var_x,
            "transform": {"categories": [
                {
                    "missing": false,  --> default: False
                    "hide": true,      --> default: True
                    "id": 1,
                    "name": "not asked"
                },
                {
                    "missing": false,
                    "hide": true,
                    "id": 4,
                    "name": "skipped"
                }
            ]}
        }
        """
        # build template payload
        parsed_template = []
        for q in template:
            # sometimes q is not a dict but simply a string, convert it to a dict
            if isinstance(q, str):
                q = {"query": q}
            as_json = {}
            parsed_q = process_expr(parse_expr(q['query']), self.resource)
            # wrap the query in a list of one dict element
            as_json['query'] = [parsed_q]
            if 'transform' in q.keys():
                as_json['transform'] = q['transform']
            parsed_template.append(as_json)
        payload = shoji_entity_wrapper(dict(
            name=name,
            is_public=is_public,
            template=parsed_template))
        new_multi = self.resource.multitables.create(payload)
        return self.multitables[new_multi.body['name']]

    def import_multitable(self, name, multi):
        """
        Copies a multitable from another Dataset into this one:
        As described at http://docs.crunch.io/#post176
        :name: Name of the new multitable
        :multi: Multitable instance to clone into this Dataset
        """
        payload = shoji_entity_wrapper(dict(
            name=name,
            multitable=multi.resource.self))
        self.resource.multitables.create(payload)
        return self.multitables[name]


class DatasetSubvariablesMixin(DatasetVariablesMixin):

    def _reload_variables(self):
        """
        Helper that takes care of updating self._vars on init and
        whenever the dataset adds a variable
        """
        self._vars = []
        self._catalog = {}
        if getattr(self.resource, 'subvariables', None):
            self._catalog = self.resource.subvariables
            self._vars = self._catalog.index.items()


class Variable(ReadOnly, DatasetSubvariablesMixin):
    """
    A pycrunch.shoji.Entity wrapper that provides variable-specific methods.
    DatasetSubvariablesMixin provides for subvariable interactions.
    """
    _MUTABLE_ATTRIBUTES = {'name', 'description',
                           'view', 'notes', 'format'}
    _IMMUTABLE_ATTRIBUTES = {'id', 'alias', 'type', 'discarded'}
    # We won't expose owner and private
    # categories in immutable. IMO it should be handled separately
    _ENTITY_ATTRIBUTES = _MUTABLE_ATTRIBUTES | _IMMUTABLE_ATTRIBUTES
    _OVERRIDDEN_ATTRIBUTES = {'categories'}

    CATEGORICAL_TYPES = {'categorical', 'multiple_response', 'categorical_array'}

    def __init__(self, var_tuple, dataset):
        """
        :param var_tuple: A Shoji Tuple for a dataset variable
        :param dataset: a Dataset object instance
        """
        self.shoji_tuple = var_tuple
        self.is_instance = False
        self._resource = None
        self.url = var_tuple.entity_url
        self.dataset = dataset
        self._reload_variables()

    @property
    def resource(self):
        if not self.is_instance:
            self._resource = self.shoji_tuple.entity
            self.is_instance = True
        return self._resource

    def __getattr__(self, item):
        # don't access self.resource unless necessary
        if hasattr(self.shoji_tuple, item):
            return self.shoji_tuple[item]
        if item in self._ENTITY_ATTRIBUTES - self._OVERRIDDEN_ATTRIBUTES:
            try:
                return self.resource.body[item]  # Has to exist
            except KeyError:
                raise AttributeError("Variable does not have attribute %s" % item)
        return super(Variable, self).__getattribute__(item)

    def edit(self, **kwargs):
        for key in kwargs:
            if key not in self._MUTABLE_ATTRIBUTES:
                raise AttributeError("Can't edit attribute %s of variable %s" % (
                    key, self.name
                ))
        return self.resource.edit(**kwargs)

    def __repr__(self):
        return "<Variable: name='{}'; id='{}'>".format(self.name, self.id)

    def __str__(self):
        return self.name

    @property
    def categories(self):
        if self.resource.body['type'] not in self.CATEGORICAL_TYPES:
            raise TypeError("Variable of type %s do not have categories" % self.resource.body.type)
        return CategoryList._from(self.resource)

    def hide(self):
        self.resource.edit(discarded=True)

    def unhide(self):
        self.resource.edit(discarded=False)

    def integrate(self):
        if self.derived:
            self.resource.edit(derived=False)

    def edit_categorical(self, categories, rules):
        # validate rules and categories are same size
        _validate_category_rules(categories, rules)
        args = [{
            'column': [c['id'] for c in categories],
            'type': {
                'value': {
                    'class': 'categorical',
                    'categories': categories}}}]
        # build the expression
        more_args = []
        for rule in rules:
            more_args.append(parse_expr(rule))
        # get dataset and build the expression
        more_args = process_expr(more_args, self.dataset)
        # epression value building
        expr = dict(function='case', args=args + more_args)
        payload = shoji_entity_wrapper(dict(expr=expr))
        # patch the variable with the new payload
        resp = self.resource.patch(payload)
        self._reload_variables()
        return resp

    def edit_derived(self, variable, mapper):
        raise NotImplementedError("Use edit_combination")

    def move(self, path, position=-1, before=None, after=None):
        self.dataset.order.place(self, path, position=position,
                                 before=before, after=after)
