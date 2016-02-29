# ElasticArmor | (c) 2016 NETWAYS GmbH | GPLv2+

import socket
import time
import threading

import ldap
import requests

from elasticarmor.util import format_ldap_error, format_elasticsearch_error, pattern_match
from elasticarmor.util.elastic import ElasticSearchError, ElasticRole
from elasticarmor.util.mixins import LoggingAware
from elasticarmor.util.rwlock import ReadWriteLock, Protector

__all__ = ['AuthorizationError', 'Auth', 'Client', 'RestrictionError', 'Restriction', 'LdapBackend',
           'LdapUserBackend', 'LdapUsergroupBackend', 'ElasticsearchRoleBackend']

CACHE_INVALIDATION_INTERVAL = 900  # Seconds


class AuthorizationError(Exception):
    """Base class for all authorization related exceptions."""
    pass


class Auth(LoggingAware, object):
    """Auth manager class for everything involved in authentication and authorization."""

    def __init__(self, settings):
        self.allow_from = settings.allow_from
        self.role_backend = settings.role_backend
        self.group_backend = settings.group_backend

    def authenticate(self, client, populate=True):
        """Authenticate the given client and return whether it succeeded or not."""
        if client.username is None or client.password is None:
            # In case we have no authentication credentials check if access by ip[:port] is permitted
            allowed_ports = self.allow_from.get(client.address, [])
            if allowed_ports is not None and client.port not in allowed_ports:
                return False
            else:
                default_timeout = socket.getdefaulttimeout()
                socket.setdefaulttimeout(2)

                try:
                    hostname = socket.gethostbyaddr(client.address)[0]
                except IOError:
                    hostname = client.address
                finally:
                    socket.setdefaulttimeout(default_timeout)

                client.name = hostname if allowed_ports is None else '%s:%u' % (hostname, client.port)
        else:
            client.name = client.username

        if populate:
            self.populate(client)

        client.authenticated = True
        return True

    def populate(self, client):
        """Populate the group and role memberships of the given client."""
        if self.group_backend is not None and client.username is not None:
            self.log.debug('Fetching group memberships for client "%s"...', client)

            try:
                client.groups = self.group_backend.get_group_memberships(client)
            except ldap.LDAPError as error:
                self.log.error('Failed to fetch ldap group memberships for client "%s". %s.',
                               client, format_ldap_error(error))
            else:
                self.log.debug('Client "%s" is a member of the following groups: %s',
                               client, ', '.join(client.groups) or 'None')
        else:
            client.groups = []

        if client.groups is not None:
            self.log.debug('Fetching role memberships for client "%s"...', client)

            try:
                client.roles = self.role_backend.get_role_memberships(client)
            except requests.RequestException as error:
                self.log.error('Failed to fetch Elasticsearch role memberships for client "%s". Error: %s',
                               client, format_elasticsearch_error(error))
            else:
                self.log.debug('Client "%s" is a member of the following roles: %s',
                               client, ', '.join(r.name for r in client.roles) or 'None')


class Client(object):
    """An object representing a client who is sending a request."""

    def __init__(self, address, port):
        self.address = address
        self.port = port

        self.name = None
        self.authenticated = False
        self.username = None
        self.password = None
        self.groups = None
        self.roles = None

    def __str__(self):
        """Return a human readable string representation for this client.
        That's either the name, username or the address and port concatenated with a colon."""
        if self.name is not None:
            return self.name

        if self.username is not None:
            return self.username

        return '%s:%u' % (self.address, self.port)

    def can_read(self, index, document=None, field=None):
        """Return whether this client is permitted to read the given entities."""
        return any(restriction.permits_read(index, document, field)
                   for role in self.roles
                   for restriction in role.restrictions)

    def can_write(self, index, document=None, field=None):
        """Return whether this client is permitted to write the given entities."""
        return any(restriction.permits_write(index, document, field)
                   for role in self.roles
                   for restriction in role.restrictions)


class RestrictionError(AuthorizationError):
    """Raised by class Restriction in case of an error."""
    pass


