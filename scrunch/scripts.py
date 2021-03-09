# coding: utf-8

import json
import pycrunch


class ScriptExecutionError(Exception):
    def __init__(self, client_error):
        self.client_error = client_error
        self.resolutions = client_error.args[2]["resolutions"]

    def __repr__(self):
        return json.dumps(self.resolutions, indent=2)


class DatasetScripts:
    def __init__(self, dataset_resource):
        """
        :param dataset_resource: Pycrunch Entity for the dataset.
        """
        self.dataset_resource = dataset_resource

    def execute(self, script_body):
        try:
            self.dataset_resource.scripts.create({
                'element': 'shoji:entity',
                'body': {"body": script_body},
            })
        except pycrunch.ClientError as err:
            if err.status_code == 400:  # Script validation
                raise ScriptExecutionError(err)
            raise err  # 404 or something else

    def collapse(self):
        """
        When a dataset has too many scripts. Collapse will concatenate
        all the previously executed scripts into one the first. It will delete
        all savepoints associated with the collapsed scripts.
        """
        self.dataset_resource.scripts.collapse.post({})

    def all(self):
        scripts_index = self.dataset_resource.scripts.index
        scripts = []
        for s_url, s in scripts_index.items():
            scripts.append(s.entity)
        scripts = sorted(scripts, key=lambda s: s.body["creation_time"])
        return scripts

    def revert_to(self, id=None, script_number=None):
        all_scripts = self.all()
        if script_number is not None:
            script = all_scripts[script_number]
        elif id is not None:
            # We have to do this because currently the API does not expose the
            # script ID directly.
            import pdb;pdb.set_trace()
            script = [s for s in all_scripts if "scripts/{}/".format(id) in s.self][0]
        else:
            raise ValueError("Must indicate either ID or script number")

        script.revert.post({})
