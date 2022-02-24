"""
Tibero database backend for Django.

This adpator requires pyodbc module for connecting database.
"""
from __future__ import unicode_literals

import datetime
import decimal
import os
import platform
import sys
import warnings
import codecs

from django.conf import settings
from django.db import utils
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.backends.base.validation import BaseDatabaseValidation
from django.utils import six, timezone
from django.utils.deprecation import RemovedInDjango30Warning
from django.utils.duration import duration_string
from django.utils.encoding import force_bytes, force_text
from django.utils.functional import cached_property


def encode_connection_string(fields):
    return ';'.join('%s=%s' % (k, encode_value(v)) for k, v in fields.items())

def encode_value(v):
    if ';' in v or v.strip(' ').startswith('{'):
        return '{%s}' % (v.replace('}', '}}'),)
    return v

def _setup_environment(environ):
    # Cygwin requires some special voodoo to set the environment variables
    # properly so that Tibero will see them.
    if platform.system().upper().startswith('CYGWIN'):
        try:
            import ctypes
        except ImportError as e:
            from django.core.exceptions import ImproperlyConfigured
            raise ImproperlyConfigured("Error loading ctypes: %s; "
                                       "the Tibero backend requires ctypes to "
                                       "operate correctly under Cygwin." % e)
        kernel32 = ctypes.CDLL('kernel32')
        for name, value in environ:
            kernel32.SetEnvironmentVariableA(name, value)
    else:
        os.environ.update(environ)

_setup_environment([
    # Tibero takes client-side character set encoding from the environment which used in ODBC Driver.
    ('TB_NLS_LANG', 'MSWIN949'),
])


try:
    import pyodbc as Database
except ImportError as e:
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured("Error loading pyodbc module: %s" % e)


from .client import DatabaseClient                          # NOQA isort:skip
from .creation import DatabaseCreation                      # NOQA isort:skip
from .features import DatabaseFeatures                      # NOQA isort:skip
from .introspection import DatabaseIntrospection            # NOQA isort:skip
from .operations import DatabaseOperations                  # NOQA isort:skip
from .schema import DatabaseSchemaEditor                    # NOQA isort:skip
from .utils import Tibero_datetime, convert_unicode         # NOQA isort:skip

DatabaseError = Database.DatabaseError
IntegrityError = Database.IntegrityError


class _UninitializedOperatorsDescriptor(object):

    def __get__(self, instance, cls=None):
        # If connection.operators is looked up before a connection has been
        # created, transparently initialize connection.operators to avert an
        # AttributeError.
        if instance is None:
            raise AttributeError("operators not available as class attribute")
        # Creating a cursor will initialize the operators.
        instance.cursor().close()
        return instance.__dict__['operators']


