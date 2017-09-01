# (C) Datadog, Inc. 2010-2016
# All rights reserved
# Licensed under Simplified BSD License (see LICENSE)

# stdlib
from urlparse import urljoin
from urllib import quote

# 3rd party
import requests

# project
from checks import AgentCheck
from util import headers

class CouchDb(AgentCheck):

    TIMEOUT = 5
    SERVICE_CHECK_NAME = 'couchdb.can_connect'
    SOURCE_TYPE_NAME = 'couchdb'

    def __init__(self, name, init_config, agentConfig, instances=None):
        AgentCheck.__init__(self, name, init_config, agentConfig, instances)
        self.checker = None

    def get(self, url, instance, service_check_tags, run_check=False):
        "Hit a given URL and return the parsed json"
        self.log.debug('Fetching CouchDB stats at url: %s' % url)

        auth = None
        if 'user' in instance and 'password' in instance:
            auth = (instance['user'], instance['password'])

        # Override Accept request header so that failures are not redirected to the Futon web-ui
        request_headers = headers(self.agentConfig)
        request_headers['Accept'] = 'text/json'


        try:
            r = requests.get(url, auth=auth, headers=request_headers,
                         timeout=int(instance.get('timeout', self.TIMEOUT)))
            r.raise_for_status()
            if run_check:
                self.service_check(self.SERVICE_CHECK_NAME, AgentCheck.OK,
                    tags=service_check_tags,
                    message='Connection to %s was successful' % url)
        except requests.exceptions.Timeout as e:
            self.service_check(self.SERVICE_CHECK_NAME, AgentCheck.CRITICAL,
                tags=service_check_tags, message="Request timeout: {0}, {1}".format(url, e))
            raise
        except requests.exceptions.HTTPError as e:
            self.service_check(self.SERVICE_CHECK_NAME, AgentCheck.CRITICAL,
                tags=service_check_tags, message=str(e.message))
            raise
        except Exception as e:
            self.service_check(self.SERVICE_CHECK_NAME, AgentCheck.CRITICAL,
                tags=service_check_tags, message=str(e))
            raise
        return r.json()

    def check(self, instance):
        server = self.get_server(instance)
        if self.checker is None:
            name = instance.get('name', server)
            version = self.get(self.get_server(instance), instance, ["instance:{0}".format(name)], True)['version']
            if version.startswith('1.'):
                self.checker = CouchDB1(self)
            elif version.startswith('2.'):
                self.checker = CouchDB2(self)
            else:
                raise Exception("Unkown version {0}".format(version))

        self.checker.check(instance)

    def get_server(self, instance):
        server = instance.get('server', None)
        if server is None:
            raise Exception("A server must be specified")
        return server