class Restriction(object):
    """Restriction object that represents a configured client restriction."""

    def __init__(self, restriction):
        self._parsed = False
        self._read_only = None
        self._index_patterns = []
        self._index_includes = []
        self._index_excludes = []
        self._type_patterns = []
        self._type_includes = []
        self._type_excludes = []
        self._field_patterns = []
        self._field_includes = []
        self._field_excludes = []

        self.raw_restriction = restriction

    def __str__(self):
        return self.raw_restriction

    def _parse_restriction(self):
        if self._parsed:
            return

        parts = self.raw_restriction.split('/')
        if not 1 > len(parts) <= 4:
            raise RestrictionError('Invalid restriction "{0}"'.format(self.raw_restriction))

        for index_pattern in (v.strip() for v in parts[1].split(',')):
            if index_pattern.startswith('-'):
                self._index_excludes.append(index_pattern[1:])
            elif index_pattern.startswith('+'):
                self._index_includes.append(index_pattern[1:])
            else:
                self._index_patterns.append(index_pattern)

        if not self._index_patterns:
            raise RestrictionError(
                'Restriction "{0}" does not provide any index patterns'.format(self.raw_restriction))

        if len(parts) > 2:
            for type_pattern in (v.strip() for v in parts[2].split(',')):
                if type_pattern.startswith('-'):
                    self._type_excludes.append(type_pattern[1:])
                elif type_pattern.startswith('+'):
                    self._type_includes.append(type_pattern[1:])
                else:
                    self._type_patterns.append(type_pattern)

            if not self._type_patterns:
                raise RestrictionError(
                    'Restriction "{0}" does not provide any document type patterns'.format(self.raw_restriction))

        if len(parts) > 3:
            for field_pattern in (v.strip() for v in parts[3].split(',')):
                if field_pattern.startswith('-'):
                    self._field_excludes.append(field_pattern[1:])
                elif field_pattern.startswith('+'):
                    self._field_includes.append(field_pattern[1:])
                else:
                    self._field_patterns.append(field_pattern)

            if not self._field_patterns:
                raise RestrictionError(
                    'Restriction "{0}" does not provide any document field patterns'.format(self.raw_restriction))

        self._read_only = parts[0] == 'read'
        self._parsed = True

    def _apply_restriction(self, subject, patterns, includes, excludes):
        if not any(pattern_match(pattern, subject) for pattern in patterns):
            return False

        if not any(pattern_match(pattern, subject) for pattern in excludes):
            return True

        return any(pattern_match(pattern, subject) for pattern in includes)

    def _check_permission(self, index, document, field):
        if field is None and self._field_patterns or document is None and self._type_patterns:
            # If it's not a document or field request and this restriction covers those access must be denied
            return False
        elif field is not None and not self._field_patterns or document is not None and not self._type_patterns:
            return False  # It's the same the other way round

        if not self._apply_restriction(index, self._index_patterns, self._index_includes, self._index_excludes):
            return False
        elif document is None:
            return True

        if not self._apply_restriction(document, self._type_patterns, self._type_includes, self._type_excludes):
            return False
        elif field is None:
            return True

        return self._apply_restriction(field, self._field_patterns, self._field_includes, self._field_excludes)

    def permits_read(self, index, document=None, field=None):
        """Return whether read access to the given entities is permitted."""
        self._parse_restriction()
        return self._check_permission(index, document, field)

    def permits_write(self, index, document=None, field=None):
        """Return whether write access to the given entities is permitted."""
        self._parse_restriction()
        if self._read_only:
            return False

        return self._check_permission(index, document, field)


class LdapBackend(object):
    """Base class for all LDAP related backends.

    It provides connection handling and basic search functionality.
    All connection related operations are thread-safe.
    """

    def __init__(self, settings):
        self.__local = threading.local()

        self.url = settings.ldap_url
        self.bind_dn = settings.ldap_bind_dn
        self.bind_pw = settings.ldap_bind_pw
        self.root_dn = settings.ldap_root_dn

    @property
    def _local(self):
        try:
            self.__local.initialized
        except AttributeError:
            self.__local.bound = False
            self.__local.connection = None
            self.__local.initialized = True

        return self.__local

    @property
    def connection(self):
        """Return the connection to the LDAP server.
        Initializes the connection lazily if necessary."""
        if self._local.connection is None:
            self._local.connection = ldap.initialize(self.url)

        return self._local.connection

    def bind(self, dn=None, password=None):
        """Send a simple bind request with the given DN and password to the LDAP server.
        If neither of those is given, the configured bind_dn and bind_pw will be used."""
        if not self._local.bound:
            if dn is None and password is None:
                dn, password = self.bind_dn, self.bind_pw

            if dn is not None and password is not None:
                self.connection.simple_bind_s(dn, password)
                self._local.bound = True

    def unbind(self):
        """Send a unbind request to the LDAP server and close the connection."""
        if self._local.bound:
            self.connection.unbind()
            self._local.bound = False

        self._local.connection = None

    def search(self, base_dn, search_filter, attributes=None):
        """Send a search request to the LDAP server and return the result.

        The given filter must be a shallow dictionary and is sent as AND filter. You may pass a list of attributes
        you want to retrieve or an empty list to return none, otherwise all attributes are retrieved."""

        if len(search_filter) > 1:
            search_string = '(&(' + ')('.join('{0}={1}'.format(k, v) for k, v in search_filter.iteritems()) + '))'
        elif search_filter:
            search_string = '({0}={1})'.format(*search_filter.items()[0])
        else:
            search_string = '(objectClass=*)'

        attrsonly = 0
        if attributes is not None and not attributes:
            # This is actually quite dirty as I was not able to find a way to select "nothing". This will now
            # only omit the values of all attributes but the attribute names itself are still transmitted
            attrsonly = 1

        return self.connection.search_s(base_dn, ldap.SCOPE_SUBTREE, search_string, attributes, attrsonly)