class DatabaseWrapper(BaseDatabaseWrapper):
    vendor = 'Tibero'

    # This dictionary maps Field objects to their associated Tibero column
    # types, as strings. 
    data_types = {
        'AutoField': 'NUMBER(11)',
        'BigAutoField': 'NUMBER(19)',
        'BinaryField': 'BLOB',
        'BooleanField': 'NUMBER(1)',
        'CharField': 'NVARCHAR2(%(max_length)s)',
        'CommaSeparatedIntegerField': 'VARCHAR2(%(max_length)s)',
        'DateField': 'DATE',
        'DateTimeField': 'TIMESTAMP',
        'DecimalField': 'NUMBER(%(max_digits)s, %(decimal_places)s)',
        'DurationField': 'INTERVAL DAY(9) TO SECOND(6)',
        'FileField': 'NVARCHAR2(%(max_length)s)',
        'FilePathField': 'NVARCHAR2(%(max_length)s)',
        'FloatField': 'DOUBLE PRECISION',
        'IntegerField': 'NUMBER(11)',
        'BigIntegerField': 'NUMBER(19)',
        'IPAddressField': 'VARCHAR2(15)',
        'GenericIPAddressField': 'VARCHAR2(39)',
        'NullBooleanField': 'NUMBER(1)',
        'OneToOneField': 'NUMBER(11)',
        'PositiveIntegerField': 'NUMBER(11)',
        'PositiveSmallIntegerField': 'NUMBER(11)',
        'SlugField': 'NVARCHAR2(%(max_length)s)',
        'SmallIntegerField': 'NUMBER(11)',
        'TextField': 'NCLOB',
        'TimeField': 'TIMESTAMP',
        'URLField': 'VARCHAR2(%(max_length)s)',
        'UUIDField': 'VARCHAR2(32)',
    }
    data_type_check_constraints = {
        'BooleanField': '%(qn_column)s IN (0,1)',
        'NullBooleanField': '(%(qn_column)s IN (0,1)) OR (%(qn_column)s IS NULL)',
        'PositiveIntegerField': '%(qn_column)s >= 0',
        'PositiveSmallIntegerField': '%(qn_column)s >= 0',
    }

    operators = _UninitializedOperatorsDescriptor()

    _standard_operators = {
        'exact': '= %s',
        'iexact': '= UPPER(%s)',
        'contains': "LIKE TRANSLATE(%s USING NCHAR_CS) ESCAPE TRANSLATE('\\' USING NCHAR_CS)",
        'icontains': "LIKE UPPER(TRANSLATE(%s USING NCHAR_CS)) ESCAPE TRANSLATE('\\' USING NCHAR_CS)",
        'gt': '> %s',
        'gte': '>= %s',
        'lt': '< %s',
        'lte': '<= %s',
        'startswith': "LIKE TRANSLATE(%s USING NCHAR_CS) ESCAPE TRANSLATE('\\' USING NCHAR_CS)",
        'endswith': "LIKE TRANSLATE(%s USING NCHAR_CS) ESCAPE TRANSLATE('\\' USING NCHAR_CS)",
        'istartswith': "LIKE UPPER(TRANSLATE(%s USING NCHAR_CS)) ESCAPE TRANSLATE('\\' USING NCHAR_CS)",
        'iendswith': "LIKE UPPER(TRANSLATE(%s USING NCHAR_CS)) ESCAPE TRANSLATE('\\' USING NCHAR_CS)",
    }

    _likec_operators = _standard_operators.copy()
    _likec_operators.update({
        'contains': "LIKEC %s ESCAPE '\\'",
        'icontains': "LIKEC UPPER(%s) ESCAPE '\\'",
        'startswith': "LIKEC %s ESCAPE '\\'",
        'endswith': "LIKEC %s ESCAPE '\\'",
        'istartswith': "LIKEC UPPER(%s) ESCAPE '\\'",
        'iendswith': "LIKEC UPPER(%s) ESCAPE '\\'",
    })

    # The patterns below are used to generate SQL pattern lookup clauses when
    # the right-hand side of the lookup isn't a raw string (it might be an expression
    # or the result of a bilateral transformation).
    # In those cases, special characters for LIKE operators (e.g. \, *, _) should be
    # escaped on database side.
    #
    # Note: we use str.format() here for readability as '%' is used as a wildcard for
    # the LIKE operator.
    pattern_esc = r"REPLACE(REPLACE(REPLACE({}, '\', '\\'), '%%', '\%%'), '_', '\_')"
    _pattern_ops = {
        'contains': "'%%' || {} || '%%'",
        'icontains': "'%%' || UPPER({}) || '%%'",
        'startswith': "{} || '%%'",
        'istartswith': "UPPER({}) || '%%'",
        'endswith': "'%%' || {}",
        'iendswith': "'%%' || UPPER({})",
    }

    _standard_pattern_ops = {k: "LIKE TRANSLATE( " + v + " USING NCHAR_CS)"
                                " ESCAPE TRANSLATE('\\' USING NCHAR_CS)"
                             for k, v in _pattern_ops.items()}
    _likec_pattern_ops = {k: "LIKEC " + v + " ESCAPE '\\'"
                          for k, v in _pattern_ops.items()}

    Database = Database
    SchemaEditorClass = DatabaseSchemaEditor

    def __init__(self, *args, **kwargs):
        self.client_class = DatabaseClient
        self.creation_class = DatabaseCreation
        self.features_class = DatabaseFeatures
        self.introspection_class = DatabaseIntrospection
        self.ops_class = DatabaseOperations
        self.validation_class = BaseDatabaseValidation
        use_returning_into = False
        self.features_class.can_return_id_from_insert = use_returning_into
        super(DatabaseWrapper, self).__init__(*args, **kwargs)


    def get_connection_params(self):
        settings_dict = self.settings_dict
        if settings_dict['NAME'] == '':
            from django.core.exception import ImproperlyConfigured
            raise ImproperlyConfigured("improper configured")
        conn_params = settings_dict.copy()
        if conn_params['NAME'] is None:
            conn_params['NAME'] = 'master' 
        return conn_params

    def get_new_connection(self, conn_params):
        charset = 'utf8'
        database = conn_params['NAME']
        host = conn_params.get('HOST', 'localhost')
        user = conn_params.get('USER', None)
        password = conn_params.get('PASSWORD', None)
        port = conn_params.get('PORT', None)

        options = conn_params.get('OPTIONS', {})
        dsn = options.get('dsn', None)
        cstr_parts = {}
        cstr_parts['DSN'] = dsn
        cstr_parts['UID'] = user
        cstr_parts['PWD'] = password
        cstr_parts['DATABASE'] = database

        connstr = encode_connection_string(cstr_parts)
        unicode_results = options.get('unicode_results', False)
        timeout = options.get('connection_timeout', 0)

        conn = None

        conn = Database.connect(connstr, unicode_results=unicode_results,
                                timeout=timeout)
        
        #set decoding 
        #Those decide the charset used for decoding the out-bind parameter. (ex. Select)
        
        #conn.setdecoding(Database.SQL_CHAR, encoding='cp949')
        #conn.setdecoding(Database.SQL_WCHAR, encoding='utf-16')
        #conn.setdecoding(Database.SQL_WMETADATA, encoding='utf-16le')
        
        #set encoding. 
        #It decides the charset used for encoding the in-bind parameter. (ex. Insert, Update)
        
        #conn.setencoding(encoding='cp949')
        return conn 


    def init_connection_state(self):
        cursor = self.create_cursor()
        # Set the territory first. The territory overrides NLS_DATE_FORMAT
        # and NLS_TIMESTAMP_FORMAT to the territory default. When all of
        # these are set in single statement it isn't clear what is supposed
        # to happen.