class CouchDB1:
    """Extracts stats from CouchDB via its REST API
    http://wiki.apache.org/couchdb/Runtime_Statistics
    """
    MAX_DB = 50

    def __init__(self, agent_check):
        self.db_blacklist = {}
        self.agent_check = agent_check
        self.gauge = agent_check.gauge

    def _create_metric(self, data, tags=None):
        overall_stats = data.get('stats', {})
        for key, stats in overall_stats.items():
            for metric, val in stats.items():
                if val['current'] is not None:
                    metric_name = '.'.join(['couchdb', key, metric])
                    self.gauge(metric_name, val['current'], tags=tags)

        for db_name, db_stats in data.get('databases', {}).items():
            for name, val in db_stats.items():
                if name in ['doc_count', 'disk_size'] and val is not None:
                    metric_name = '.'.join(['couchdb', 'by_db', name])
                    metric_tags = list(tags)
                    metric_tags.append('db:%s' % db_name)
                    self.gauge(metric_name, val, tags=metric_tags, device_name=db_name)

    def check(self, instance):
        server = self.agent_check.get_server(instance)
        tags = ['instance:%s' % server]
        data = self.get_data(server, instance, tags)
        self._create_metric(data, tags=tags)

    def get_data(self, server, instance, tags):
        # The dictionary to be returned.
        couchdb = {'stats': None, 'databases': {}}

        # First, get overall statistics.
        endpoint = '/_stats/'

        url = urljoin(server, endpoint)

        overall_stats = self.agent_check.get(url, instance, tags, True)

        # No overall stats? bail out now
        if overall_stats is None:
            raise Exception("No stats could be retrieved from %s" % url)

        couchdb['stats'] = overall_stats

        # Next, get all database names.
        endpoint = '/_all_dbs/'

        url = urljoin(server, endpoint)

        # Get the list of whitelisted databases.
        db_whitelist = instance.get('db_whitelist')
        self.db_blacklist.setdefault(server,[])
        self.db_blacklist[server].extend(instance.get('db_blacklist',[]))
        whitelist = set(db_whitelist) if db_whitelist else None
        databases = set(self.agent_check.get(url, instance, tags)) - set(self.db_blacklist[server])
        databases = databases.intersection(whitelist) if whitelist else databases

        if len(databases) > self.MAX_DB:
            self.warning('Too many databases, only the first %s will be checked.' % self.MAX_DB)
            databases = list(databases)[:self.MAX_DB]

        for dbName in databases:
            url = urljoin(server, quote(dbName, safe = ''))
            try:
                db_stats = self.agent_check.get(url, instance, tags)
            except requests.exceptions.HTTPError as e:
                couchdb['databases'][dbName] = None
                if (e.response.status_code == 403) or (e.response.status_code == 401):
                    self.db_blacklist[server].append(dbName)
                    self.warning('Database %s is not readable by the configured user. It will be added to the blacklist. Please restart the agent to clear.' % dbName)
                    del couchdb['databases'][dbName]
                    continue
            if db_stats is not None:
                couchdb['databases'][dbName] = db_stats
        return couchdb

class CouchDB2:

    def __init__(self, agent_check):
        self.agent_check = agent_check
        self.gauge = agent_check.gauge

    def _build_metrics(self, data, tags, prefix = 'couchdb'):
        for key, value in data.items():
            if "type" in value:
                if value["type"] == "histogram":
                    for metric, value in value["value"].items():
                        if metric == "histogram":
                            continue
                        elif metric == "percentile":
                            for pair in value:
                                self.gauge("{0}.{1}.percentile.{2}".format(prefix, key, pair[0]), pair[1], tags=tags)
                        else:
                            self.gauge("{0}.{1}.{2}".format(prefix, key, metric), value, tags=tags)
                else:
                    self.gauge("{0}.{1}".format(prefix, key), value["value"], tags=tags)
            elif type(value) is dict:
                self._build_metrics(value, tags, "{0}.{1}".format(prefix, key))


    def _build_db_metrics(self, data, tags):
        for key, value in data['sizes'].items():
            self.gauge("couchdb.by_db.{0}_size".format(key), value, tags)

        for key in ['purge_seq', 'doc_del_count', 'doc_count']:
            self.gauge("couchdb.by_db.{0}".format(key), data[key], tags)

    def check(self, instance):
        server = self.agent_check.get_server(instance)

        name = instance.get('name', None)
        if name is None:
            raise Exception("At least one name is required")

        tags = ["instance:{0}".format(name)]
        self._build_metrics(self._get_node_stats(server, name, instance, tags), tags)

        db_whitelist = instance.get('db_whitelist', None)
        for db in self.agent_check.get(urljoin(server, "/_all_dbs"), instance, tags):
            if db_whitelist is None or db in db_whitelist:
                tags = ["instance:{0}".format(name), "db:{0}".format(db)]
                self._build_db_metrics(self.agent_check.get(urljoin(server, db), instance, tags), tags)

    def _get_node_stats(self, server, name, instance, tags):
        url = urljoin(server, "/_node/{}/_stats".format(name))

        # Fetch initial stats and capture a service check based on response.
        stats = self.agent_check.get(url, instance, tags, True)

        # No overall stats? bail out now
        if stats is None:
            raise Exception("No stats could be retrieved from %s" % url)

        return stats
