from __future__ import unicode_literals

import datetime
import re
import uuid

from django.conf import settings
from django.db.backends.base.operations import BaseDatabaseOperations
from django.db.backends.utils import truncate_name
from django.utils import six, timezone
from django.utils.encoding import force_bytes, force_text

from .base import Database
from .utils import InsertIdVar, Tibero_datetime, convert_unicode


class DatabaseOperations(BaseDatabaseOperations):
    compiler_module = "django.db.backends.tibero.compiler"

    # Tibero uses NUMBER(11) and NUMBER(19) for integer fields.
    integer_field_ranges = {
        'SmallIntegerField': (-99999999999, 99999999999),
        'IntegerField': (-99999999999, 99999999999),
        'BigIntegerField': (-9999999999999999999, 9999999999999999999),
        'PositiveSmallIntegerField': (0, 99999999999),
        'PositiveIntegerField': (0, 99999999999),
    }

    # TODO: colorize this SQL code with style.SQL_KEYWORD(), etc.
    _sequence_reset_sql = """
DECLARE
    table_value integer;
    seq_value integer;
BEGIN
    SELECT NVL(MAX(%(column)s), 0) INTO table_value FROM %(table)s;
    SELECT NVL(last_number - cache_size, 0) INTO seq_value FROM user_sequences
           WHERE sequence_name = '%(sequence)s';
    WHILE table_value > seq_value LOOP
        SELECT "%(sequence)s".nextval INTO seq_value FROM dual;
    END LOOP;
END;
"""

    def autoinc_sql(self, table, column):
        # To simulate auto-incrementing primary keys in Tibero, we have to
        # create a sequence and a trigger.
        args = {
            'sq_name': self._get_sequence_name(table),
            'tr_name': self._get_trigger_name(table),
            'tbl_name': self.quote_name(table),
            'col_name': self.quote_name(column),
        }
        sequence_sql = """
DECLARE
    i INTEGER;
BEGIN
    SELECT COUNT(*) INTO i FROM USER_CATALOG
        WHERE OBJECT_NAME = '%(sq_name)s' AND OBJECT_TYPE = 'SEQUENCE';
    IF i = 0 THEN
        EXECUTE IMMEDIATE 'CREATE SEQUENCE "%(sq_name)s"';
    END IF;
END;
""" % args
        trigger_sql = """
CREATE OR REPLACE TRIGGER "%(tr_name)s"
BEFORE INSERT ON %(tbl_name)s
FOR EACH ROW
WHEN (new.%(col_name)s IS NULL)
    BEGIN
        SELECT "%(sq_name)s".nextval
        INTO :new.%(col_name)s FROM dual;
    END;
""" % args
        return sequence_sql, trigger_sql

    def cache_key_culling_sql(self):
        return """
            SELECT cache_key
              FROM (SELECT cache_key, rank() OVER (ORDER BY cache_key) AS rank FROM %s)
             WHERE rank = %%s + 1
        """

    def date_extract_sql(self, lookup_type, field_name):
        if lookup_type == 'week_day':
            # TO_CHAR(field, 'D') returns an integer from 1-7, where 1=Sunday.
            return "TO_CHAR(%s, 'D')" % field_name
        else:
            return "EXTRACT(%s FROM %s)" % (lookup_type.upper(), field_name)

    def date_interval_sql(self, timedelta):

        minutes, seconds = divmod(timedelta.seconds, 60)
        hours, minutes = divmod(minutes, 60)
        days = str(timedelta.days)
        day_precision = len(days)
        fmt = "INTERVAL '%s %02d:%02d:%02d.%06d' DAY(%d) TO SECOND(6)"
        return fmt % (days, hours, minutes, seconds, timedelta.microseconds, day_precision), []

    def date_trunc_sql(self, lookup_type, field_name):
        
        if lookup_type in ('year', 'month'):
            return "TRUNC(%s, '%s')" % (field_name, lookup_type.upper())
        else:
            return "TRUNC(%s)" % field_name

    _tzname_re = re.compile(r'^[\w/:+-]+$')

    def _convert_field_to_tz(self, field_name, tzname):
        if settings.USE_TZ:
            if not self._tzname_re.match(tzname):
                raise ValueError("Invalid time zone name: %s" % tzname)
            # Convert from UTC to local time, returning TIMESTAMP WITH TIME ZONE.
            field_name = "(FROM_TZ(%s, '0:00') AT TIME ZONE '%s')" % (field_name, tzname)
        field_name = "TO_CHAR(%s, 'YYYY-MM-DD HH24:MI:SS')" % field_name
        field_name = "TO_DATE(%s, 'YYYY-MM-DD HH24:MI:SS')" % field_name
        # Re-convert to a TIMESTAMP because EXTRACT only handles the date part
        # on DATE values, even though they actually store the time part.
        return "CAST(%s AS TIMESTAMP)" % field_name

    def datetime_cast_date_sql(self, field_name, tzname):
        field_name = self._convert_field_to_tz(field_name, tzname)
        sql = 'TRUNC(%s)' % field_name
        return sql, []

    def datetime_extract_sql(self, lookup_type, field_name, tzname):
        field_name = self._convert_field_to_tz(field_name, tzname)
        sql = self.date_extract_sql(lookup_type, field_name)
        return sql, []

    def datetime_trunc_sql(self, lookup_type, field_name, tzname):
        field_name = self._convert_field_to_tz(field_name, tzname)
        if lookup_type in ('year', 'month'):
            sql = "TRUNC(%s, '%s')" % (field_name, lookup_type.upper())
        elif lookup_type == 'day':
            sql = "TRUNC(%s)" % field_name
        elif lookup_type == 'hour':
            sql = "TRUNC(%s, 'HH24')" % field_name
        elif lookup_type == 'minute':
            sql = "TRUNC(%s, 'MI')" % field_name
        else:
            sql = field_name    # Cast to DATE removes sub-second precision.
        return sql, []

    def get_db_converters(self, expression):
        converters = super(DatabaseOperations, self).get_db_converters(expression)
        internal_type = expression.output_field.get_internal_type()
        if internal_type == 'TextField':
            converters.append(self.convert_textfield_value)
        elif internal_type == 'BinaryField':
            converters.append(self.convert_binaryfield_value)
        elif internal_type in ['BooleanField', 'NullBooleanField']:
            converters.append(self.convert_booleanfield_value)
        elif internal_type == 'DateTimeField':
            converters.append(self.convert_datetimefield_value)
        elif internal_type == 'DateField':
            converters.append(self.convert_datefield_value)
        elif internal_type == 'TimeField':
            converters.append(self.convert_timefield_value)
        elif internal_type == 'UUIDField':
            converters.append(self.convert_uuidfield_value)
        converters.append(self.convert_empty_values)
        return converters

    def convert_textfield_value(self, value, expression, connection, context):
        if value is not None:
            value = force_text(value)
        return value

    def convert_binaryfield_value(self, value, expression, connection, context):
        if value is not None:
            value = force_bytes(value)
        return value

    def convert_booleanfield_value(self, value, expression, connection, context):
        if value in (0, 1):
            value = bool(value)
        return value


    def convert_datetimefield_value(self, value, expression, connection, context):
        if value is not None:
            if settings.USE_TZ:
                value = timezone.make_aware(value, self.connection.timezone)
        return value

    def convert_datefield_value(self, value, expression, connection, context):
        if isinstance(value, Database.Timestamp):
            value = value.date()
        return value

    def convert_timefield_value(self, value, expression, connection, context):
        if isinstance(value, Database.Timestamp):
            value = value.time()
        return value

    def convert_uuidfield_value(self, value, expression, connection, context):
        if value is not None:
            value = uuid.UUID(value)
        return value

    def convert_empty_values(self, value, expression, connection, context):
        field = expression.output_field
        if value is None and field.empty_strings_allowed:
            value = ''
            if field.get_internal_type() == 'BinaryField':
                value = b''
        return value

    def deferrable_sql(self):
        return " " #DEFERRABLE INITIALLY DEFERRED"

    def drop_sequence_sql(self, table):
        return "DROP SEQUENCE %s;" % self.quote_name(self._get_sequence_name(table))

    def fetch_returned_insert_id(self, cursor):
        return int(cursor._insert_id_var.getvalue())

    def field_cast_sql(self, db_type, internal_type):
        if db_type and db_type.endswith('LOB'):
            return "DBMS_LOB.SUBSTR(%s)"
        else:
            return "%s"

    def last_executed_query(self, cursor, sql, params):
        return super(DatabaseOperations, self).last_executed_query(cursor, cursor.last_sql, cursor.last_params)

    def last_insert_id(self, cursor, table_name, pk_name):
        sq_name = self._get_sequence_name(table_name)
        cursor.execute('SELECT "%s".currval FROM dual' % sq_name)
        return cursor.fetchone()[0]

    def lookup_cast(self, lookup_type, internal_type=None):
        if lookup_type in ('iexact', 'icontains', 'istartswith', 'iendswith'):
            return "UPPER(%s)"
        return "%s"

    def max_in_list_size(self):
        return 1000

    def max_name_length(self):
        return 30

    def pk_default_value(self):
        return "NULL"

    def prep_for_iexact_query(self, x):
        return x

    def process_clob(self, value):
        if value is None:
            return ''
        return force_text(value.read())

    def quote_name(self, name):
        if not name.startswith('"') and not name.endswith('"'):
            name = '"%s"' % truncate_name(name.upper(), self.max_name_length())
        name = name.replace('%', '%%')
        return name.upper()

    def random_function_sql(self):
        return "DBMS_RANDOM.RANDOM"

    def regex_lookup(self, lookup_type):
        if lookup_type == 'regex':
            match_option = "'c'"
        else:
            match_option = "'i'"
        return 'REGEXP_LIKE(%%s, %%s, %s)' % match_option

    def return_insert_id(self):
        return "RETURNING %s INTO %%s", (InsertIdVar(),)

    def savepoint_create_sql(self, sid):
        return "SAVEPOINT " + self.quote_name(sid)

    def savepoint_rollback_sql(self, sid):
        return "ROLLBACK TO SAVEPOINT " + self.quote_name(sid)

    def sql_flush(self, style, tables, sequences, allow_cascade=False):
        # Return a list of 'TRUNCATE x;', 'TRUNCATE y;',
        # 'TRUNCATE z;'... style SQL statements
        if tables:
            sql = ['%s %s %s;' % (
                style.SQL_KEYWORD('DELETE'),
                style.SQL_KEYWORD('FROM'),
                style.SQL_FIELD(self.quote_name(table))
            ) for table in tables]
            # Since we've just deleted all the rows, running our sequence
            # ALTER code will reset the sequence to 0.
            sql.extend(self.sequence_reset_by_name_sql(style, sequences))
            return sql
        else:
            return []

    def sequence_reset_by_name_sql(self, style, sequences):
        sql = []
        for sequence_info in sequences:
            sequence_name = self._get_sequence_name(sequence_info['table'])
            table_name = self.quote_name(sequence_info['table'])
            column_name = self.quote_name(sequence_info['column'] or 'id')
            query = self._sequence_reset_sql % {
                'sequence': sequence_name,
                'table': table_name,
                'column': column_name,
            }
            sql.append(query)
        return sql

    def sequence_reset_sql(self, style, model_list):
        from django.db import models
        output = []
        query = self._sequence_reset_sql
        for model in model_list:
            for f in model._meta.local_fields:
                if isinstance(f, models.AutoField):
                    table_name = self.quote_name(model._meta.db_table)
                    sequence_name = self._get_sequence_name(model._meta.db_table)
                    column_name = self.quote_name(f.column)
                    output.append(query % {'sequence': sequence_name,
                                           'table': table_name,
                                           'column': column_name})
                    # Only one AutoField is allowed per model, so don't
                    # continue to loop
                    break
            for f in model._meta.many_to_many:
                if not f.remote_field.through:
                    table_name = self.quote_name(f.m2m_db_table())
                    sequence_name = self._get_sequence_name(f.m2m_db_table())
                    column_name = self.quote_name('id')
                    output.append(query % {'sequence': sequence_name,
                                           'table': table_name,
                                           'column': column_name})
        return output

    def start_transaction_sql(self):
        return ''

    def tablespace_sql(self, tablespace, inline=False):
        if inline:
            return "USING INDEX TABLESPACE %s" % self.quote_name(tablespace)
        else:
            return "TABLESPACE %s" % self.quote_name(tablespace)

    def adapt_datefield_value(self, value):
        return value

    def adapt_datetimefield_value(self, value):
        if value is None:
            return None

        if timezone.is_aware(value):
            if settings.USE_TZ:
                value = timezone.make_naive(value, self.connection.timezone)
            else:
                raise ValueError("Tibero backend does not support timezone-aware datetimes when USE_TZ is False.")

        return Tibero_datetime.from_datetime(value)

    def adapt_timefield_value(self, value):
        if value is None:
            return None

        if isinstance(value, six.string_types):
            return datetime.datetime.strptime(value, '%H:%M:%S')

        if timezone.is_aware(value):
            raise ValueError("Tibero backend does not support timezone-aware times.")

        return Tibero_datetime(1900, 1, 1, value.hour, value.minute,
                               value.second, value.microsecond)

    def combine_expression(self, connector, sub_expressions):
        "Tibero requires special cases for %% and & operators in query expressions"
        if connector == '%%':
            return 'MOD(%s)' % ','.join(sub_expressions)
        elif connector == '&':
            return 'BITAND(%s)' % ','.join(sub_expressions)
        elif connector == '|':
            raise NotImplementedError("Bit-wise or is not supported in Tibero.")
        elif connector == '^':
            return 'POWER(%s)' % ','.join(sub_expressions)
        return super(DatabaseOperations, self).combine_expression(connector, sub_expressions)

    def _get_sequence_name(self, table):
        name_length = self.max_name_length() - 3
        return '%s_SQ' % truncate_name(table, name_length).upper()

    def _get_trigger_name(self, table):
        name_length = self.max_name_length() - 3
        return '%s_TR' % truncate_name(table, name_length).upper()

    def bulk_insert_sql(self, fields, placeholder_rows):
        return " UNION ALL ".join(
            "SELECT %s FROM DUAL" % ", ".join(row)
            for row in placeholder_rows
        )

    def subtract_temporals(self, internal_type, lhs, rhs):
        if internal_type == 'DateField':
            lhs_sql, lhs_params = lhs
            rhs_sql, rhs_params = rhs
            return "NUMTODSINTERVAL(%s - %s, 'DAY')" % (lhs_sql, rhs_sql), lhs_params + rhs_params
        return super(DatabaseOperations, self).subtract_temporals(internal_type, lhs, rhs)