#        cursor.execute("ALTER SESSION SET NLS_TERRITORY = 'AMERICA'")
        # Set Tibero date to ANSI date format.  This only needs to execute
        # once when we create a new connection. We also set the Territory
        # to 'AMERICA' which forces Sunday to evaluate to a '1' in
        # TO_CHAR().
        cursor.execute(
            "ALTER SESSION SET NLS_DATE_FORMAT = 'YYYY-MM-DD HH24:MI:SS'"
            " NLS_TIMESTAMP_FORMAT = 'YYYY-MM-DD HH24:MI:SS.FF'" +
            (" TIME_ZONE = 'UTC'" if settings.USE_TZ else '')
        )
        cursor.close()
        if 'operators' not in self.__dict__:
            # Ticket #14149: Check whether our LIKE implementation will
            # work for this connection or we need to fall back on LIKEC.
            # This check is performed only once per DatabaseWrapper
            # instance per thread, since subsequent connections will use
            # the same settings.
            cursor = self.create_cursor()
            try:
                cursor.execute("SELECT 1 FROM DUAL WHERE DUMMY %s"
                               % self._standard_operators['contains'],
                               ['X'])
            except DatabaseError:
                self.operators = self._likec_operators
                self.pattern_ops = self._likec_pattern_ops
            else:
                self.operators = self._standard_operators
                self.pattern_ops = self._standard_pattern_ops
            cursor.close()

        try:
            self.connection.stmtcachesize = 20
        except AttributeError:
            # If stmtcache doesn't exist, it will ignored.
            pass
        # Ensure all changes are preserved even when AUTOCOMMIT is False.
        if not self.get_autocommit():
            self.commit()

    def create_cursor(self, name=None):
        return FormatStylePlaceholderCursor(self.connection)

    def _commit(self):
        if self.connection is not None:
            try:
                return self.connection.commit()
            except Database.DatabaseError as e:
                # When error occurs, raise it. 
                raise 

    # Tibero doesn't support releasing savepoints. Only, log it. 
    def _savepoint_commit(self, sid):
        if self.queries_logged:
            self.queries_log.append({
                'sql': '-- RELEASE SAVEPOINT %s (faked)' % self.ops.quote_name(sid),
                'time': '0.000',
            })

    def _set_autocommit(self, autocommit):
        with self.wrap_database_errors:
            self.connection.autocommit = autocommit
        

    def is_usable(self):
        try:
            self.create_cursor().execute("select 1 from dual")
        except Database.Error:
            return False
        else:
            return True
 
    @cached_property
    def tibero_full_version(self):
        return Database.version

    @cached_property
    def tibero_version(self):
        try:
            return int(self.tibero_full_version.split('.')[0])
        except ValueError:
            return None


class VariableWrapper(object):
    """
    This class is used for wrapping the object passed into execute.
    """
    def __init__(self, var):
        self.var = var

    def bind_parameter(self, cursor):
        return self.var

    def __getattr__(self, key):
        return getattr(self.var, key)

    def __setattr__(self, key, value):
        if key == 'var':
            self.__dict__[key] = value
        else:
            setattr(self.var, key, value)