class LdapUserBackend(LdapBackend):
    """LDAP backend class providing user account related operations."""

    def __init__(self, settings):
        super(LdapUserBackend, self).__init__(settings)

        self.user_base_dn = settings.ldap_user_base_dn
        self.user_object_class = settings.ldap_user_object_class
        self.user_name_attribute = settings.ldap_user_name_attribute

    def fetch_user_dn(self, user):
        """Fetch and return the DN of the given user.
        Raises either ldap.NO_RESULTS_RETURNED if no DN could be found or ldap.LDAPError if multiple DNs were found."""
        user_filter = {'objectClass': self.user_object_class, self.user_name_attribute: user}
        result = self.search(self.user_base_dn, user_filter, [])
        if not result:
            raise ldap.NO_RESULTS_RETURNED({'desc': 'No DN found for user {0}'.format(user)})
        elif len(result) > 1:
            raise ldap.LDAPError({'desc': 'Multiple DNs found for user {0}'.format(user)})

        return result[0][0]


class LdapUsergroupBackend(LdapUserBackend):
    """LDAP backend class providing usergroup related operations."""

    def __init__(self, settings):
        super(LdapUsergroupBackend, self).__init__(settings)
        self._group_cache = {}
        self._cache_lock = ReadWriteLock()

        self.group_base_dn = settings.ldap_group_base_dn
        self.group_object_class = settings.ldap_group_object_class
        self.group_name_attribute = settings.ldap_group_name_attribute
        self.group_membership_attribute = settings.ldap_group_membership_attribute

    def clear_cache(self):
        """Clear the internal group membership cache."""
        with self._cache_lock.writeContext:
            self._group_cache.clear()

    @Protector('_cache_lock')
    def get_group_memberships(self, client):
        """Fetch and return all usergroups the given client is a member of."""
        membership_cache = self._group_cache.get(client.name)
        now = time.time()

        if membership_cache is not None and membership_cache['expires'] > now:
            memberships = membership_cache['memberships']
        else:
            with self._cache_lock.writeContext:
                self.bind()
                group_filter = {'objectClass': self.group_object_class,
                                self.group_membership_attribute: self.fetch_user_dn(client.name)}
                results = self.search(self.group_base_dn, group_filter, [self.group_name_attribute])
                memberships = []
                for result in (r for r in results if self.group_name_attribute in r[1]):
                    memberships.extend(result[1][self.group_name_attribute])
                self._group_cache[client.name] = {
                    'memberships': memberships,
                    'expires': now + CACHE_INVALIDATION_INTERVAL
                }
                self.unbind()

        return memberships


class ElasticsearchRoleBackend(LoggingAware, object):
    """Elasticsearch backend class providing role related operations."""

    def __init__(self, settings):
        self.connection = settings.elasticsearch

    def get_role_memberships(self, client):
        """Fetch and return all roles the given client is a member of."""
        request = ElasticRole.search(client.name, client.groups)
        request.params['size'] = 1000  # If you know how to express "unlimited", feel free to change this!

        response = self.connection.process(request)
        if response is None:
            return

        response.raise_for_status()
        result = response.json()

        roles = []
        for hit in result.get('hits', {}).get('hits', []):
            try:
                role = ElasticRole.from_search_result(hit)
            except ElasticSearchError as error:
                self.log.warning('Failed to create role from search result. An error occurred: %s', error)
            else:
                role.restrictions = [Restriction(r) for r in role.restrictions]
                roles.append(role)

        return roles