class FormatStylePlaceholderCursor(object):
    """
    Django uses "format" (e.g. '%s') style placeholders, but Tibero uses ":var"
    style. This fixes it -- but note that if you want to use a literal "%s" in
    a query, you'll need to use "%%s".
    """
    
    def __init__(self, connection):
        self.active = True
        self.cursor = connection.cursor()
        # Necessary to retrieve decimal values without rounding error.
#        self.cursor.numbersAsStrings = True
        # Default arraysize of 1 is highly sub-optimal.
        self.cursor.arraysize = 100
        self.connection = connection
        self.last_sql = ''
        self.last_params = ()

    def format_params(self, params):
        fp = []
        if params is not None:
            for p in params:
                if isinstance(p, datetime.timedelta):
                    p = duration_string(p)
                    if ' ' not in p:
                        p = '0 ' + p
                fp.append(p)
        return tuple(fp)

    def format_query(self, query, params):
        if params is not None:
            query = query % tuple('?' * len(params))
        return query

    def execute(self, query, params=None):
        self.last_sql = query
        query = self.format_query(query, params)
        params = self.format_params(params)
        self.last_params = params
        try:
            return self.cursor.execute(query, params)
        except Database.DatabaseError as e:
            raise

    def executemany(self, query, params_list=()):
        if not params_list:
            return None
        # uniform treatment for sequences and iterables
        raw_pll = [p for p in params_list]
        query = self.format_query(query, raw_pll[0])
        params_list = [self.format_params(p) for p in raw_pll]

        try:
            return self.cursor.executemany(query, params_list)
        except Database.DatabaseError as e:
            raise

    def fetchone(self):
        row = self.cursor.fetchone()
        if row is None:
            return row
        return _rowfactory(row, self.cursor)

    def fetchmany(self, size=None):
        if size is None:
            size = self.arraysize
        return tuple(_rowfactory(r, self.cursor) for r in self.cursor.fetchmany(size))

    def fetchall(self):
        return tuple(_rowfactory(r, self.cursor) for r in self.cursor.fetchall())

    def close(self):
        if self.active:
            self.active = False
            self.cursor.close()

    def var(self, *args):
        return VariableWrapper(self.cursor.var(*args))

    def arrayvar(self, *args):
        return VariableWrapper(self.cursor.arrayvar(*args))

    def __getattr__(self, attr):
        if attr in self.__dict__:
            return self.__dict__[attr]
        else:
            return getattr(self.cursor, attr)

    def __iter__(self):
        return CursorIterator(self.cursor)


class CursorIterator(six.Iterator):
    """
    Cursor iterator wrapper that invokes our custom row factory.
    """

    def __init__(self, cursor):
        self.cursor = cursor
        self.iter = iter(cursor)

    def __iter__(self):
        return self

    def __next__(self):
        return _rowfactory(next(self.iter), self.cursor)


def _rowfactory(row, cursor):
    # Cast numeric values as the appropriate Python type based upon the
    # cursor description, and convert strings to unicode.
    casted = []
    for value, desc in zip(row, cursor.description):
        if value is not None and desc[1] is Database.NUMBER:
            precision, scale = desc[4:6]
            if scale == -127:
                if precision == 0:
                    # NUMBER column: decimal-precision floating point
                    # This will normally be an integer from a sequence,
                    # but it could be a decimal value.
                    if '.' in value:
                        value = decimal.Decimal(value)
                    else:
                        value = int(value)
                else:
                    # FLOAT column: binary-precision floating point.
                    # This comes from FloatField columns.
                    value = float(value)
            elif precision > 0:
                # NUMBER(p,s) column: decimal-precision fixed point.
                # This comes from IntField and DecimalField columns.
                if scale == 0:
                    value = int(value)
                else:
                    value = decimal.Decimal(value)
            elif '.' in value:
                # No type information. This normally comes from a
                # mathematical expression in the SELECT list. Guess int
                # or Decimal based on whether it has a decimal point.
                value = decimal.Decimal(value)
            else:
                value = int(value)
        elif desc[1] in (Database.STRING,):
            value = to_unicode(value)
        casted.append(value)
    return tuple(casted)


def to_unicode(s):
    """
    Convert strings to Unicode objects (and return all other data types
    unchanged).
    """
    if isinstance(s, six.string_types):
        return force_text(s)
    return s
